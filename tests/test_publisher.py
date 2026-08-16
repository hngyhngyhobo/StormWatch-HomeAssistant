"""Unit tests for stormwatch.publisher (DESIGN.md §5, §8).

No network, no real broker: a small fake MQTT client records every call the
Publisher makes so we can assert on topics/payloads/retain flags directly.
"""

from __future__ import annotations

import json

import pytest

from stormwatch import __version__
from stormwatch.config import Config
from stormwatch.publisher import (
    ATTR_TOPIC_PREFIX,
    AVAILABILITY_TOPIC,
    EVENT_TOPIC_PREFIX,
    STATE_TOPIC_PREFIX,
    EntitySpec,
    Publisher,
)


class _Publish:
    """One recorded client.publish() call."""

    def __init__(self, topic: str, payload, qos: int, retain: bool) -> None:
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain


class FakeMqttClient:
    """Minimal stand-in for paho.mqtt.client.Client (VERSION2 callback shape)."""

    def __init__(self) -> None:
        self.will: tuple[str, object, int, bool] | None = None
        self.connected_to: tuple[str, int] | None = None
        self.loop_started = False
        self.published: list[_Publish] = []
        self.on_connect = None
        self.on_disconnect = None
        self.username: str | None = None
        self.password: str | None = None

    def will_set(self, topic: str, payload=None, qos: int = 0, retain: bool = False) -> None:
        self.will = (topic, payload, qos, retain)

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.username = username
        self.password = password

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connected_to = (host, port)

    def loop_start(self) -> None:
        self.loop_started = True

    def publish(self, topic: str, payload=None, qos: int = 0, retain: bool = False) -> None:
        self.published.append(_Publish(topic, payload, qos, retain))


@pytest.fixture
def config() -> Config:
    return Config(latitude=35.2, longitude=-80.8, mqtt_host="localhost")


@pytest.fixture
def fake_client() -> FakeMqttClient:
    return FakeMqttClient()


def _by_topic(client: FakeMqttClient) -> dict[str, _Publish]:
    return {p.topic: p for p in client.published}


# --- connect() / LWT -------------------------------------------------------


