"""End-to-end integration test: the real Supervisor (config -> NWS poll ->
rule engine -> AlertTracker -> MQTT publish) against a local Mosquitto
broker and a stubbed NWS HTTP server.

Requires a broker reachable at MQTT_TEST_HOST (default "localhost") on port
1883 with anonymous auth allowed - see tests/integration/test_mqtt_roundtrip.py
for the docker run command. Never touches the real api.weather.gov; a
stdlib http.server thread serves tests/fixtures/nws_alerts_active.json at
/alerts/active instead, and Config.nws_api_base points at it.

The fixture has two active alerts: a "Severe Thunderstorm Warning" (matches
the default ALERTS_HIGH env rule and this test's alerts.yaml) and a "Winter
Storm Warning". To get a deterministic single alert_issued event, this test
pre-seeds config_dir with an alerts.yaml (DESIGN.md §7.1's own illustrative
example, winter rule left `enabled: false`) *before* starting the
Supervisor - Supervisor only auto-generates a default alerts.yaml when the
file is absent, so this hand-written one is loaded as-is. Under it, the
winter storm alert is deliberately unmatched (ignored): it still counts
toward the raw `active_alerts` total (both alerts are "active near me"
regardless of configured priority) but produces no event.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

from stormwatch.__main__ import Supervisor
from stormwatch.config import Config
from stormwatch.publisher import AVAILABILITY_TOPIC

MQTT_HOST = os.environ.get("MQTT_TEST_HOST", "localhost")
MQTT_PORT = 1883
_DEADLINE_S = 10.0
_POLL_S = 0.1

_FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "nws_alerts_active.json"

_ALERTS_YAML = """\
version: 1

defaults:
  priority: ignore
  min_severity: Moderate

rules:
  - name: Tornado Warning
    match:
      event: ["Tornado Warning"]
    priority: critical
    include_description: true

  - name: Severe Thunderstorm Warning
    match:
      event: ["Severe Thunderstorm Warning"]
      urgency: ["Immediate", "Expected"]
    priority: high

  - name: Watches
    match:
      event_regex: ".*Watch$"
    priority: normal
    quiet_hours: true

  - name: Winter weather
    match:
      event: ["Winter Storm Warning", "Ice Storm Warning", "Blizzard Warning"]
    priority: high
    enabled: false
"""


def _wait_until(predicate: Callable[[], bool], deadline_s: float = _DEADLINE_S) -> bool:
    """Poll predicate() until True or the deadline passes (no bare long sleeps)."""
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        if predicate():
            return True
        time.sleep(_POLL_S)
    return predicate()


class _StubNwsHandler(BaseHTTPRequestHandler):
    """Serves the fixture GeoJSON for any /alerts/active?... request."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self.path.startswith("/alerts/active"):
            self.send_response(404)
            self.end_headers()
            return
        body = _FIXTURE_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/geo+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep test output clean


class Observer:
    """Subscribes to stormwatch/# and homeassistant/# and keeps the latest
    payload seen per topic (mirrors tests/integration/test_mqtt_roundtrip.py)."""

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

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture
def stub_nws_server():
    server = HTTPServer(("127.0.0.1", 0), _StubNwsHandler)
    thread = threading.Thread(target=server.serve_forever, name="stub-nws", daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def observer():
    obs = Observer(MQTT_HOST, MQTT_PORT)
    assert _wait_until(lambda: obs.subscribed), "observer failed to subscribe within deadline"
    yield obs
    obs.close()


@pytest.mark.integration
def test_supervisor_e2e_polls_nws_and_publishes_alerts(stub_nws_server, observer, tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "alerts.yaml").write_text(_ALERTS_YAML, encoding="utf-8")

    port = stub_nws_server.server_address[1]
    device = f"StormWatch-E2E-{uuid.uuid4().hex[:8]}"
    config = Config(
        latitude=34.0234,
        longitude=-84.6155,
        mqtt_host=MQTT_HOST,
        mqtt_port=MQTT_PORT,
        nws_contact="you@example.com",
        nws_api_base=f"http://127.0.0.1:{port}",
        nws_poll_seconds=30,  # floor; first poll fires immediately on start()
        config_dir=str(config_dir),
        device_name=device,
    )

    supervisor = Supervisor(config)
    try:
        supervisor.start()

        assert _wait_until(lambda: observer.messages.get(AVAILABILITY_TOPIC) == "online"), (
            "publisher never came online"
        )

        slug = supervisor.publisher.slug
        discovery_topic = f"homeassistant/sensor/{slug}/{slug}_active_alerts/config"
        assert _wait_until(lambda: discovery_topic in observer.messages), (
            "discovery config for active_alerts never arrived"
        )
        discovery_body = json.loads(observer.messages[discovery_topic])
        assert discovery_body["unique_id"] == f"{slug}_active_alerts"
        assert discovery_body["availability_topic"] == AVAILABILITY_TOPIC

        assert _wait_until(
            lambda: observer.messages.get("stormwatch/state/active_alerts") == "2"
        ), "active_alerts never reached 2 (raw NWS alert count for the point)"

        assert _wait_until(lambda: "stormwatch/event/alert_issued" in observer.messages), (
            "no alert_issued event observed"
        )
        issued = json.loads(observer.messages["stormwatch/event/alert_issued"])
        assert issued["event"] == "Severe Thunderstorm Warning"
        assert issued["priority"] == "high"

        assert _wait_until(
            lambda: (
                observer.messages.get("stormwatch/state/highest_alert")
                == "Severe Thunderstorm Warning"
            )
        )
        assert _wait_until(lambda: observer.messages.get("stormwatch/state/nws_available") == "ON")
        assert _wait_until(
            lambda: observer.messages.get("stormwatch/state/config_problem") == "OFF"
        )

        # No cleared events should have fired for a first-ever poll.
        assert "stormwatch/event/alert_cleared" not in observer.messages
    finally:
        supervisor.stop()

    assert _wait_until(lambda: observer.messages.get(AVAILABILITY_TOPIC) == "offline"), (
        "clean shutdown never published offline"
    )
