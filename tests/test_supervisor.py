"""Tests for stormwatch.__main__ (supervisor) and stormwatch.health.

Covers: the pure(-ish) per-poll cycle (``run_alert_cycle``) with a fake
poller/publisher and the real rule engine/tracker; the MQTT reconnect step
(``_maybe_reconnect``); the alerts.yaml generate/hot-reload helpers; the
exact startup log line contract; and the /healthz endpoint over a real loop-
back HTTP GET on an ephemeral port. No real MQTT broker and no network
beyond that loopback health-server socket — see tests/integration/
test_e2e_alerts.py for the full stack against local Mosquitto.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from stormwatch import __version__
from stormwatch.__main__ import (
    _ALWAYS_ENTITIES,
    _ENTITIES,
    Supervisor,
    _handle_shutdown_signal,
    _maybe_reconnect,
    main,
    run_alert_cycle,
)
from stormwatch.config import Config, ConfigError
from stormwatch.health import start_health_server
from stormwatch.rules import AlertTracker, RuleEngine

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nws_alerts_active.json"
NOON = datetime(2026, 8, 9, 12, 0, 0)


def _fixture_features() -> list[dict]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))["features"]


def _config(**overrides: object) -> Config:
    base: dict[str, object] = dict(
        latitude=34.0234,
        longitude=-84.6155,
        mqtt_host="192.168.1.10",
        nws_contact="you@example.com",
    )
    base.update(overrides)
    return Config(**base)


class FakePoller:
    """Duck-typed stand-in for NwsPoller — no network."""

    def __init__(self, features: list[dict] | None, available: bool = True) -> None:
        self._features = features
        self.available = available
        self.calls = 0

    def poll_once(self) -> list[dict] | None:
        self.calls += 1
        return self._features


class FakePublisher:
    """Duck-typed stand-in for Publisher — records calls, no network."""

    def __init__(self, connected: bool = False) -> None:
        self.connected = connected
        self.states: list[tuple[str, object, dict | None]] = []
        self.events: list[tuple[str, dict]] = []
        self.discovery: list[object] = []
        self.connect_calls = 0
        self.offline_calls = 0

    def publish_discovery(self, entities) -> None:
        self.discovery.extend(entities)

    def publish_state(self, key, value, attrs=None) -> None:
        self.states.append((key, value, attrs))

    def publish_event(self, name, payload) -> None:
        self.events.append((name, payload))

    def connect(self) -> None:
        self.connect_calls += 1

    def offline(self) -> None:
        self.offline_calls += 1

    def state_by_key(self) -> dict[str, tuple[object, dict | None]]:
        return {key: (value, attrs) for key, value, attrs in self.states}


# --- run_alert_cycle ---------------------------------------------------------


def test_run_alert_cycle_publishes_raw_active_count_and_matched_highest() -> None:
    # Default env rules: ALERTS_HIGH includes "Severe Thunderstorm Warning"
    # but nothing matches "Winter Storm Warning" - it's still counted in the
    # raw active_alerts total, just excluded from highest/critical.
    config = _config()
    engine = RuleEngine(config)
    tracker = AlertTracker()
    poller = FakePoller(_fixture_features())
    publisher = FakePublisher()

    result = run_alert_cycle(poller, tracker, engine, publisher, NOON)

    assert result == 2
    states = publisher.state_by_key()
    assert states["active_alerts"][0] == 2
    assert len(states["active_alerts"][1]["alerts"]) == 2
    assert states["highest_alert"][0] == "Severe Thunderstorm Warning"
    assert states["highest_alert"][1]["headline"].startswith("Severe Thunderstorm Warning")
    assert states["critical_alert"][0] is False
    assert states["nws_available"][0] is True
    assert states["config_problem"] == (False, {"last_error": None})


def test_run_alert_cycle_active_alerts_attrs_include_richer_nws_fields() -> None:
    # task LOC feature B: each active_alerts attrs entry carries id/event/
    # headline/severity/urgency/certainty/expires straight from NWS
    # properties (fixture has both alerts populated for all of them).
    config = _config()
    engine = RuleEngine(config)
    tracker = AlertTracker()
    poller = FakePoller(_fixture_features())
    publisher = FakePublisher()

    run_alert_cycle(poller, tracker, engine, publisher, NOON)

    alerts = publisher.state_by_key()["active_alerts"][1]["alerts"]
    assert len(alerts) == 2
    thunderstorm = next(a for a in alerts if a["event"] == "Severe Thunderstorm Warning")
    assert thunderstorm["id"] == "urn:oid:2.49.0.1.840.0.a1b2c3d4e5f6.2026.08.09.16.00.00.001"
    assert thunderstorm["headline"].startswith("Severe Thunderstorm Warning")
    assert thunderstorm["severity"] == "Severe"
    assert thunderstorm["urgency"] == "Immediate"
    assert thunderstorm["certainty"] == "Observed"
    assert thunderstorm["expires"] == "2026-08-09T16:45:00-04:00"

    winter = next(a for a in alerts if a["event"] == "Winter Storm Warning")
    assert winter["urgency"] == "Expected"
    assert winter["certainty"] == "Likely"
    assert winter["expires"] == "2026-01-16T12:00:00-05:00"


def test_run_alert_cycle_active_alerts_attrs_blank_when_fields_absent() -> None:
    config = _config()
    engine = RuleEngine(config)
    tracker = AlertTracker()
    minimal_alert = {
        "properties": {"id": "A1", "event": "Tornado Warning"}
        # severity/urgency/certainty/headline/expires deliberately absent
    }
    poller = FakePoller([minimal_alert])
    publisher = FakePublisher()

    run_alert_cycle(poller, tracker, engine, publisher, NOON)

    alerts = publisher.state_by_key()["active_alerts"][1]["alerts"]
    assert alerts == [
        {
            "id": "A1",
            "event": "Tornado Warning",
            "headline": "",
            "severity": "",
            "urgency": "",
            "certainty": "",
            "expires": "",
        }
    ]


def test_run_alert_cycle_emits_alert_issued_event_for_matched_alert_only() -> None:
    config = _config()
    engine = RuleEngine(config)
    tracker = AlertTracker()
    poller = FakePoller(_fixture_features())
    publisher = FakePublisher()

    run_alert_cycle(poller, tracker, engine, publisher, NOON)

    assert len(publisher.events) == 1
    name, payload = publisher.events[0]
    assert name == "alert_issued"
    assert payload["event"] == "Severe Thunderstorm Warning"
    assert payload["priority"] == "high"


def test_run_alert_cycle_event_payload_includes_richer_nws_fields() -> None:
    # task LOC feature B: event payloads gain id (NWS alert_id),
    # severity/urgency/certainty alongside the existing headline/priority/
    # description/event fields.
    config = _config()
    engine = RuleEngine(config)
    tracker = AlertTracker()
    poller = FakePoller(_fixture_features())
    publisher = FakePublisher()

    run_alert_cycle(poller, tracker, engine, publisher, NOON)

    assert len(publisher.events) == 1
    _, payload = publisher.events[0]
    assert payload["id"] == "urn:oid:2.49.0.1.840.0.a1b2c3d4e5f6.2026.08.09.16.00.00.001"
    assert payload["severity"] == "Severe"
    assert payload["urgency"] == "Immediate"
    assert payload["certainty"] == "Observed"
    assert payload["headline"].startswith("Severe Thunderstorm Warning")
    assert payload["description"]


def test_run_alert_cycle_second_call_with_same_alerts_emits_no_new_events() -> None:
    config = _config()
    engine = RuleEngine(config)
    tracker = AlertTracker()
    poller = FakePoller(_fixture_features())
    publisher = FakePublisher()

    run_alert_cycle(poller, tracker, engine, publisher, NOON)
    publisher.events.clear()

    run_alert_cycle(poller, tracker, engine, publisher, NOON)

    assert publisher.events == []


def test_run_alert_cycle_forwards_cleared_event_when_alert_disappears() -> None:
    config = _config()
    engine = RuleEngine(config)
    tracker = AlertTracker()
    publisher = FakePublisher()

    run_alert_cycle(FakePoller(_fixture_features()), tracker, engine, publisher, NOON)
    publisher.events.clear()

    result = run_alert_cycle(FakePoller([]), tracker, engine, publisher, NOON)

    assert result == 0
    assert len(publisher.events) == 1
    name, payload = publisher.events[0]
    assert name == "alert_cleared"
    assert payload["event"] == "Severe Thunderstorm Warning"


def test_run_alert_cycle_returns_none_and_skips_alert_states_on_poll_failure() -> None:
    config = _config()
    engine = RuleEngine(config)
    tracker = AlertTracker()
    poller = FakePoller(None, available=False)
    publisher = FakePublisher()

    result = run_alert_cycle(poller, tracker, engine, publisher, NOON)

    assert result is None
    keys = {key for key, _, _ in publisher.states}
    assert keys == {"nws_available", "config_problem"}
    assert publisher.state_by_key()["nws_available"][0] is False


def test_run_alert_cycle_no_matched_alerts_publishes_none_highest() -> None:
    config = _config(alerts_critical=(), alerts_high=(), alerts_normal=())
    engine = RuleEngine(config)
    tracker = AlertTracker()
    poller = FakePoller(_fixture_features())
    publisher = FakePublisher()

    run_alert_cycle(poller, tracker, engine, publisher, NOON)

    states = publisher.state_by_key()
    assert states["active_alerts"][0] == 2
    assert states["highest_alert"][0] == "None"
    assert states["critical_alert"][0] is False
    assert publisher.events == []


def test_run_alert_cycle_critical_alert_on_when_a_matched_alert_is_critical() -> None:
    config = _config(alerts_critical=("Severe Thunderstorm Warning",))
    engine = RuleEngine(config)
    tracker = AlertTracker()
    poller = FakePoller(_fixture_features())
    publisher = FakePublisher()

    run_alert_cycle(poller, tracker, engine, publisher, NOON)

    states = publisher.state_by_key()
    assert states["critical_alert"][0] is True
    assert states["highest_alert"][0] == "Severe Thunderstorm Warning"


def test_run_alert_cycle_config_problem_reflects_engine_last_error() -> None:
    config = _config()
    engine = RuleEngine(config)
    engine.last_error = "boom: bad yaml"
    tracker = AlertTracker()
    poller = FakePoller(_fixture_features())
    publisher = FakePublisher()

    run_alert_cycle(poller, tracker, engine, publisher, NOON)

    value, attrs = publisher.state_by_key()["config_problem"]
    assert value is True
    assert attrs == {"last_error": "boom: bad yaml"}


# --- _maybe_reconnect ---------------------------------------------------------


class ConnectFakePublisher:
    def __init__(self, connect_side_effect=None) -> None:
        self.connected = False
        self.connect_calls = 0
        self._side_effect = connect_side_effect

    def connect(self) -> None:
        self.connect_calls += 1
        if self._side_effect is not None:
            self._side_effect(self)


def test_maybe_reconnect_returns_floor_when_already_connected() -> None:
    publisher = ConnectFakePublisher()
    publisher.connected = True

    backoff = _maybe_reconnect(publisher, 40.0)

    assert backoff == 5.0
    assert publisher.connect_calls == 0


def test_maybe_reconnect_calls_connect_and_doubles_backoff_when_still_disconnected() -> None:
    publisher = ConnectFakePublisher()

    backoff = _maybe_reconnect(publisher, 5.0)

    assert publisher.connect_calls == 1
    assert backoff == 10.0


def test_maybe_reconnect_caps_backoff_at_60() -> None:
    publisher = ConnectFakePublisher()

    backoff = _maybe_reconnect(publisher, 40.0)

    assert backoff == 60.0


def test_maybe_reconnect_swallows_connect_exception_and_still_backs_off() -> None:
    def _boom(pub) -> None:
        raise RuntimeError("unreachable broker")

    publisher = ConnectFakePublisher(connect_side_effect=_boom)

    backoff = _maybe_reconnect(publisher, 5.0)  # must not raise

    assert publisher.connect_calls == 1
    assert backoff == 10.0


def test_maybe_reconnect_resets_to_floor_once_connect_succeeds() -> None:
    def _succeed(pub) -> None:
        pub.connected = True

    publisher = ConnectFakePublisher(connect_side_effect=_succeed)

    backoff = _maybe_reconnect(publisher, 40.0)

    assert backoff == 5.0


# --- entity registry -----------------------------------------------------


def test_entities_cover_the_b_milestone_entity_map() -> None:
    keys = {entity.key for entity in _ENTITIES}
    assert keys == {
        "active_alerts",
        "highest_alert",
        "critical_alert",
        "config_problem",
        "nws_available",
    }

    by_key = {entity.key: entity for entity in _ENTITIES}
    assert by_key["config_problem"].name == "Config problem"
    assert by_key["config_problem"].entity_category == "diagnostic"
    assert by_key["nws_available"].entity_category == "diagnostic"
    assert by_key["critical_alert"].device_class == "safety"
    assert by_key["config_problem"].device_class == "problem"
    assert by_key["nws_available"].device_class == "connectivity"
    assert by_key["active_alerts"].component == "sensor"
    assert by_key["critical_alert"].component == "binary_sensor"


# --- Supervisor: alerts.yaml generate + hot reload ----------------------------


def _fake_supervisor(tmp_path: Path, **config_overrides: object) -> Supervisor:
    config = _config(config_dir=str(tmp_path), **config_overrides)
    return Supervisor(
        config,
        poller=FakePoller(_fixture_features()),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=FakePublisher(),
    )


def test_ensure_alerts_yaml_generates_default_when_absent(tmp_path: Path) -> None:
    supervisor = _fake_supervisor(tmp_path)
    rules_path = tmp_path / "alerts.yaml"
    assert not rules_path.exists()

    supervisor._ensure_alerts_yaml()

    assert rules_path.exists()
    assert "Tornado Warning" in rules_path.read_text(encoding="utf-8")


def test_ensure_alerts_yaml_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    rules_path = tmp_path / "alerts.yaml"
    rules_path.write_text("version: 1\nrules: []\n", encoding="utf-8")
    supervisor = _fake_supervisor(tmp_path)

    supervisor._ensure_alerts_yaml()

    assert rules_path.read_text(encoding="utf-8") == "version: 1\nrules: []\n"


def test_reload_rules_if_changed_loads_on_first_call(tmp_path: Path) -> None:
    rules_path = tmp_path / "alerts.yaml"
    rules_path.write_text("version: 1\nrules: []\n", encoding="utf-8")
    supervisor = _fake_supervisor(tmp_path)

    supervisor._reload_rules_if_changed()

    assert supervisor.engine.last_error is None
    assert supervisor._rules_mtime is not None


def test_reload_rules_if_changed_skips_reload_when_mtime_unchanged(tmp_path: Path) -> None:
    rules_path = tmp_path / "alerts.yaml"
    rules_path.write_text("version: 1\nrules: []\n", encoding="utf-8")
    supervisor = _fake_supervisor(tmp_path)
    supervisor._reload_rules_if_changed()

    calls: list[str] = []
    supervisor.engine.load = lambda path: calls.append(path)  # type: ignore[method-assign]

    supervisor._reload_rules_if_changed()

    assert calls == []


def test_reload_rules_if_changed_reloads_when_mtime_changes(tmp_path: Path) -> None:
    rules_path = tmp_path / "alerts.yaml"
    rules_path.write_text("version: 1\nrules: []\n", encoding="utf-8")
    supervisor = _fake_supervisor(tmp_path)
    supervisor._reload_rules_if_changed()
    first_mtime = supervisor._rules_mtime
    assert first_mtime is not None

    rules_path.write_text("version: 1\nrules: []\n", encoding="utf-8")
    os.utime(rules_path, (first_mtime + 5, first_mtime + 5))

    supervisor._reload_rules_if_changed()

    assert supervisor._rules_mtime == first_mtime + 5


def test_rules_loop_tick_survives_and_logs_on_unexpected_exception(tmp_path: Path, caplog) -> None:
    # _reload_rules_if_changed can raise something other than the OSError it
    # already guards against - e.g. UnicodeDecodeError from a non-UTF-8
    # alerts.yaml bubbling up out of engine.load(). The rules hot-reload
    # daemon thread must survive that (mirrors _nws_loop's try/except), not
    # die silently and permanently. Drive the per-iteration body directly
    # instead of the real 30s-interval threaded loop.
    supervisor = _fake_supervisor(tmp_path)

    def _boom() -> None:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    supervisor._reload_rules_if_changed = _boom  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR):
        supervisor._rules_loop_tick()  # must not raise

    assert any(
        record.name == "stormwatch" and "rules reload check failed" in record.getMessage()
        for record in caplog.records
    )


# --- Supervisor: poll_now / _status --------------------------------------


def test_poll_now_updates_active_alerts_count(tmp_path: Path) -> None:
    supervisor = _fake_supervisor(tmp_path)

    supervisor.poll_now()

    assert supervisor._active_alerts_count == 2


def test_poll_now_leaves_count_unchanged_on_poll_failure(tmp_path: Path) -> None:
    supervisor = _fake_supervisor(tmp_path)
    supervisor.poll_now()
    supervisor.poller = FakePoller(None, available=False)

    supervisor.poll_now()

    assert supervisor._active_alerts_count == 2


def test_status_reports_health_snapshot() -> None:
    config = _config()
    poller = FakePoller(_fixture_features(), available=True)
    publisher = FakePublisher(connected=True)
    engine = RuleEngine(config)
    supervisor = Supervisor(
        config, poller=poller, tracker=AlertTracker(), engine=engine, publisher=publisher
    )
    supervisor._active_alerts_count = 3

    status = supervisor._status()

    assert status == {
        "status": "ok",
        "sources": {"nws": {"available": True}},
        "state": {"active_alerts": 3},
        "config_ok": True,
        "version": __version__,
    }


def test_status_reports_degraded_when_publisher_not_connected() -> None:
    config = _config()
    supervisor = Supervisor(
        config,
        poller=FakePoller(None),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=FakePublisher(connected=False),
    )

    assert supervisor._status()["status"] == "degraded"


def test_status_reports_degraded_when_nws_unavailable_and_enabled() -> None:
    config = _config()
    supervisor = Supervisor(
        config,
        poller=FakePoller(None, available=False),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=FakePublisher(connected=True),
    )

    assert supervisor._status()["status"] == "degraded"


def test_status_reports_ok_when_nws_unavailable_but_disabled() -> None:
    config = _config(nws_enabled=False)
    supervisor = Supervisor(
        config,
        poller=FakePoller(None, available=False),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=FakePublisher(connected=True),
    )

    status = supervisor._status()

    assert status["status"] == "ok"
    # NWS_ENABLED=false: the whole "nws" entry disappears from sources
    # rather than being reported unavailable (mirrors how "lightning" is
    # simply absent when lightning wiring isn't active).
    assert "nws" not in status["sources"]


def test_status_reports_degraded_when_engine_has_last_error() -> None:
    config = _config()
    engine = RuleEngine(config)
    engine.last_error = "boom: bad yaml"
    supervisor = Supervisor(
        config,
        poller=FakePoller(_fixture_features(), available=True),
        tracker=AlertTracker(),
        engine=engine,
        publisher=FakePublisher(connected=True),
    )

    status = supervisor._status()

    assert status["status"] == "degraded"
    assert status["config_ok"] is False


# --- Supervisor.start(): exact startup log lines -----------------------------


class _FakeHealthServer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_start_logs_exact_startup_lines_and_registers_discovery(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    # Startup lines are quoted verbatim in the INSTALL docs (task contract) -
    # pin both the logger name and the exact message text. start_health_server
    # is monkeypatched so this test opens no real socket at all.
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    config = _config(config_dir=str(tmp_path), nws_contact="you@example.com", nws_poll_seconds=45)
    publisher = FakePublisher()
    supervisor = Supervisor(
        config,
        poller=FakePoller(_fixture_features()),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=publisher,
    )

    with caplog.at_level(logging.INFO):
        supervisor.start()
    supervisor.stop()

    messages = [(record.name, record.levelname, record.getMessage()) for record in caplog.records]
    assert ("stormwatch", "INFO", f"StormWatch {__version__} starting") in messages
    assert (
        "stormwatch.sources.nws",
        "INFO",
        "NWS poller started (contact: you@example.com, poll interval 45s)",
    ) in messages
    # Discovery also always includes the always-on entity set (Fix 2:
    # binary_sensor.stormwatch_connected -- never gated, see _ALWAYS_ENTITIES
    # in stormwatch.__main__), on top of the NWS set registered here.
    assert len(publisher.discovery) == len(_ENTITIES) + len(_ALWAYS_ENTITIES)
    assert fake_health.stopped is True


# --- Supervisor.start(): NWS_ENABLED=false actually disables NWS -------------


def test_start_does_not_start_nws_or_rules_threads_when_nws_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    # Real spec bug (E2E-discovered): NWS_ENABLED=false must stop the NWS
    # poll thread and the alerts.yaml hot-reload thread from starting at
    # all, not just make _status() treat unavailability as moot. A
    # lightning-only or rain-only deployment must not spin up NWS polling
    # it never needed.
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    config = _config(config_dir=str(tmp_path), nws_enabled=False, nws_contact="")
    publisher = FakePublisher()
    supervisor = Supervisor(
        config,
        poller=FakePoller(_fixture_features()),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=publisher,
    )

    supervisor.start()
    supervisor.stop()

    thread_names = {t.name for t in supervisor._threads}
    assert "stormwatch-nws" not in thread_names
    assert "stormwatch-rules" not in thread_names
    assert "stormwatch-mqtt" in thread_names


def test_start_excludes_nws_entities_from_discovery_when_nws_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    config = _config(config_dir=str(tmp_path), nws_enabled=False, nws_contact="")
    publisher = FakePublisher()
    supervisor = Supervisor(
        config,
        poller=FakePoller(_fixture_features()),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=publisher,
    )

    supervisor.start()
    supervisor.stop()

    # NWS-only entities are excluded, but the always-on set (Fix 2:
    # binary_sensor.stormwatch_connected) still registers even with
    # everything else disabled -- see _ALWAYS_ENTITIES in stormwatch.__main__.
    assert {e.key for e in publisher.discovery} == {e.key for e in _ALWAYS_ENTITIES}


# --- main(): ConfigError exit path and SIGTERM/SIGINT wiring -----------------


class _FakeSupervisorForMain:
    """Stand-in for Supervisor injected into main() via monkeypatch.

    Records start()/stop() calls with no real threads, sockets, or MQTT -
    lets us drive main()'s control flow (config load -> start -> wait for
    shutdown signal -> stop) in a unit test.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_main_exits_1_and_logs_config_error_when_mqtt_host_missing(monkeypatch, caplog) -> None:
    # load_config() reads os.environ directly inside main() - remove the
    # required MQTT_HOST var so it raises ConfigError. (LATITUDE/LONGITUDE
    # are no longer required at this layer -- see stormwatch.location -- so
    # MQTT_HOST is the trigger here instead.) logging.basicConfig is
    # stubbed out so this test never mutates the real root logger's handler
    # list (caplog captures independently of it, and other tests must not
    # inherit handlers this test would otherwise install).
    monkeypatch.delenv("MQTT_HOST", raising=False)
    monkeypatch.setattr("stormwatch.__main__.logging.basicConfig", lambda *a, **k: None)

    with caplog.at_level(logging.ERROR, logger="stormwatch"):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    assert any("MQTT_HOST" in record.getMessage() for record in caplog.records)


