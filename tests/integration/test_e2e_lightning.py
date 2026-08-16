"""End-to-end integration test: the real Supervisor wired to a REAL
BlitzortungClient (config -> Blitzortung MQTT -> LightningStateMachine ->
LightningWiring -> MQTT publish) against a local Mosquitto broker that
doubles as both the user's broker AND the fake Blitzortung broker (task C5).

Requires a broker reachable at MQTT_TEST_HOST (default "localhost") on port
1883 with anonymous auth allowed - see tests/integration/
test_mqtt_roundtrip.py for the docker run command. Never touches
blitzortung.ha.sed.pl: BLITZORTUNG_MQTT_HOST is pointed at the same local
Mosquitto instance and a synthetic strike is published directly to the
geohash topic BlitzortungClient itself subscribes to.

Also never touches the real api.weather.gov: this test sets
NWS_ENABLED=false, and ``Supervisor.start()`` now actually honors that (a
real spec bug this test's author found -- NWS_ENABLED=false used to leave
``Supervisor._nws_loop`` starting unconditionally, hitting the real NWS
endpoint on its very first poll unless something local absorbed it). With
that fixed, no NWS poll thread starts at all when NWS is disabled, so no
stub HTTP server is needed here anymore as a safety net -- this test
exercises exactly the "lightning-only deployment" scenario the fix exists
for. See DESIGN.md's healthz doc / docs/TROUBLESHOOTING.md for the
now-current contract: ``sources.nws`` is simply absent from /healthz when
NWS_ENABLED=false, rather than reported unavailable.

Wiring mirrors main() in src/stormwatch/__main__.py: build Config, build
Supervisor(config), then attach a real BlitzortungClient via
``_make_blitzortung_client`` *before* calling ``start()`` (Supervisor never
auto-creates one - see the Supervisor class docstring).

Compressed all-clear timer: Config.all_clear_minutes is an int (whole
minutes only), so the smallest real value is 60s - too slow for a bounded
test. LightningWiring builds its own real LightningStateMachine internally
and Supervisor exposes no constructor hook to inject a different one, so
this test reaches into the already-constructed object and overwrites the
private ``_all_clear_seconds`` duration directly (see state.py's
LightningStateMachine.__init__) before calling ``start()`` and before any
strike arrives. This is a test-only patch of instance state, not a source
edit.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable

import paho.mqtt.client as mqtt
import pytest

from stormwatch.__main__ import Supervisor, _lightning_entities, _make_blitzortung_client
from stormwatch.config import Config
from stormwatch.geo import cells_for_radius, distance_and_bearing
from stormwatch.publisher import AVAILABILITY_TOPIC

MQTT_HOST = os.environ.get("MQTT_TEST_HOST", "localhost")
MQTT_PORT = 1883
_DEADLINE_S = 10.0
_POLL_S = 0.1

# Mirrors the hardcoded `_HEALTH_PORT` in stormwatch/__main__.py (not
# currently configurable via Supervisor/Config).
_HEALTH_PORT = 8099
_HEALTHZ_URL = f"http://127.0.0.1:{_HEALTH_PORT}/healthz"

_EARTH_RADIUS_KM = 6371.0  # mirrors stormwatch.geo's own constant/docstring
_STRIKE_DISTANCE_KM = 5.0

TEST_LAT = 34.0234
TEST_LON = -84.6155
# Offset due north by ~5km (pure latitude delta keeps the geo.py math simple
# and exact: distance_and_bearing's equirectangular formula reduces to
# EARTH_RADIUS_KM * |delta_phi| when delta_lambda is 0).
STRIKE_LAT = TEST_LAT + math.degrees(_STRIKE_DISTANCE_KM / _EARTH_RADIUS_KM)
STRIKE_LON = TEST_LON

# Compressed all-clear window for the test (see module docstring); the
# lightning ticker runs every 1s (_LIGHTNING_TICK_SECONDS in __main__.py),
# so this fires within ~1s of expiry.
_ALL_CLEAR_SECONDS = 4.0


def _wait_until(predicate: Callable[[], bool], deadline_s: float = _DEADLINE_S) -> bool:
    """Poll predicate() until True or the deadline passes (no bare long sleeps)."""
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        if predicate():
            return True
        time.sleep(_POLL_S)
    return predicate()


class Observer:
    """Subscribes to stormwatch/# and homeassistant/# and keeps the latest
    payload seen per topic (mirrors tests/integration/test_mqtt_roundtrip.py).
    Also doubles as the publisher of the synthetic Blitzortung strike, since
    it already owns a live connection to the same local broker."""

    def __init__(self, host: str, port: int) -> None:
        self.messages: dict[str, str] = {}
        self.subscribed = False
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="", clean_session=True
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        client.subscribe("stormwatch/#", qos=1)
        client.subscribe("homeassistant/#", qos=1)
        self.subscribed = True

    def _on_message(self, client, userdata, msg) -> None:
        self.messages[msg.topic] = msg.payload.decode("utf-8")

    def publish_strike(self, topic: str, lat: float, lon: float) -> None:
        payload = json.dumps({"lat": lat, "lon": lon, "time": int(time.time() * 1e9)})
        self.client.publish(topic, payload=payload, qos=1)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def _wait_for_health(
    predicate: Callable[[dict], bool], deadline_s: float = _DEADLINE_S
) -> dict | None:
    """Poll GET /healthz until predicate(body) is True or the deadline passes.

    Tolerates connection errors early on (the health server binds
    synchronously in Supervisor.start(), but this keeps the helper robust
    regardless)."""
    start = time.monotonic()
    last: dict | None = None
    while time.monotonic() - start < deadline_s:
        try:
            with urllib.request.urlopen(_HEALTHZ_URL, timeout=2) as response:
                last = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError):
            last = None
        else:
            if predicate(last):
                return last
        time.sleep(_POLL_S)
    return last


def _health_port_closed(deadline_s: float = _DEADLINE_S) -> bool:
    """True once nothing accepts connections on the health port anymore."""

    def _closed() -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", _HEALTH_PORT)) != 0

    return _wait_until(_closed, deadline_s)


@pytest.fixture
def observer():
    obs = Observer(MQTT_HOST, MQTT_PORT)
    assert _wait_until(lambda: obs.subscribed), "observer failed to subscribe within deadline"
    yield obs
    obs.close()


@pytest.mark.integration
def test_supervisor_e2e_lightning_strike_closes_and_all_clears(observer, tmp_path) -> None:
    # Sanity-check the synthetic strike's geometry before trusting the rest
    # of the test on it: STRIKE_LAT/STRIKE_LON must actually be ~5km from
    # the configured home point.
    computed_km, _bearing = distance_and_bearing(TEST_LAT, TEST_LON, STRIKE_LAT, STRIKE_LON)
    assert abs(computed_km - _STRIKE_DISTANCE_KM) < 0.05

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    device = f"StormWatch-E2E-Lightning-{uuid.uuid4().hex[:8]}"

    config = Config(
        latitude=TEST_LAT,
        longitude=TEST_LON,
        mqtt_host=MQTT_HOST,
        mqtt_port=MQTT_PORT,
        nws_enabled=False,
        rain_enabled=False,
        nws_contact="",  # not required: both NWS and rain disabled
        blitzortung_enabled=True,
        blitzortung_mqtt_host=MQTT_HOST,
        blitzortung_mqtt_port=MQTT_PORT,
        config_dir=str(config_dir),
        device_name=device,
    )

    # Real strike topic BlitzortungClient itself subscribes to for this
    # config (center cell of cells_for_radius(lat, lon, watch_radius_km)),
    # per DESIGN.md/task-C5-brief.md's "blitzortung/1.1/<geohash>/#" scheme.
    home_cell = cells_for_radius(config.latitude, config.longitude, config.watch_radius_km)[0]
    strike_topic = f"blitzortung/1.1/{'/'.join(home_cell)}/strike"

    supervisor = Supervisor(config)
    supervisor.blitzortung_client = _make_blitzortung_client(config, supervisor)
    # See module docstring: compress the all-clear timer directly on the
    # already-built LightningStateMachine instance instead of via config.
    supervisor._lightning._state._all_clear_seconds = _ALL_CLEAR_SECONDS

    try:
        supervisor.start()

        assert _wait_until(lambda: observer.messages.get(AVAILABILITY_TOPIC) == "online"), (
            "publisher never came online"
        )
        # NWS_ENABLED=false must not prevent the rest of the container from
        # coming up and staying up -- and (the fix under test) must not
        # start the NWS poll thread or the alerts.yaml hot-reload thread at
        # all, not just tolerate their unavailability.
        assert all(t.is_alive() for t in supervisor._threads), (
            "a supervisor thread died on startup with NWS disabled"
        )
        thread_names = {t.name for t in supervisor._threads}
        assert "stormwatch-nws" not in thread_names, (
            "NWS poll thread started despite NWS_ENABLED=false"
        )
        assert "stormwatch-rules" not in thread_names, (
            "rules hot-reload thread started despite NWS_ENABLED=false"
        )

        slug = supervisor.publisher.slug
        discovery_topics = [
            f"{config.discovery_prefix}/{entity.component}/{slug}/{slug}_{entity.key}/config"
            for entity in _lightning_entities(config)
        ]
        assert _wait_until(lambda: all(t in observer.messages for t in discovery_topics)), (
            "not all lightning discovery configs arrived"
        )

        # healthz should reflect the (real) Blitzortung connection coming up,
        # independent of any strike having happened yet.
        health = _wait_for_health(
            lambda body: body.get("sources", {}).get("lightning", {}).get("available") is True
        )
        assert health is not None, "healthz never reported the lightning source available"
        assert health["sources"]["lightning"]["available"] is True
        assert health["status"] == "ok"
        # NWS_ENABLED=false: "nws" is absent from sources entirely, not
        # reported as unavailable (mirrors "lightning"'s own absence when
        # BLITZORTUNG_ENABLED=false).
        assert "nws" not in health["sources"]

        # Publish the synthetic strike, re-sending it every poll tick until
        # swim_status flips to CLOSED - avoids racing BlitzortungClient's
        # async connect -> subscribe handshake with a blind sleep. Re-sends
        # are safe: LightningStateMachine only (re)emits 'lightning_close'
        # on the CLEAR/WATCH -> CLOSED transition, not on every strike.
        def _closed() -> bool:
            observer.publish_strike(strike_topic, STRIKE_LAT, STRIKE_LON)
            return observer.messages.get("stormwatch/state/swim_status") == "CLOSED"

        assert _wait_until(_closed, deadline_s=8.0), "swim_status never reached CLOSED"

        assert _wait_until(
            lambda: observer.messages.get("stormwatch/state/lightning_nearby") == "ON"
        ), "lightning_nearby never went ON"

        assert _wait_until(lambda: "stormwatch/event/lightning_close" in observer.messages), (
            "no lightning_close event observed"
        )
        close_payload = json.loads(observer.messages["stormwatch/event/lightning_close"])
        assert abs(close_payload["distance_km"] - _STRIKE_DISTANCE_KM) < 0.5

        # No further strikes: within the compressed all-clear window, the
        # state machine should time out back to CLEAR.
        assert _wait_until(
            lambda: "stormwatch/event/all_clear" in observer.messages, deadline_s=10.0
        ), "no all_clear event observed within the compressed window"
        assert _wait_until(
            lambda: observer.messages.get("stormwatch/state/swim_status") == "CLEAR"
        ), "swim_status never returned to CLEAR"
    finally:
        supervisor.stop()

    # Clean teardown: no leaked threads, ports, or client connections.
    assert all(not t.is_alive() for t in supervisor._threads), (
        "a supervisor thread was still alive after stop()"
    )
    assert supervisor.blitzortung_client.available is False, (
        "Blitzortung client did not report disconnected after stop()"
    )
    assert _health_port_closed(), "health server port still accepting connections after stop()"
    assert _wait_until(lambda: observer.messages.get(AVAILABILITY_TOPIC) == "offline"), (
        "clean shutdown never published offline"
    )
