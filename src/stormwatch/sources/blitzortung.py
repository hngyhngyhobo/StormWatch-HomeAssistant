"""Blitzortung community MQTT client (DESIGN.md §4.1).

Geohash-partitioned subscription; distance/bearing computed client-side.
Exponential backoff, never reconnect in a tight loop — volunteer-run broker.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from stormwatch.config import Config
from stormwatch.geo import cells_for_radius, distance_and_bearing

logger = logging.getLogger("stormwatch.sources.blitzortung")

_TOPIC_PREFIX = "blitzortung/1.1"
_RECONNECT_MIN_DELAY_SECONDS = 1
_RECONNECT_MAX_DELAY_SECONDS = 300
_KEEPALIVE_SECONDS = 60
_NANOSECONDS_PER_SECOND = 1e9


class BlitzortungClient:
    """Persistent MQTT subscription to geohash-scoped strike topics.

    Owns its own paho client, separate from Publisher's connection to the
    user's broker (DESIGN.md §4.1.1: this is a courtesy connection to a
    volunteer-run broker and must never share state or reconnect
    aggressively with the user's own MQTT traffic).
    """

    def __init__(
        self,
        config: Config,
        on_strike: Callable[[float, float, float, float, float], None],
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._on_strike = on_strike
        self._connected = False
        self.client = client if client is not None else self._build_client()

    @property
    def available(self) -> bool:
        """True while connected to the Blitzortung broker (connection-based,
        not strike-recency)."""
        return self._connected

    def _build_client(self) -> mqtt.Client:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="", clean_session=True)

    def start(self) -> None:
        """Connect and subscribe to the geohash cells covering the watch radius.

        Subscriptions are (re)issued from on_connect so a clean_session
        client re-subscribes automatically after paho's backed-off
        reconnect. reconnect_delay_set(1, 300) is paho's built-in
        exponential backoff between reconnect attempts -- required so a
        broker outage never turns into a tight reconnect loop against
        someone else's volunteer infrastructure.
        """
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(
            min_delay=_RECONNECT_MIN_DELAY_SECONDS, max_delay=_RECONNECT_MAX_DELAY_SECONDS
        )
        self.client.connect(
            self._config.blitzortung_mqtt_host,
            self._config.blitzortung_mqtt_port,
            keepalive=_KEEPALIVE_SECONDS,
        )
        self.client.loop_start()

    def stop(self) -> None:
        """Disconnect from the broker and stop the network loop thread."""
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(
        self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        if reason_code != 0:
            logger.error("Blitzortung MQTT connect failed: reason_code=%s", reason_code)
            return
        self._connected = True
        logger.info(
            "Connected to Blitzortung broker %s:%s",
            self._config.blitzortung_mqtt_host,
            self._config.blitzortung_mqtt_port,
        )
        cells = cells_for_radius(
            self._config.latitude, self._config.longitude, self._config.watch_radius_km
        )
        for cell in cells:
            topic = f"{_TOPIC_PREFIX}/{'/'.join(cell)}/#"
            self.client.subscribe(topic)

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        self._connected = False
        logger.warning("Blitzortung MQTT connection lost (rc=%s)", reason_code)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        try:
            data = json.loads(message.payload)
        except (ValueError, TypeError) as exc:
            logger.debug("Ignoring malformed Blitzortung payload (bad JSON): %s", exc)
            return

        try:
            lat = float(data["lat"])
            lon = float(data["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("Ignoring malformed Blitzortung payload (bad lat/lon): %s", exc)
            return

        age_s = self._age_seconds(data.get("time"))
        distance_km, bearing_deg = distance_and_bearing(
            self._config.latitude, self._config.longitude, lat, lon
        )
        self._on_strike(distance_km, bearing_deg, age_s, lat, lon)

    @staticmethod
    def _age_seconds(raw_time_ns: Any) -> float:
        """Strike payload ``time`` is nanoseconds since epoch; missing or
        unparseable values default to age_s=0 rather than dropping the strike."""
        if raw_time_ns is None:
            return 0.0
        try:
            strike_epoch_s = float(raw_time_ns) / _NANOSECONDS_PER_SECOND
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, time.time() - strike_epoch_s)