def test_handle_shutdown_signal_sets_stop_event() -> None:
    # The handler used to be an inline lambda closure in main(); factored to
    # module level so it can be exercised directly - no real OS signal is
    # sent (SIGTERM in particular can't be delivered to self reliably on
    # Windows).
    stop_event = threading.Event()

    _handle_shutdown_signal(stop_event, signal.SIGTERM, None)

    assert stop_event.is_set()


def test_main_stops_supervisor_when_shutdown_signal_fires(monkeypatch) -> None:
    # End-to-end through main()'s own control flow: signal.signal is faked
    # to record the handler main() installs (instead of touching real
    # process-wide signal state), and threading.Event.wait is faked to
    # invoke that recorded handler - exactly as the OS would on SIGTERM/
    # SIGINT - before unblocking, proving the handler drives the blocking
    # wait to return and supervisor.stop() to run. No real signal is sent.
    monkeypatch.setenv("LATITUDE", "34.0234")
    monkeypatch.setenv("LONGITUDE", "-84.6155")
    monkeypatch.setenv("MQTT_HOST", "192.168.1.10")
    monkeypatch.setenv("NWS_CONTACT", "you@example.com")
    monkeypatch.setattr("stormwatch.__main__.logging.basicConfig", lambda *a, **k: None)

    fake_supervisor = _FakeSupervisorForMain(None)
    monkeypatch.setattr("stormwatch.__main__.Supervisor", lambda config: fake_supervisor)

    registered: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: registered.__setitem__(sig, handler))

    def _fire_signal_then_unblock(self: threading.Event, timeout: float | None = None) -> bool:
        registered[signal.SIGTERM](signal.SIGTERM, None)
        return True

    monkeypatch.setattr(threading.Event, "wait", _fire_signal_then_unblock)

    main()

    assert signal.SIGTERM in registered
    assert signal.SIGINT in registered
    assert fake_supervisor.started is True
    assert fake_supervisor.stopped is True


