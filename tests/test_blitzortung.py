"""Unit tests for stormwatch.sources.blitzortung (DESIGN.md §4.1).

No network, no real broker, no sleeps: a small fake MQTT client records
subscribe/callback wiring so we can invoke paho's callbacks directly and
assert on distance/bearing math, availability tracking, and malformed
payload handling.
"""

from __future__ import annotations

import json
import logging

import paho.mqtt.client as mqtt
import pytest

from stormwatch.config import Config
from stormwatch.geo import cells_for_radius, distance_and_bearing
from stormwatch.sources import blitzortung
from stormwatch.sources.blitzortung import BlitzortungClient


class FakeMqttMessage:
    """Minimal stand-in for paho.mqtt.client.MQTTMessage."""

    def __init__(self, payload: bytes) -> None:
        self.topic = "blitzortung/1.1/x/y/#"
        self.payload = payload


class FakeMqttClient:
    """Minimal stand-in for paho.mqtt.client.Client (VERSION2 callback shape)."""

    def __init__(self) -> None:
        self.connected_to: tuple[str, int, int] | None = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.subscriptions: list[str] = []
        self.reconnect_delay: tuple[int, int] | None = None
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connected_to = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.disconnected = True

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscriptions.append(topic)


@pytest.fixture
def config() -> Config:
    return Config(
        latitude=35.2271,
        longitude=-80.8431,
        mqtt_host="localhost",
        blitzortung_mqtt_host="blitzortung.ha.sed.pl",
        watch_radius_km=40.23,
    )


@pytest.fixture
def fake_client() -> FakeMqttClient:
    return FakeMqttClient()


