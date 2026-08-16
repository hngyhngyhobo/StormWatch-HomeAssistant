"""MQTT publisher: Home Assistant discovery configs, state topics, event
topics, and LWT-backed availability (DESIGN.md §5, §8).

All entities appear under a single StormWatch device via MQTT discovery.
Topics (fixed, independent of DISCOVERY_PREFIX/DEVICE_NAME):
    stormwatch/state/<key>        retained state value
    stormwatch/attr/<key>         retained JSON attributes
    stormwatch/event/<name>       not retained, one-shot event payload
    stormwatch/availability       retained "online"/"offline", LWT-backed

Discovery configs are published under
    {discovery_prefix}/{component}/{slug}/{slug}_{key}/config
where slug = DEVICE_NAME lowercased with every non-alphanumeric character
replaced by "_".
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import paho.mqtt.client as mqtt

from stormwatch import __version__

if TYPE_CHECKING:
    from stormwatch.config import Config

logger = logging.getLogger("stormwatch.publisher")

AVAILABILITY_TOPIC = "stormwatch/availability"
STATE_TOPIC_PREFIX = "stormwatch/state"
ATTR_TOPIC_PREFIX = "stormwatch/attr"
EVENT_TOPIC_PREFIX = "stormwatch/event"

_PAYLOAD_ONLINE = "online"
_PAYLOAD_OFFLINE = "offline"
_MANUFACTURER = "StormWatch"
_PUBLISH_QOS = 1


@dataclass(frozen=True)
class EntitySpec:
    """Describes one Home Assistant entity published via MQTT discovery."""

    key: str
    name: str
    component: str  # 'sensor' | 'binary_sensor'
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    value_is_json_attr: bool = False


def _slugify(name: str) -> str:
    """DEVICE_NAME lowercased, non-alphanumeric characters replaced with '_'."""
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower())


def _format_value(value: Any) -> str:
    """Render a Python value as the plain-text MQTT state payload.

    bool -> "ON"/"OFF" (HA binary_sensor default payload); float -> 1 decimal
    place (distances etc.); everything else -> str().
    """
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        return f"{round(value, 1):.1f}"
    return str(value)


class Publisher:
    """Owns the MQTT connection to the user's broker."""

    def __init__(self, config: Config, client: Any | None = None) -> None:
        self.config = config
        self.slug = _slugify(config.device_name)
        self._entities: dict[str, EntitySpec] = {}
        self._entities_lock = threading.Lock()
        self._connected = False
        self.client = client if client is not None else self._build_client()

    @property
    def connected(self) -> bool:
        """True once the broker has accepted this session (on_connect,
        reason_code 0); False before connecting and after a disconnect."""
        return self._connected

    def _build_client(self) -> mqtt.Client:
        return mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="",
            clean_session=True,
            reconnect_on_failure=False,
        )

    def connect(self) -> None:
        """Connect with Last-Will so HA sees availability honestly."""
        if self.config.mqtt_username:
            self.client.username_pw_set(self.config.mqtt_username, self.config.mqtt_password)
        self.client.will_set(
            AVAILABILITY_TOPIC, payload=_PAYLOAD_OFFLINE, qos=_PUBLISH_QOS, retain=True
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        self.client.loop_start()

    def _on_connect(
        self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        if reason_code != 0:
            logger.error("MQTT connect failed: reason_code=%s", reason_code)
            return
        self._connected = True
        logger.info("Connected to MQTT broker %s:%s", self.config.mqtt_host, self.config.mqtt_port)
        self.client.publish(
            AVAILABILITY_TOPIC, payload=_PAYLOAD_ONLINE, qos=_PUBLISH_QOS, retain=True
        )
        with self._entities_lock:
            entities = list(self._entities.values())
        for entity in entities:
            self._publish_entity_discovery(entity)

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        self._connected = False
        logger.warning("MQTT connection lost (rc=%s)", reason_code)

    def publish_discovery(self, entities: list[EntitySpec]) -> None:
        """Publish HA MQTT discovery configs for all entities.

        Entities are also remembered so they are republished automatically
        whenever the connection is (re)established (see _on_connect).
        """
        with self._entities_lock:
            for entity in entities:
                self._entities[entity.key] = entity
        for entity in entities:
            self._publish_entity_discovery(entity)

    def _publish_entity_discovery(self, entity: EntitySpec) -> None:
        topic = (
            f"{self.config.discovery_prefix}/{entity.component}/{self.slug}/"
            f"{self.slug}_{entity.key}/config"
        )
        payload = self._build_discovery_payload(entity)
        self.client.publish(topic, payload=json.dumps(payload), qos=_PUBLISH_QOS, retain=True)

    def _build_discovery_payload(self, entity: EntitySpec) -> dict:
        payload: dict[str, Any] = {
            "name": entity.name,
            "unique_id": f"{self.slug}_{entity.key}",
            "state_topic": f"{STATE_TOPIC_PREFIX}/{entity.key}",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": _PAYLOAD_ONLINE,
            "payload_not_available": _PAYLOAD_OFFLINE,
            "device": {
                "identifiers": [self.slug],
                "name": self.config.device_name,
                "manufacturer": _MANUFACTURER,
                "sw_version": __version__,
            },
        }
        if entity.device_class is not None:
            payload["device_class"] = entity.device_class
        if entity.unit is not None:
            payload["unit_of_measurement"] = entity.unit
        if entity.state_class is not None:
            payload["state_class"] = entity.state_class
        if entity.icon is not None:
            payload["icon"] = entity.icon
        if entity.entity_category is not None:
            payload["entity_category"] = entity.entity_category
        if entity.value_is_json_attr:
            payload["json_attributes_topic"] = f"{ATTR_TOPIC_PREFIX}/{entity.key}"
        return payload

    def publish_state(self, key: str, value: Any, attrs: dict | None = None) -> None:
        """Publish retained state to a stormwatch/state/<key> topic.

        When attrs is given, also publishes it (as JSON) retained to
        stormwatch/attr/<key>.
        """
        self.client.publish(
            f"{STATE_TOPIC_PREFIX}/{key}",
            payload=_format_value(value),
            qos=_PUBLISH_QOS,
            retain=True,
        )
        if attrs is not None:
            self.client.publish(
                f"{ATTR_TOPIC_PREFIX}/{key}",
                payload=json.dumps(attrs),
                qos=_PUBLISH_QOS,
                retain=True,
            )

    def publish_event(self, name: str, payload: dict) -> None:
        """Publish a one-shot event (alert_issued, all_clear, ...); not retained."""
        self.client.publish(
            f"{EVENT_TOPIC_PREFIX}/{name}",
            payload=json.dumps(payload),
            qos=_PUBLISH_QOS,
            retain=False,
        )

    def offline(self) -> None:
        """Proactively mark availability offline (graceful shutdown path).

        A clean client.disconnect() suppresses the broker's Last-Will, so a
        graceful shutdown must publish "offline" itself before disconnecting.
        """
        self.client.publish(
            AVAILABILITY_TOPIC, payload=_PAYLOAD_OFFLINE, qos=_PUBLISH_QOS, retain=True
        )