# --- main(): location resolution wiring (task LOC) ---------------------------


def _set_required_env(monkeypatch, **overrides: str) -> None:
    env = {
        "MQTT_HOST": "192.168.1.10",
        "NWS_CONTACT": "you@example.com",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for var in ("LATITUDE", "LONGITUDE", "LOCATION"):
        if var not in env:
            monkeypatch.delenv(var, raising=False)


def _install_fake_supervisor(monkeypatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def _factory(config: Config) -> _FakeSupervisorForMain:
        supervisor = _FakeSupervisorForMain(config)
        captured["supervisor"] = supervisor
        return supervisor

    monkeypatch.setattr("stormwatch.__main__.Supervisor", _factory)
    monkeypatch.setattr("stormwatch.__main__.logging.basicConfig", lambda *a, **k: None)
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: True)
    return captured


def test_main_default_location_logs_warning_and_uses_default_coords(monkeypatch, caplog) -> None:
    _set_required_env(monkeypatch)
    captured = _install_fake_supervisor(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="stormwatch"):
        main()

    supervisor = captured["supervisor"]
    assert supervisor.config.latitude == pytest.approx(33.749)
    assert supervisor.config.longitude == pytest.approx(-84.388)
    assert any(
        "default location" in record.getMessage().lower() and "Atlanta" in record.getMessage()
        for record in caplog.records
    )


def test_main_explicit_coordinates_logs_no_default_warning(monkeypatch, caplog) -> None:
    _set_required_env(monkeypatch, LATITUDE="34.0234", LONGITUDE="-84.6155")
    captured = _install_fake_supervisor(monkeypatch)

    with caplog.at_level(logging.INFO, logger="stormwatch"):
        main()

    supervisor = captured["supervisor"]
    assert supervisor.config.latitude == pytest.approx(34.0234)
    assert supervisor.config.longitude == pytest.approx(-84.6155)
    assert not any("default location" in record.getMessage().lower() for record in caplog.records)


def test_main_geocoded_source_logs_resolved_location(monkeypatch, caplog) -> None:
    _set_required_env(monkeypatch, LOCATION="New York, NY")
    captured = _install_fake_supervisor(monkeypatch)

    def _fake_resolve(config, cache_path):
        return (40.7128, -74.006, "geocoded")

    monkeypatch.setattr("stormwatch.__main__.resolve_location", _fake_resolve)

    with caplog.at_level(logging.INFO, logger="stormwatch"):
        main()

    supervisor = captured["supervisor"]
    assert supervisor.config.latitude == pytest.approx(40.7128)
    assert supervisor.config.longitude == pytest.approx(-74.006)
    assert any(
        "New York, NY" in record.getMessage()
        and "40.7128" in record.getMessage()
        and "geocoded" in record.getMessage()
        for record in caplog.records
    )


def test_main_cache_source_logs_resolved_location(monkeypatch, caplog) -> None:
    _set_required_env(monkeypatch, LOCATION="New York, NY")
    captured = _install_fake_supervisor(monkeypatch)

    def _fake_resolve(config, cache_path):
        return (40.7128, -74.006, "cache")

    monkeypatch.setattr("stormwatch.__main__.resolve_location", _fake_resolve)

    with caplog.at_level(logging.INFO, logger="stormwatch"):
        main()

    supervisor = captured["supervisor"]
    assert supervisor.config.latitude == pytest.approx(40.7128)
    assert any("cache" in record.getMessage() for record in caplog.records)


def test_main_passes_location_cache_path_under_config_dir(monkeypatch) -> None:
    _set_required_env(monkeypatch, LOCATION="New York, NY", CONFIG_DIR="/config")
    _install_fake_supervisor(monkeypatch)

    calls: list[tuple[object, str]] = []

    def _fake_resolve(config, cache_path):
        calls.append((config, cache_path))
        return (40.7128, -74.006, "geocoded")

    monkeypatch.setattr("stormwatch.__main__.resolve_location", _fake_resolve)

    main()

    assert len(calls) == 1
    _, cache_path = calls[0]
    assert cache_path == os.path.join("/config", "location.json")


def test_main_exits_1_when_resolve_location_raises_config_error(monkeypatch, caplog) -> None:
    _set_required_env(monkeypatch, LOCATION="Nonexistent Place, ZZ")
    monkeypatch.setattr("stormwatch.__main__.logging.basicConfig", lambda *a, **k: None)

    def _raise_config_error(config, cache_path):
        raise ConfigError("Could not determine coordinates for LOCATION='Nonexistent Place, ZZ'")

    monkeypatch.setattr("stormwatch.__main__.resolve_location", _raise_config_error)

    def _fail_if_constructed(config):
        raise AssertionError("Supervisor must not be constructed when location resolution fails")

    monkeypatch.setattr("stormwatch.__main__.Supervisor", _fail_if_constructed)

    with caplog.at_level(logging.ERROR, logger="stormwatch"):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    assert any("Nonexistent Place" in record.getMessage() for record in caplog.records)


# --- health.py: real HTTP GET over loopback on an ephemeral port -------------


def test_health_server_returns_status_json_on_ephemeral_port() -> None:
    status = {
        "status": "ok",
        "sources": {"nws": {"available": True}},
        "state": {"active_alerts": 1},
        "config_ok": True,
        "version": __version__,
    }
    server = start_health_server(lambda: status, port=0, host="127.0.0.1")
    try:
        url = f"http://127.0.0.1:{server.port}/healthz"
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/json"
            body = json.loads(response.read().decode("utf-8"))
        assert body == status
    finally:
        server.stop()


def test_health_server_404_for_unknown_path() -> None:
    server = start_health_server(lambda: {}, port=0, host="127.0.0.1")
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=5)
        assert exc_info.value.code == 404
    finally:
        server.stop()


def test_health_server_reflects_status_provider_changes_across_requests() -> None:
    counter = {"active_alerts": 0}
    server = start_health_server(
        lambda: {"state": {"active_alerts": counter["active_alerts"]}},
        port=0,
        host="127.0.0.1",
    )
    try:
        url = f"http://127.0.0.1:{server.port}/healthz"
        with urllib.request.urlopen(url, timeout=5) as response:
            first = json.loads(response.read().decode("utf-8"))
        counter["active_alerts"] = 3
        with urllib.request.urlopen(url, timeout=5) as response:
            second = json.loads(response.read().decode("utf-8"))
        assert first["state"]["active_alerts"] == 0
        assert second["state"]["active_alerts"] == 3
    finally:
        server.stop()
