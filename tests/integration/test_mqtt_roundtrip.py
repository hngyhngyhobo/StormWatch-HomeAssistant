"""Integration tests: Publisher against a real local Mosquitto broker.

Requires a broker reachable at MQTT_TEST_HOST (default "localhost") on port
1883 with anonymous auth allowed. Local broker for these tests:

    docker run -d --name sw-mosq -p 1883:1883 eclipse-mosquitto:2 sh -c \
      "echo 'listener 1883' > /m.conf; echo 'allow_anonymous true' >> /m.conf; mosquitto -c /m.conf"

Never touches api.weather.gov or blitzortung.ha.sed.pl.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable

import paho.mqtt.client as mqtt
import pytest

from stormwatch.config import Config
from stormwatch.publisher import AVAILABILITY_TOPIC, EntitySpec, Publisher

MQTT_HOST = os.environ.get("MQTT_TEST_HOST", "localhost")
MQTT_PORT = 1883
_DEADLINE_S = 10.0
_POLL_S = 0.1


def _wait_until(predicate: Callable[[], bool], deadline_s: float = _DEADLINE_S) -> bool:
    """Poll predicate() until True or the deadline passes (no bare long sleeps)."""
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        if predicate():
            return True
        time.sleep(_POLL_S)
    return predicate()


class Observer:
    """Subscribes to stormwatch/# and keeps the latest payload seen per topic."""

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
        # stormwatch/# carries state/attr/event/availability; discovery configs
        # live under the (separately configurable) discovery prefix, default
        # "homeassistant/#" - subscribe to both trees.
        client.subscribe("stormwatch/#", qos=1)
        client.subscribe("homeassistant/#", qos=1)
        self.subscribed = True

    def _on_message(self, client, userdata, msg) -> None:
        self.messages[msg.topic] = msg.payload.decode("utf-8")

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture
def config() -> Config:
    # Unique device name per test run so discovery topics don't collide with
    # retained configs left over from a previous test run.
    device = f"StormWatch-Test-{uuid.uuid4().hex[:8]}"
    return Config(
        latitude=35.2,
        longitude=-80.8,
        mqtt_host=MQTT_HOST,
        mqtt_port=MQTT_PORT,
        device_name=device,
    )


@pytest.fixture
def observer():
    obs = Observer(MQTT_HOST, MQTT_PORT)
    assert _wait_until(lambda: obs.subscribed), "observer failed to subscribe within deadline"
    yield obs
    obs.close()


@pytest.mark.integration
def test_discovery_state_event_round_trip(config, observer) -> None:
    entity = EntitySpec(
        key="active_alerts",
        name="Active Alerts",
        component="sensor",
        state_class="measurement",
        value_is_json_attr=True,
    )
    pub = Publisher(config)
    try:
        pub.connect()
        assert _wait_until(lambda: observer.messages.get(AVAILABILITY_TOPIC) == "online"), (
            "publisher never came online"
        )

        pub.publish_discovery([entity])
        pub.publish_state("active_alerts", 2, attrs={"alerts": ["Tornado Warning"]})
        pub.publish_event("alert_issued", {"headline": "Tornado Warning"})

        discovery_topic = f"homeassistant/sensor/{pub.slug}/{pub.slug}_active_alerts/config"
        assert _wait_until(lambda: discovery_topic in observer.messages), (
            "discovery config never arrived"
        )
        body = json.loads(observer.messages[discovery_topic])
        assert body["unique_id"] == f"{pub.slug}_active_alerts"
        assert body["state_topic"] == "stormwatch/state/active_alerts"
        assert body["json_attributes_topic"] == "stormwatch/attr/active_alerts"
        assert body["availability_topic"] == AVAILABILITY_TOPIC
        assert body["device"]["identifiers"] == [pub.slug]

        assert _wait_until(
            lambda: observer.messages.get("stormwatch/state/active_alerts") == "2"
        ), "state payload never arrived"
        assert _wait_until(
            lambda: (
                observer.messages.get("stormwatch/attr/active_alerts")
                == json.dumps({"alerts": ["Tornado Warning"]})
            )
        ), "attr payload never arrived"
        assert _wait_until(lambda: "stormwatch/event/alert_issued" in observer.messages), (
            "event payload never arrived"
        )
        assert json.loads(observer.messages["stormwatch/event/alert_issued"]) == {
            "headline": "Tornado Warning"
        }

        pub.offline()
        assert _wait_until(lambda: observer.messages.get(AVAILABILITY_TOPIC) == "offline"), (
            "explicit offline() was not observed"
        )
    finally:
        pub.client.loop_stop()
        pub.client.disconnect()


@pytest.mark.integration
def test_lwt_fires_offline_on_ungraceful_disconnect(config, observer) -> None:
    """Kill the publisher's raw TCP socket (no MQTT DISCONNECT packet first) so
    the broker treats the session as abnormally terminated and fires the Will.

    Technique: pub.client._sock.close() closes the socket directly, bypassing
    paho's disconnect() (which sends a clean DISCONNECT and would suppress the
    Will). Mosquitto reacts to the resulting connection loss immediately -
    it does not wait out the MQTT keepalive interval - so this is fast and
    deterministic enough for a bounded poll loop.
    """
    pub = Publisher(config)
    pub.connect()
    assert _wait_until(lambda: observer.messages.get(AVAILABILITY_TOPIC) == "online"), (
        "publisher never came online"
    )

    sock = pub.client._sock
    assert sock is not None, "publisher has no live socket to kill"
    sock.close()

    try:
        assert _wait_until(lambda: observer.messages.get(AVAILABILITY_TOPIC) == "offline"), (
            "LWT offline was not observed after ungraceful disconnect"
        )
    finally:
        pub.client.loop_stop()
