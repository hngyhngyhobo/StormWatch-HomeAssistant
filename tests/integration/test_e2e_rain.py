"""End-to-end integration test: the real Supervisor (config -> RainSource ->
RainStore -> RainWiring -> MQTT publish) against a local Mosquitto broker and
a stubbed NWS HTTP server (task D2).

Extends tests/integration/test_e2e_alerts.py's stub-NWS-server approach: the
same kind of stdlib http.server thread now also serves the four rain
fixtures (tests/fixtures/nws_points.json, nws_gridpoint.json,
nws_stations.json, nws_observations.json) at their real api.weather.gov
paths, alongside nws_alerts_active.json at /alerts/active (NWS_ENABLED=true
too, per the task brief -- "reuse alerts fixture server"). Never touches the
real api.weather.gov or blitzortung.ha.sed.pl; lightning is disabled
(BLITZORTUNG_ENABLED=false) since this test only exercises rain + NWS.

nws_points.json's forecastGridData/observationStations fields are absolute
"https://api.weather.gov/..." URLs in the fixture as committed -- RainSource
follows those literally once discovered, so the stub handler rewrites them
to point back at itself (http://127.0.0.1:<stub port>/...) before serving
the points response; otherwise a real RainSource would attempt to hit the
actual internet for the gridpoint/stations follow-up requests.

nws_stations.json lists two stations: KKFFC2 ("no precip sensor" per its own
comment) before KFFC. The stub deliberately serves an empty observations
feature list for KKFFC2 so RainSource's real station-discovery loop
(sources/rain.py's _discover_station) skips it and lands on KFFC, matching
the fixture's intent.

RainSource's forecast clock is injected as a fixed UTC instant
(2026-08-09T18:00:00Z) rather than the real wall clock, so
rain_forecast_today/rain_forecast_48h are fully deterministic regardless of
when this test actually runs -- verified against sources/rain.py's own
_sum_overlap logic against the gridpoint fixture: today_mm=7.2 (UTC-day
window, independent of time-of-day) and h48_mm=14.75 at that instant, i.e.
"0.28"/"0.58" in (imperial, 2dp). RainStore's observation totals
(rain_last_24h/rain_last_7d), by contrast, are computed against the real
wall clock (Supervisor._rain_obs_tick always passes _utc_now()) against the
fixture's fixed historical timestamps (2026-08-07..09) -- deterministic only
as long as "now" stays within roughly a week of those dates, which isn't
guaranteed indefinitely, so this test intentionally does not pin an exact
number for them; it only asserts they arrive with a real (non-"None"),
correctly-formatted value and that the hourly attrs came through.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

from stormwatch.__main__ import Supervisor
from stormwatch.config import Config
from stormwatch.publisher import AVAILABILITY_TOPIC
from stormwatch.sources.rain import RainSource, RainStore

MQTT_HOST = os.environ.get("MQTT_TEST_HOST", "localhost")
MQTT_PORT = 1883
_DEADLINE_S = 10.0
_POLL_S = 0.1

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_ALERTS_FIXTURE = _FIXTURES_DIR / "nws_alerts_active.json"
_POINTS_FIXTURE = _FIXTURES_DIR / "nws_points.json"
_GRIDPOINT_FIXTURE = _FIXTURES_DIR / "nws_gridpoint.json"
_STATIONS_FIXTURE = _FIXTURES_DIR / "nws_stations.json"
_OBSERVATIONS_FIXTURE = _FIXTURES_DIR / "nws_observations.json"

# Instant the RainSource forecast clock is fixed at for this test -- see the
# module docstring for the fixture-math derivation of the expected values.
_FIXED_FORECAST_NOW = datetime(2026, 8, 9, 18, 0, 0, tzinfo=UTC)
_EXPECTED_FORECAST_TODAY_IN = "0.28"
_EXPECTED_FORECAST_48H_IN = "0.58"

_NUMERIC_RE = re.compile(r"^\d+\.\d{2}$")  # imperial, 2dp


def _wait_until(predicate: Callable[[], bool], deadline_s: float = _DEADLINE_S) -> bool:
    """Poll predicate() until True or the deadline passes (no bare long sleeps)."""
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        if predicate():
            return True
        time.sleep(_POLL_S)
    return predicate()


class _StubNwsHandler(BaseHTTPRequestHandler):
    """Serves NWS alerts + all four rain fixtures for the E2E rain flow."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path.startswith("/alerts/active"):
            self._serve_file(_ALERTS_FIXTURE)
        elif path.startswith("/points/"):
            self._serve_points()
        elif path == "/gridpoints/FFC/60,66":
            self._serve_file(_GRIDPOINT_FIXTURE)
        elif path == "/gridpoints/FFC/60,66/stations":
            self._serve_file(_STATIONS_FIXTURE)
        elif path == "/stations/KFFC/observations":
            self._serve_file(_OBSERVATIONS_FIXTURE)
        elif path == "/stations/KKFFC2/observations":
            # "no precip sensor" per nws_stations.json's own comment -- an
            # empty feature list makes RainSource's station-discovery loop
            # skip straight past it to KFFC.
            self._serve_json({"features": []})
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_points(self) -> None:
        # Rewrite the fixture's real api.weather.gov URLs to point back at
        # this stub server -- see the module docstring.
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        data = json.loads(_POINTS_FIXTURE.read_text(encoding="utf-8"))
        data["properties"]["forecastGridData"] = f"{base}/gridpoints/FFC/60,66"
        data["properties"]["observationStations"] = f"{base}/gridpoints/FFC/60,66/stations"
        self._serve_json(data)

    def _serve_file(self, path: Path) -> None:
        self._respond(path.read_bytes())

    def _serve_json(self, data: object) -> None:
        self._respond(json.dumps(data).encode("utf-8"))

    def _respond(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/geo+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep test output clean


class Observer:
    """Subscribes to stormwatch/# and homeassistant/# and keeps the latest
    payload seen per topic (mirrors tests/integration/test_e2e_alerts.py)."""

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
    thread = threading.Thread(target=server.serve_forever, name="stub-nws-rain", daemon=True)
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
def test_supervisor_e2e_polls_rain_and_publishes_all_four_sensors(
    stub_nws_server, observer, tmp_path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    port = stub_nws_server.server_address[1]
    device = f"StormWatch-E2E-Rain-{uuid.uuid4().hex[:8]}"
    config = Config(
        latitude=34.0234,
        longitude=-84.6155,
        mqtt_host=MQTT_HOST,
        mqtt_port=MQTT_PORT,
        nws_contact="you@example.com",
        nws_api_base=f"http://127.0.0.1:{port}",
        nws_enabled=True,
        nws_poll_seconds=30,  # floor; first poll fires immediately on start()
        rain_enabled=True,
        # Large enough that a second poll cycle never fires within this
        # test's run -- only the immediate first cycle at thread start.
        rain_obs_poll_seconds=100_000,
        rain_forecast_poll_seconds=100_000,
        blitzortung_enabled=False,
        units="imperial",
        config_dir=str(config_dir),
        device_name=device,
    )

    supervisor = Supervisor(config)
    supervisor.rain_source = RainSource(config, clock=lambda: _FIXED_FORECAST_NOW)
    supervisor.rain_store = RainStore(str(config_dir / "rain_history.json"))

    try:
        supervisor.start()

        assert _wait_until(lambda: observer.messages.get(AVAILABILITY_TOPIC) == "online"), (
            "publisher never came online"
        )

        slug = supervisor.publisher.slug
        rain_discovery_topics = [
            f"homeassistant/{component}/{slug}/{slug}_{key}/config"
            for key, component in [
                ("rain_forecast_today", "sensor"),
                ("rain_forecast_48h", "sensor"),
                ("rain_last_24h", "sensor"),
                ("rain_last_7d", "sensor"),
                ("rain_available", "binary_sensor"),
            ]
        ]
        assert _wait_until(lambda: all(t in observer.messages for t in rain_discovery_topics)), (
            "not all rain discovery configs arrived"
        )

        # Deterministic (fixed forecast clock) -- see the module docstring.
        assert _wait_until(
            lambda: (
                observer.messages.get("stormwatch/state/rain_forecast_today")
                == _EXPECTED_FORECAST_TODAY_IN
            )
        ), "rain_forecast_today never reached the expected fixture-derived value"
        assert _wait_until(
            lambda: (
                observer.messages.get("stormwatch/state/rain_forecast_48h")
                == _EXPECTED_FORECAST_48H_IN
            )
        ), "rain_forecast_48h never reached the expected fixture-derived value"

        # Wall-clock dependent (see module docstring) -- assert arrival and
        # well-formedness, not an exact pinned number.
        assert _wait_until(
            lambda: (
                _NUMERIC_RE.match(observer.messages.get("stormwatch/state/rain_last_24h", ""))
                is not None
            )
        ), "rain_last_24h never published a real (non-None) value"
        assert _wait_until(
            lambda: (
                _NUMERIC_RE.match(observer.messages.get("stormwatch/state/rain_last_7d", ""))
                is not None
            )
        ), "rain_last_7d never published a real (non-None) value"

        hourly_attrs = json.loads(observer.messages["stormwatch/attr/rain_last_24h"])
        assert hourly_attrs, "rain_last_24h attrs (hourly buckets) were empty"

        assert _wait_until(
            lambda: observer.messages.get("stormwatch/state/rain_available") == "ON"
        ), "rain_available never went ON"

        def _health_ok() -> dict | None:
            import urllib.request

            try:
                with urllib.request.urlopen("http://127.0.0.1:8099/healthz", timeout=2) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except OSError:
                return None

        health = None

        def _health_reports_rain_available() -> bool:
            nonlocal health
            health = _health_ok()
            return bool(health and health.get("sources", {}).get("rain", {}).get("available"))

        assert _wait_until(_health_reports_rain_available), (
            "healthz never reported sources.rain.available = true"
        )
        assert health["sources"]["rain"] == {"available": True}
        assert isinstance(health["state"]["rain_last_24h"], int | float)
    finally:
        supervisor.stop()

    assert _wait_until(lambda: observer.messages.get(AVAILABILITY_TOPIC) == "offline"), (
        "clean shutdown never published offline"
    )