def test_connect_sets_last_will_offline_retained(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.connect()
    assert fake_client.will == (AVAILABILITY_TOPIC, "offline", 1, True)


def test_connect_uses_configured_host_and_port(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.connect()
    assert fake_client.connected_to == (config.mqtt_host, config.mqtt_port)
    assert fake_client.loop_started is True


def test_connect_sets_credentials_when_configured(fake_client) -> None:
    config = Config(
        latitude=1.0,
        longitude=2.0,
        mqtt_host="broker",
        mqtt_username="alice",
        mqtt_password="secret",
    )
    pub = Publisher(config, client=fake_client)
    pub.connect()
    assert fake_client.username == "alice"
    assert fake_client.password == "secret"


# --- on_connect: online + discovery publish ---------------------------------


def test_on_connect_publishes_online_retained(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.connect()
    fake_client.published.clear()

    fake_client.on_connect(fake_client, None, {}, 0, None)

    topics = _by_topic(fake_client)
    assert topics[AVAILABILITY_TOPIC].payload == "online"
    assert topics[AVAILABILITY_TOPIC].retain is True
    assert topics[AVAILABILITY_TOPIC].qos == 1


def test_on_connect_republishes_discovery_for_registered_entities(config, fake_client) -> None:
    entity = EntitySpec(key="active_alerts", name="Active Alerts", component="sensor")
    pub = Publisher(config, client=fake_client)
    pub.publish_discovery([entity])
    pub.connect()
    fake_client.published.clear()

    fake_client.on_connect(fake_client, None, {}, 0, None)

    discovery_topic = "homeassistant/sensor/stormwatch/stormwatch_active_alerts/config"
    topics = _by_topic(fake_client)
    assert discovery_topic in topics
    assert topics[discovery_topic].retain is True


def test_on_connect_does_nothing_extra_on_failure_reason_code(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.connect()
    fake_client.published.clear()

    fake_client.on_connect(fake_client, None, {}, 1, None)

    assert fake_client.published == []


# --- connected property / on_connect / on_disconnect -------------------------


def test_connected_is_false_before_connect(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    assert pub.connected is False


def test_on_connect_sets_connected_true(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.connect()
    assert pub.connected is False

    fake_client.on_connect(fake_client, None, {}, 0, None)

    assert pub.connected is True


def test_on_connect_does_not_set_connected_true_on_failure_reason_code(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.connect()

    fake_client.on_connect(fake_client, None, {}, 1, None)

    assert pub.connected is False


def test_on_disconnect_sets_connected_false(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.connect()
    fake_client.on_connect(fake_client, None, {}, 0, None)
    assert pub.connected is True

    fake_client.on_disconnect(fake_client, None, {}, 7, None)

    assert pub.connected is False


def test_on_disconnect_logs_warning_with_reason_code(config, fake_client, caplog) -> None:
    import logging

    pub = Publisher(config, client=fake_client)
    pub.connect()

    with caplog.at_level(logging.WARNING, logger="stormwatch.publisher"):
        fake_client.on_disconnect(fake_client, None, {}, 7, None)

    assert any("MQTT connection lost" in record.message for record in caplog.records)


# --- publish_discovery() ----------------------------------------------------


def test_discovery_payload_topic_and_core_fields(config, fake_client) -> None:
    entity = EntitySpec(key="active_alerts", name="Active Alerts", component="sensor")
    pub = Publisher(config, client=fake_client)
    pub.publish_discovery([entity])

    msg = fake_client.published[-1]
    assert msg.topic == "homeassistant/sensor/stormwatch/stormwatch_active_alerts/config"
    assert msg.retain is True
    assert msg.qos == 1

    body = json.loads(msg.payload)
    assert body["unique_id"] == "stormwatch_active_alerts"
    assert body["name"] == "Active Alerts"
    assert body["state_topic"] == "stormwatch/state/active_alerts"
    assert body["availability_topic"] == AVAILABILITY_TOPIC
    assert body["payload_available"] == "online"
    assert body["payload_not_available"] == "offline"
    assert body["device"] == {
        "identifiers": ["stormwatch"],
        "name": "StormWatch",
        "manufacturer": "StormWatch",
        "sw_version": __version__,
    }
    assert "json_attributes_topic" not in body


def test_discovery_payload_includes_json_attributes_topic_when_flagged(config, fake_client) -> None:
    entity = EntitySpec(
        key="active_alerts",
        name="Active Alerts",
        component="sensor",
        value_is_json_attr=True,
    )
    pub = Publisher(config, client=fake_client)
    pub.publish_discovery([entity])

    body = json.loads(fake_client.published[-1].payload)
    assert body["json_attributes_topic"] == "stormwatch/attr/active_alerts"


def test_discovery_payload_optional_fields(config, fake_client) -> None:
    entity = EntitySpec(
        key="config_ok",
        name="Config OK",
        component="binary_sensor",
        device_class="problem",
        unit="km",
        state_class="measurement",
        icon="mdi:cog-outline",
    )
    pub = Publisher(config, client=fake_client)
    pub.publish_discovery([entity])

    body = json.loads(fake_client.published[-1].payload)
    assert body["device_class"] == "problem"
    assert body["unit_of_measurement"] == "km"
    assert body["state_class"] == "measurement"
    assert body["icon"] == "mdi:cog-outline"


def test_discovery_payload_includes_entity_category_when_set(config, fake_client) -> None:
    entity = EntitySpec(
        key="last_error",
        name="Last Error",
        component="sensor",
        entity_category="diagnostic",
    )
    pub = Publisher(config, client=fake_client)
    pub.publish_discovery([entity])

    body = json.loads(fake_client.published[-1].payload)
    assert body["entity_category"] == "diagnostic"


def test_discovery_payload_omits_entity_category_when_unset(config, fake_client) -> None:
    entity = EntitySpec(key="active_alerts", name="Active Alerts", component="sensor")
    pub = Publisher(config, client=fake_client)
    pub.publish_discovery([entity])

    body = json.loads(fake_client.published[-1].payload)
    assert "entity_category" not in body


def test_discovery_topic_and_slug_from_custom_device_name(fake_client) -> None:
    config = Config(latitude=1.0, longitude=2.0, mqtt_host="h", device_name="My Weather!!")
    entity = EntitySpec(key="x", name="X", component="sensor")
    pub = Publisher(config, client=fake_client)
    pub.publish_discovery([entity])

    msg = fake_client.published[-1]
    assert msg.topic == "homeassistant/sensor/my_weather__/my_weather___x/config"
    body = json.loads(msg.payload)
    assert body["unique_id"] == "my_weather___x"
    assert body["device"]["identifiers"] == ["my_weather__"]
    assert body["device"]["name"] == "My Weather!!"


def test_discovery_uses_configured_prefix(fake_client) -> None:
    config = Config(latitude=1.0, longitude=2.0, mqtt_host="h", discovery_prefix="custom_prefix")
    entity = EntitySpec(key="x", name="X", component="sensor")
    pub = Publisher(config, client=fake_client)
    pub.publish_discovery([entity])

    assert fake_client.published[-1].topic == "custom_prefix/sensor/stormwatch/stormwatch_x/config"


# --- publish_state() ---------------------------------------------------------


def test_publish_state_retains_int_value(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.publish_state("active_alerts", 2)

    msg = fake_client.published[-1]
    assert msg.topic == f"{STATE_TOPIC_PREFIX}/active_alerts"
    assert msg.payload == "2"
    assert msg.retain is True


def test_publish_state_string_value(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.publish_state("highest_alert", "Tornado Warning")
    assert fake_client.published[-1].payload == "Tornado Warning"


def test_publish_state_bool_value_as_on_off(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.publish_state("critical_alert", True)
    assert fake_client.published[-1].payload == "ON"

    pub.publish_state("critical_alert", False)
    assert fake_client.published[-1].payload == "OFF"


def test_publish_state_rounds_floats_to_one_decimal(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.publish_state("nearest_strike_distance", 7.2345)
    assert fake_client.published[-1].payload == "7.2"

    pub.publish_state("nearest_strike_distance", 3.0)
    assert fake_client.published[-1].payload == "3.0"


def test_publish_state_without_attrs_does_not_touch_attr_topic(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.publish_state("active_alerts", 2)
    assert all(not p.topic.startswith(ATTR_TOPIC_PREFIX) for p in fake_client.published)


def test_publish_state_with_attrs_publishes_retained_json_attr_topic(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.publish_state(
        "highest_alert", "Tornado Warning", attrs={"headline": "h", "description": "d"}
    )

    attr_msgs = [
        p for p in fake_client.published if p.topic == f"{ATTR_TOPIC_PREFIX}/highest_alert"
    ]
    assert len(attr_msgs) == 1
    assert attr_msgs[0].retain is True
    assert json.loads(attr_msgs[0].payload) == {"headline": "h", "description": "d"}


# --- publish_event() ---------------------------------------------------------


def test_publish_event_not_retained(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.publish_event("alert_issued", {"headline": "Tornado Warning"})

    msg = fake_client.published[-1]
    assert msg.topic == f"{EVENT_TOPIC_PREFIX}/alert_issued"
    assert msg.retain is False
    assert json.loads(msg.payload) == {"headline": "Tornado Warning"}


# --- offline() ----------------------------------------------------------------


def test_offline_publishes_retained_offline(config, fake_client) -> None:
    pub = Publisher(config, client=fake_client)
    pub.offline()

    msg = fake_client.published[-1]
    assert msg.topic == AVAILABILITY_TOPIC
    assert msg.payload == "offline"
    assert msg.retain is True
    assert msg.qos == 1