class _Recorder:
    """Records on_strike(distance_km, bearing_deg, age_s) calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, float, float]] = []

    def __call__(self, distance_km: float, bearing_deg: float, age_s: float) -> None:
        self.calls.append((distance_km, bearing_deg, age_s))


def _connect(fake_client: FakeMqttClient, client: BlitzortungClient) -> None:
    client.start()
    fake_client.on_connect(fake_client, None, {}, 0, None)


# --- start() / connection setup ---------------------------------------------


def test_start_uses_configured_blitzortung_host_and_port(config, fake_client) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    client.start()

    assert fake_client.connected_to == (
        config.blitzortung_mqtt_host,
        config.blitzortung_mqtt_port,
        60,
    )
    assert fake_client.loop_started is True


def test_start_configures_reconnect_delay_1_to_300(config, fake_client) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    client.start()

    assert fake_client.reconnect_delay == (1, 300)


def test_stop_disconnects_and_stops_loop(config, fake_client) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    client.start()

    client.stop()

    assert fake_client.disconnected is True
    assert fake_client.loop_stopped is True


# --- subscriptions -----------------------------------------------------------


def test_on_connect_subscribes_exactly_cells_for_radius_topics(config, fake_client) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    _connect(fake_client, client)

    expected_cells = cells_for_radius(config.latitude, config.longitude, config.watch_radius_km)
    expected_topics = {f"blitzortung/1.1/{'/'.join(cell)}/#" for cell in expected_cells}

    assert set(fake_client.subscriptions) == expected_topics
    assert len(fake_client.subscriptions) == len(expected_cells)


def test_subscription_topic_uses_slash_separated_geohash_chars(config, fake_client) -> None:
    # A small radius selects precision-5 cells (geo.py: <25km -> p5), so the
    # subscribed topic has five single-char levels before the wildcard --
    # matching the "blitzortung/1.1/c/h/a/r/s/#" shape from the task brief.
    small_config = Config(
        latitude=config.latitude,
        longitude=config.longitude,
        mqtt_host=config.mqtt_host,
        watch_radius_km=5.0,
    )
    client = BlitzortungClient(small_config, on_strike=_Recorder(), client=fake_client)
    _connect(fake_client, client)

    center_cell = cells_for_radius(small_config.latitude, small_config.longitude, 5.0)[0]
    assert len(center_cell) == 5
    expected_topic = f"blitzortung/1.1/{'/'.join(center_cell)}/#"
    assert expected_topic in fake_client.subscriptions


def test_on_connect_with_failure_reason_code_does_not_subscribe(config, fake_client) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    client.start()

    fake_client.on_connect(fake_client, None, {}, 1, None)

    assert fake_client.subscriptions == []


# --- on_message: valid strikes ------------------------------------------------


def test_on_message_valid_strike_calls_on_strike_with_correct_distance_and_bearing(
    config, fake_client, monkeypatch
) -> None:
    monkeypatch.setattr(blitzortung.time, "time", lambda: 1_700_000_100.0)
    recorder = _Recorder()
    client = BlitzortungClient(config, on_strike=recorder, client=fake_client)
    _connect(fake_client, client)

    strike_lat, strike_lon = 35.30, -80.90
    strike_time_ns = int(1_700_000_000.0 * 1e9)  # 100s before the mocked "now"
    payload = json.dumps({"lat": strike_lat, "lon": strike_lon, "time": strike_time_ns}).encode()

    fake_client.on_message(fake_client, None, FakeMqttMessage(payload))

    assert len(recorder.calls) == 1
    distance_km, bearing_deg, age_s = recorder.calls[0]
    expected_distance, expected_bearing = distance_and_bearing(
        config.latitude, config.longitude, strike_lat, strike_lon
    )
    assert distance_km == pytest.approx(expected_distance)
    assert bearing_deg == pytest.approx(expected_bearing)
    assert age_s == pytest.approx(100.0, abs=1e-6)


def test_on_message_missing_time_defaults_age_to_zero(config, fake_client) -> None:
    recorder = _Recorder()
    client = BlitzortungClient(config, on_strike=recorder, client=fake_client)
    _connect(fake_client, client)

    payload = json.dumps({"lat": 35.30, "lon": -80.90}).encode()
    fake_client.on_message(fake_client, None, FakeMqttMessage(payload))

    assert len(recorder.calls) == 1
    assert recorder.calls[0][2] == 0.0


def test_on_message_future_strike_time_clamps_age_to_zero(config, fake_client, monkeypatch) -> None:
    monkeypatch.setattr(blitzortung.time, "time", lambda: 1_700_000_000.0)
    recorder = _Recorder()
    client = BlitzortungClient(config, on_strike=recorder, client=fake_client)
    _connect(fake_client, client)

    # Strike timestamp is 5s ahead of "now" (clock skew) -- age must clamp to 0.
    strike_time_ns = int(1_700_000_005.0 * 1e9)
    payload = json.dumps({"lat": 35.30, "lon": -80.90, "time": strike_time_ns}).encode()
    fake_client.on_message(fake_client, None, FakeMqttMessage(payload))

    assert recorder.calls[0][2] == 0.0


# --- on_message: malformed payloads ------------------------------------------


def test_on_message_bad_json_is_ignored_and_logged(config, fake_client, caplog) -> None:
    recorder = _Recorder()
    client = BlitzortungClient(config, on_strike=recorder, client=fake_client)
    _connect(fake_client, client)

    with caplog.at_level(logging.DEBUG, logger="stormwatch.sources.blitzortung"):
        fake_client.on_message(fake_client, None, FakeMqttMessage(b"not json{{{"))

    assert recorder.calls == []
    assert any("malformed" in r.message.lower() for r in caplog.records)


def test_on_message_missing_lat_lon_is_ignored(config, fake_client) -> None:
    recorder = _Recorder()
    client = BlitzortungClient(config, on_strike=recorder, client=fake_client)
    _connect(fake_client, client)

    payload = json.dumps({"time": 1_700_000_000_000_000_000}).encode()
    fake_client.on_message(fake_client, None, FakeMqttMessage(payload))

    assert recorder.calls == []


def test_on_message_non_numeric_lat_lon_is_ignored(config, fake_client) -> None:
    recorder = _Recorder()
    client = BlitzortungClient(config, on_strike=recorder, client=fake_client)
    _connect(fake_client, client)

    payload = json.dumps({"lat": "north", "lon": "west"}).encode()
    fake_client.on_message(fake_client, None, FakeMqttMessage(payload))

    assert recorder.calls == []


def test_on_message_non_dict_json_is_ignored(config, fake_client) -> None:
    recorder = _Recorder()
    client = BlitzortungClient(config, on_strike=recorder, client=fake_client)
    _connect(fake_client, client)

    payload = json.dumps([1, 2, 3]).encode()
    fake_client.on_message(fake_client, None, FakeMqttMessage(payload))

    assert recorder.calls == []


def test_on_message_never_raises(config, fake_client) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    _connect(fake_client, client)

    # None of these should raise out of the callback.
    fake_client.on_message(fake_client, None, FakeMqttMessage(b""))
    fake_client.on_message(fake_client, None, FakeMqttMessage(b"null"))
    fake_client.on_message(fake_client, None, FakeMqttMessage(b'{"lat": null, "lon": null}'))


# --- available -----------------------------------------------------------------


def test_available_false_before_start(config, fake_client) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    assert client.available is False


def test_available_true_after_on_connect_success(config, fake_client) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    _connect(fake_client, client)

    assert client.available is True


def test_available_false_after_on_connect_failure_reason_code(config, fake_client) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    client.start()

    fake_client.on_connect(fake_client, None, {}, 1, None)

    assert client.available is False


def test_available_false_after_on_disconnect(config, fake_client) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    _connect(fake_client, client)
    assert client.available is True

    fake_client.on_disconnect(fake_client, None, {}, 7, None)

    assert client.available is False


def test_on_disconnect_logs_warning(config, fake_client, caplog) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder(), client=fake_client)
    _connect(fake_client, client)

    with caplog.at_level(logging.WARNING, logger="stormwatch.sources.blitzortung"):
        fake_client.on_disconnect(fake_client, None, {}, 7, None)

    assert any("connection lost" in r.message.lower() for r in caplog.records)


# --- own paho client, separate from Publisher's ------------------------------


def test_default_client_is_paho_version2(config) -> None:
    client = BlitzortungClient(config, on_strike=_Recorder())

    assert isinstance(client.client, mqtt.Client)
    assert client.client.callback_api_version is mqtt.CallbackAPIVersion.VERSION2


def test_each_instance_builds_its_own_client(config) -> None:
    client_a = BlitzortungClient(config, on_strike=_Recorder())
    client_b = BlitzortungClient(config, on_strike=_Recorder())

    assert client_a.client is not client_b.client
