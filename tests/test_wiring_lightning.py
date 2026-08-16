"""Tests for the lightning/pool wiring in stormwatch.__main__ (DESIGN.md
§6, §8, task C4).

Two layers:

- ``LightningWiring`` unit tests: strike/tick in, publish/event calls out,
  driven directly with a fake Publisher and a real LightningStateMachine
  (fake settable clock) -- no Supervisor, no threads, no sockets.
- Supervisor-level tests: entity registration, the exact Blitzortung
  startup log line, /healthz lightning fields, and the disabled-mode
  no-op -- using FakePoller/FakePublisher/FakeBlitzortungClient. Where a
  test calls ``Supervisor.start()`` it always monkeypatches
  ``start_health_server`` (mirrors tests/test_supervisor.py) so no real
  socket opens, and always calls ``.stop()`` afterwards so the fixed
  health port and background threads never leak between tests.

No real MQTT broker and no network to blitzortung.ha.sed.pl anywhere in
this file.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from stormwatch.__main__ import (
    _ALWAYS_ENTITIES,
    _ENTITIES,
    LightningWiring,
    Supervisor,
    _entity_specs,
    _lightning_entities,
    _make_blitzortung_client,
)
from stormwatch.config import Config
from stormwatch.geo import cells_for_radius
from stormwatch.rules import AlertTracker, RuleEngine

ALL_CLEAR_MINUTES = 30
ALL_CLEAR_SECONDS = ALL_CLEAR_MINUTES * 60


class FakeClock:
    """Settable monotonic-like clock for deterministic timer tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakePublisher:
    """Duck-typed stand-in for Publisher -- records calls, no network."""

    def __init__(self, connected: bool = True) -> None:
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

    def state_values(self, key: str) -> list[object]:
        return [value for k, value, _ in self.states if k == key]

    def last_state(self, key: str) -> tuple[object, dict | None] | None:
        matches = [(value, attrs) for k, value, attrs in self.states if k == key]
        return matches[-1] if matches else None


class FakePoller:
    """Duck-typed stand-in for NwsPoller -- no network."""

    def __init__(self, available: bool = True) -> None:
        self.available = available

    def poll_once(self) -> list[dict]:
        return []


class FakeBlitzortungClient:
    """Duck-typed stand-in for BlitzortungClient -- no network."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


def _config(**overrides: object) -> Config:
    base: dict[str, object] = dict(
        latitude=34.0234,
        longitude=-84.6155,
        mqtt_host="192.168.1.10",
        nws_contact="you@example.com",
    )
    base.update(overrides)
    return Config(**base)


class _FakeHealthServer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


# --- LightningWiring: strike in -> publish/event out -------------------------


def test_strike_within_close_radius_publishes_closed_once_and_emits_event_once() -> None:
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)

    wiring.on_strike(8.0, 225.0, 0.0, 34.1, -84.1)

    assert publisher.last_state("swim_status") == ("CLOSED", None)
    assert publisher.last_state("lightning_nearby") == (True, None)
    assert publisher.events == [
        ("lightning_close", {"distance_km": 8.0, "bearing_deg": 225.0, "bearing_compass": "SW"})
    ]


def test_second_qualifying_strike_does_not_reemit_lightning_close() -> None:
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)

    wiring.on_strike(8.0, 225.0, 0.0, 34.1, -84.1)
    clock.advance(60)
    wiring.on_strike(5.0, 230.0, 0.0, 34.1, -84.1)

    assert len(publisher.events) == 1


def test_tick_with_no_new_strikes_does_not_republish_unchanged_keys() -> None:
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)
    wiring.on_strike(8.0, 225.0, 0.0, 34.1, -84.1)
    count_before = len(publisher.states)

    wiring.tick(available=True)

    new_states = publisher.states[count_before:]
    # "strikes" is expected here too: on_strike marks the strike buffer
    # dirty, and this is the first tick since -- but nothing else (that
    # on_strike's own _publish_state_locked call already republished)
    # should fire again just because a tick happened.
    non_heartbeat = [
        entry for entry in new_states if entry[0] not in ("lightning_available", "strikes")
    ]
    assert non_heartbeat == []
    assert {entry[0] for entry in new_states} == {"lightning_available", "strikes"}

    # A second, unchanged tick must not republish "strikes" again -- the
    # dirty flag was cleared by the first tick's publish.
    count_before_second = len(publisher.states)
    wiring.tick(available=True)
    new_keys_second = [entry[0] for entry in publisher.states[count_before_second:]]
    assert new_keys_second == ["lightning_available"]


def test_lightning_available_heartbeat_republishes_every_tick_even_when_unchanged() -> None:
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=FakeClock())

    wiring.tick(available=True)
    wiring.tick(available=True)

    assert publisher.state_values("lightning_available") == [True, True]


def test_tick_force_republishes_all_lightning_keys_every_60th_tick() -> None:
    # Retained-store-loss resilience: if the broker's retained store gets
    # wiped (e.g. a Mosquitto restart with a fresh persistence file), a new
    # Home Assistant subscriber sees nothing until *something* changes. The
    # 1s ticker must republish every lightning key unconditionally once
    # every 60 ticks (~60s), bypassing the publish-on-change cache -- not
    # just the lightning_available heartbeat, which already does this every
    # tick.
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)

    for _ in range(59):
        wiring.tick(available=True)
    count_before_60th = len(publisher.states)

    wiring.tick(available=True)  # the 60th tick: forced republish

    republished_keys = {key for key, _, _ in publisher.states[count_before_60th:]}
    assert republished_keys == {
        "swim_status",
        "nearest_strike_distance",
        "nearest_strike_bearing",
        "strike_count_15m",
        "all_clear_at",
        "lightning_nearby",
        "lightning_available",
        "strikes",
    }


def test_tick_after_forced_republish_change_detection_still_suppresses_unchanged() -> None:
    # The forced republish must still update the change-suppression cache,
    # otherwise the very next unchanged tick would spuriously republish
    # everything again instead of just the heartbeat.
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)

    for _ in range(60):
        wiring.tick(available=True)
    count_before_61st = len(publisher.states)

    wiring.tick(available=True)  # the 61st tick: nothing changed since the 60th

    new_keys = {key for key, _, _ in publisher.states[count_before_61st:]}
    assert new_keys == {"lightning_available"}


def test_strike_count_15m_increments_per_strike_and_decays_after_window() -> None:
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)

    wiring.on_strike(30.0, 180.0, 0.0, 34.1, -84.1)
    assert publisher.last_state("strike_count_15m") == (1, None)

    clock.advance(10)
    wiring.on_strike(28.0, 180.0, 0.0, 34.1, -84.1)
    assert publisher.last_state("strike_count_15m") == (2, None)

    clock.advance(901)  # both strikes now outside the 15-minute ring window
    wiring.tick(available=True)
    assert publisher.last_state("strike_count_15m") == (0, None)


# --- strikes map (StrikeBuffer -> sensor.stormwatch_strikes geojson) ----------


def test_tick_publishes_strikes_entity_with_geojson_feature_per_strike() -> None:
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)

    wiring.on_strike(8.0, 225.0, 0.0, 34.1, -84.1)
    wiring.on_strike(5.0, 230.0, 0.0, 34.2, -84.2)

    wiring.tick(available=True)

    value, attrs = publisher.last_state("strikes")
    assert value == 2
    assert attrs["geojson"]["type"] == "FeatureCollection"
    assert len(attrs["geojson"]["features"]) == 2


def test_first_tick_with_no_strikes_publishes_initial_empty_strikes_state() -> None:
    # Fix 1: a freshly-constructed LightningWiring must publish an initial
    # "strikes" value on the very first tick, even with zero strikes so far
    # -- otherwise sensor.stormwatch_strikes sits at HA's "unknown" until
    # either the first real strike or the 60s force-republish cycle.
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=FakeClock())

    wiring.tick(available=True)

    assert publisher.last_state("strikes") == (
        0,
        {"geojson": {"type": "FeatureCollection", "features": []}},
    )


def test_on_strike_alone_does_not_publish_strikes_geojson() -> None:
    # The geojson publish is throttled to <=1/sec by only happening from
    # tick() -- on_strike (paho's own network thread) must never publish it
    # directly, however many strikes arrive between ticks.
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)

    wiring.on_strike(8.0, 225.0, 0.0, 34.1, -84.1)

    assert publisher.last_state("strikes") is None


def test_strikes_geojson_drops_features_once_pruned_by_a_later_tick() -> None:
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(strike_map_window_minutes=1), publisher, clock=clock)

    wiring.on_strike(8.0, 225.0, 0.0, 34.1, -84.1)
    wiring.tick(available=True)
    value, attrs = publisher.last_state("strikes")
    assert value == 1
    assert len(attrs["geojson"]["features"]) == 1

    clock.advance(61)  # past the 1-minute strike-map window
    wiring.tick(available=True)

    value, attrs = publisher.last_state("strikes")
    assert value == 0
    assert attrs["geojson"]["features"] == []


# --- unit conversion at publish -----------------------------------------------


def test_nearest_strike_distance_converts_km_to_miles_rounded_1dp_when_imperial() -> None:
    publisher = FakePublisher()
    wiring = LightningWiring(_config(units="imperial"), publisher, clock=FakeClock())

    wiring.on_strike(8.0, 90.0, 0.0, 34.1, -84.1)

    assert publisher.last_state("nearest_strike_distance") == (5.0, None)


def test_nearest_strike_distance_stays_km_when_metric() -> None:
    publisher = FakePublisher()
    wiring = LightningWiring(_config(units="metric"), publisher, clock=FakeClock())

    wiring.on_strike(8.0, 90.0, 0.0, 34.1, -84.1)

    assert publisher.last_state("nearest_strike_distance") == (8.0, None)


def test_nearest_strike_bearing_publishes_compass_string_with_degrees_attr() -> None:
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=FakeClock())

    wiring.on_strike(8.0, 225.0, 0.0, 34.1, -84.1)

    assert publisher.last_state("nearest_strike_bearing") == ("SW", {"degrees": 225.0})


def test_km_per_mile_constant_matches_configs_conversion_factor() -> None:
    # __main__._KM_PER_MILE is a deliberate local duplicate of
    # config._MILES_TO_KM (config.py isn't wired to export it cleanly, and
    # config.py is out of scope for this change) -- this test is the
    # drift-catcher: if one changes without the other, this fails instead
    # of nearest_strike_distance silently reporting different miles than
    # CLOSE_RADIUS/WATCH_RADIUS's own imperial<->km conversion.
    from stormwatch import config as stormwatch_config
    from stormwatch.__main__ import _KM_PER_MILE

    assert _KM_PER_MILE == stormwatch_config._MILES_TO_KM


# --- all_clear_at ISO derivation -----------------------------------------------


def test_all_clear_at_publishes_iso_string_roughly_all_clear_minutes_ahead() -> None:
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=FakeClock())

    wiring.on_strike(8.0, 90.0, 0.0, 34.1, -84.1)

    value, _ = publisher.last_state("all_clear_at")
    assert isinstance(value, str) and value != ""
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    delta_seconds = (parsed - datetime.now(UTC)).total_seconds()
    assert ALL_CLEAR_SECONDS - 5 <= delta_seconds <= ALL_CLEAR_SECONDS + 5


def test_all_clear_at_publishes_none_string_after_all_clear_event() -> None:
    # Literal "None" (not "") when CLEAR: an empty-string MQTT payload is a
    # retained-delete, which would wipe the entity instead of showing a
    # value -- "None" matches the nearest_strike_distance/bearing
    # convention for "no current value" (see _distance_value/_bearing_value).
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(all_clear_minutes=ALL_CLEAR_MINUTES), publisher, clock=clock)
    wiring.on_strike(8.0, 90.0, 0.0, 34.1, -84.1)

    clock.advance(ALL_CLEAR_SECONDS)
    wiring.tick(available=True)

    assert publisher.last_state("all_clear_at") == ("None", None)
    assert any(name == "all_clear" for name, _ in publisher.events)


# --- stale-feed handling (Fix 1, SAFETY-CRITICAL) -------------------------------
#
# A dead Blitzortung feed must never let the all-clear timer expire on
# silence alone -- "stale data is never clear" (DESIGN.md §6). While
# available=False, the timer must hold (state.tick() skipped entirely); on
# recovery (False->True), a not-CLEAR state gets its timer RESET to a fresh
# full window (state.restart_timer()), so an all-clear only ever follows a
# full window of LIVE, no-strike data -- never however much of the old
# monotonic deadline happened to already elapse before/during the outage.


def test_feed_lost_while_closed_freezes_timer_no_all_clear_across_full_window(
    caplog,
) -> None:
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)
    wiring.on_strike(8.0, 225.0, 0.0, 34.1, -84.1)  # CLOSED, deadline = ALL_CLEAR_SECONDS
    assert wiring._state.state == "CLOSED"

    with caplog.at_level(logging.WARNING):
        wiring.tick(available=False)  # True -> False transition: warn once

    assert (
        "stormwatch",
        "WARNING",
        "Lightning feed lost — holding state CLOSED, all-clear timer suspended",
    ) in [(r.name, r.levelname, r.getMessage()) for r in caplog.records]

    # Tick clean past the *entire* all-clear window while unavailable.
    for _ in range(int(ALL_CLEAR_SECONDS) + 5):
        clock.advance(1)
        wiring.tick(available=False)

    assert wiring._state.state == "CLOSED"
    assert wiring._state.all_clear_at is not None
    assert not any(name == "all_clear" for name, _ in publisher.events)
    assert publisher.last_state("swim_status") == ("CLOSED", None)


def test_feed_restored_after_loss_requires_a_full_fresh_window_before_all_clear() -> None:
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)
    wiring.on_strike(8.0, 225.0, 0.0, 34.1, -84.1)  # CLOSED, deadline = ALL_CLEAR_SECONDS

    clock.advance(ALL_CLEAR_SECONDS - 5)  # nearly expired
    wiring.tick(available=False)  # feed drops, timer frozen with ~5s left

    clock.advance(500)  # a long outage -- old deadline is now far in the past
    wiring.tick(available=True)  # feed restored: timer must be RESET, not resumed

    assert wiring._state.state == "CLOSED"  # not all_clear yet
    assert not any(name == "all_clear" for name, _ in publisher.events)

    # Advance almost the full fresh window: still no all-clear.
    for _ in range(int(ALL_CLEAR_SECONDS) - 1):
        clock.advance(1)
        wiring.tick(available=True)
    assert not any(name == "all_clear" for name, _ in publisher.events)

    # The rest of the fresh window elapses: now it fires.
    clock.advance(2)
    wiring.tick(available=True)
    assert any(name == "all_clear" for name, _ in publisher.events)
    assert wiring._state.state == "CLEAR"


def test_feed_restored_after_loss_logs_restart_with_state(caplog) -> None:
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)
    wiring.on_strike(30.0, 90.0, 0.0, 34.1, -84.1)  # WATCH
    wiring.tick(available=False)

    with caplog.at_level(logging.INFO):
        wiring.tick(available=True)

    assert (
        "stormwatch",
        "INFO",
        "Lightning feed restored — all-clear timer restarted (WATCH)",
    ) in [(r.name, r.levelname, r.getMessage()) for r in caplog.records]


def test_feed_lost_and_restored_while_clear_no_warning_or_restart_log(caplog) -> None:
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)
    assert wiring._state.state == "CLEAR"

    with caplog.at_level(logging.INFO):
        wiring.tick(available=False)  # lost while CLEAR: no warning
        wiring.tick(available=False)
        wiring.tick(available=True)  # restored while CLEAR: no restart log

    messages = [r.getMessage() for r in caplog.records]
    assert not any("Lightning feed lost" in m for m in messages)
    assert not any("Lightning feed restored" in m for m in messages)
    assert wiring._state.state == "CLEAR"
    assert wiring._state.all_clear_at is None


def test_strike_while_unavailable_still_processed_via_on_strike() -> None:
    # paho may still deliver a queued/in-flight message during a brief
    # availability flap; on_strike must keep working regardless of the
    # ticker's availability flag (it's not gated on it at all).
    clock = FakeClock()
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=clock)

    wiring.tick(available=False)
    wiring.on_strike(8.0, 225.0, 0.0, 34.1, -84.1)

    assert wiring._state.state == "CLOSED"
    assert publisher.last_state("swim_status") == ("CLOSED", None)
    assert publisher.events == [
        ("lightning_close", {"distance_km": 8.0, "bearing_deg": 225.0, "bearing_compass": "SW"})
    ]


def test_available_tick_from_a_fresh_wiring_does_not_log_restore(caplog) -> None:
    # No spurious "restored" log on ordinary startup (available=True from the
    # first tick, nothing was ever lost).
    publisher = FakePublisher()
    wiring = LightningWiring(_config(), publisher, clock=FakeClock())

    with caplog.at_level(logging.INFO):
        wiring.tick(available=True)
        wiring.tick(available=True)

    assert not any("Lightning feed restored" in r.getMessage() for r in caplog.records)


# --- entity registry -----------------------------------------------------------


def test_lightning_entities_cover_the_c_milestone_entity_map() -> None:
    entities = _lightning_entities(_config())
    keys = {entity.key for entity in entities}

    assert keys == {
        "swim_status",
        "nearest_strike_distance",
        "nearest_strike_bearing",
        "strike_count_15m",
        "all_clear_at",
        "lightning_nearby",
        "lightning_available",
        "strikes",
    }
    by_key = {entity.key: entity for entity in entities}
    assert by_key["nearest_strike_distance"].unit == "mi"
    assert by_key["nearest_strike_bearing"].value_is_json_attr is True
    assert by_key["strike_count_15m"].state_class == "measurement"
    assert by_key["all_clear_at"].device_class == "timestamp"
    assert by_key["lightning_nearby"].device_class == "safety"
    assert by_key["lightning_available"].device_class == "connectivity"
    assert by_key["lightning_available"].entity_category == "diagnostic"
    assert by_key["strikes"].component == "sensor"
    assert by_key["strikes"].state_class == "measurement"
    assert by_key["strikes"].icon == "mdi:flash"
    assert by_key["strikes"].value_is_json_attr is True


def test_lightning_entities_use_km_unit_when_metric() -> None:
    entities = _lightning_entities(_config(units="metric"))
    by_key = {entity.key: entity for entity in entities}
    assert by_key["nearest_strike_distance"].unit == "km"


def test_always_entities_include_connected_not_gated_not_diagnostic() -> None:
    # Fix 2: binary_sensor.stormwatch_connected (DESIGN.md §8) is the
    # headline connectivity entity -- it must never be gated by any source
    # (unlike nws_available/lightning_available/rain_available, which are
    # diagnostic and each gated by their own feature), so it lives in its
    # own always-registered set, not _ENTITIES (which is itself gated by
    # nws_active -- see _entity_specs).
    keys = {e.key for e in _ALWAYS_ENTITIES}
    assert keys == {"connected"}
    by_key = {e.key: e for e in _ALWAYS_ENTITIES}
    assert by_key["connected"].name == "Connected"
    assert by_key["connected"].component == "binary_sensor"
    assert by_key["connected"].device_class == "connectivity"
    assert by_key["connected"].entity_category is None


def test_entity_specs_includes_lightning_only_when_active() -> None:
    config = _config()
    assert {e.key for e in _entity_specs(config, lightning_active=False)} == {
        e.key for e in _ENTITIES
    } | {e.key for e in _ALWAYS_ENTITIES}
    with_lightning = {e.key for e in _entity_specs(config, lightning_active=True)}
    assert with_lightning == {e.key for e in _ENTITIES} | {e.key for e in _ALWAYS_ENTITIES} | {
        e.key for e in _lightning_entities(config)
    }


def test_entity_specs_excludes_nws_entities_when_nws_inactive() -> None:
    # NWS_ENABLED=false must exclude the whole NWS alert entity set (all of
    # _ENTITIES: active_alerts, highest_alert, critical_alert, nws_available,
    # config_problem) -- mirrors how lightning_active gates _lightning_entities.
    # A lightning-only deployment must still register its own entities. The
    # always-on set (_ALWAYS_ENTITIES: "connected") is never excluded by any
    # of these gates -- it registers in every mode, including everything
    # disabled.
    config = _config()
    everything_disabled = {
        e.key for e in _entity_specs(config, lightning_active=False, nws_active=False)
    }
    assert everything_disabled == {e.key for e in _ALWAYS_ENTITIES}
    lightning_only = {e.key for e in _entity_specs(config, lightning_active=True, nws_active=False)}
    assert lightning_only == {e.key for e in _ALWAYS_ENTITIES} | {
        e.key for e in _lightning_entities(config)
    }


# --- Supervisor: registration + startup log line ------------------------------


def test_supervisor_start_registers_lightning_entities_when_active(
    tmp_path: Path, monkeypatch
) -> None:
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    config = _config(config_dir=str(tmp_path))
    publisher = FakePublisher()
    supervisor = Supervisor(
        config,
        poller=FakePoller(),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=publisher,
        blitzortung_client=FakeBlitzortungClient(),
    )

    supervisor.start()
    supervisor.stop()

    discovered_keys = {e.key for e in publisher.discovery}
    assert discovered_keys == {e.key for e in _ENTITIES} | {e.key for e in _ALWAYS_ENTITIES} | {
        e.key for e in _lightning_entities(config)
    }


def test_supervisor_start_logs_exact_blitzortung_startup_line(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    config = _config(config_dir=str(tmp_path))
    publisher = FakePublisher()
    client = FakeBlitzortungClient()
    supervisor = Supervisor(
        config,
        poller=FakePoller(),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=publisher,
        blitzortung_client=client,
    )
    expected_cells = len(
        cells_for_radius(config.latitude, config.longitude, config.watch_radius_km)
    )

    with caplog.at_level(logging.INFO):
        supervisor.start()
    supervisor.stop()

    messages = [(r.name, r.levelname, r.getMessage()) for r in caplog.records]
    assert (
        "stormwatch.sources.blitzortung",
        "INFO",
        f"Blitzortung client started ({expected_cells} cells, host {config.blitzortung_mqtt_host})",
    ) in messages
    assert client.start_calls == 1


def test_supervisor_stop_disconnects_blitzortung_client_when_active(
    tmp_path: Path, monkeypatch
) -> None:
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    config = _config(config_dir=str(tmp_path))
    client = FakeBlitzortungClient()
    supervisor = Supervisor(
        config,
        poller=FakePoller(),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=FakePublisher(),
        blitzortung_client=client,
    )

    supervisor.start()
    supervisor.stop()

    assert client.stop_calls == 1


# --- Supervisor: healthz lightning fields -------------------------------------


def test_status_includes_lightning_source_and_swim_status_when_active() -> None:
    config = _config()
    client = FakeBlitzortungClient(available=True)
    supervisor = Supervisor(
        config,
        poller=FakePoller(),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=FakePublisher(connected=True),
        blitzortung_client=client,
    )
    supervisor._lightning.on_strike(8.0, 90.0, 0.0, 34.1, -84.1)

    status = supervisor._status()

    assert status["sources"]["lightning"] == {"available": True}
    assert status["state"]["swim_status"] == "CLOSED"
    assert status["status"] == "ok"


def test_status_degraded_when_lightning_enabled_but_client_unavailable() -> None:
    config = _config()
    supervisor = Supervisor(
        config,
        poller=FakePoller(),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=FakePublisher(connected=True),
        blitzortung_client=FakeBlitzortungClient(available=False),
    )

    status = supervisor._status()

    assert status["status"] == "degraded"
    assert status["sources"]["lightning"] == {"available": False}


# --- disabled mode: no client, no entities, no ticker, no healthz source -----


def test_disabled_mode_publishes_no_lightning_entities_or_log_line(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    config = _config(config_dir=str(tmp_path), blitzortung_enabled=False)
    publisher = FakePublisher()
    supervisor = Supervisor(
        config,
        poller=FakePoller(),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=publisher,
    )

    with caplog.at_level(logging.INFO):
        supervisor.start()
    supervisor.stop()

    discovered_keys = {e.key for e in publisher.discovery}
    assert discovered_keys == {e.key for e in _ENTITIES} | {e.key for e in _ALWAYS_ENTITIES}
    assert not any("Blitzortung client started" in r.getMessage() for r in caplog.records)
    status = supervisor._status()
    assert "lightning" not in status["sources"]
    assert "swim_status" not in status["state"]


def test_disabled_mode_ignored_even_if_a_client_is_attached_anyway(tmp_path: Path) -> None:
    # BLITZORTUNG_ENABLED=false must win even if something attached a
    # client object -- the config flag is authoritative, not client
    # presence alone.
    config = _config(config_dir=str(tmp_path), blitzortung_enabled=False)
    supervisor = Supervisor(
        config,
        poller=FakePoller(),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=FakePublisher(),
        blitzortung_client=FakeBlitzortungClient(),
    )

    assert supervisor._lightning_active is False


# --- Fix 2: binary_sensor.stormwatch_connected publish on connect/reconnect ---
#
# The 'connected' entity (see _ALWAYS_ENTITIES above) needs a *state*, not
# just a discovery config: "ON" (retained) after every successful publisher
# connect -- initial and every reconnect. Home Assistant's own picture of
# "is it actually alive right now" comes from the LWT availability topic
# (unaffected by this section, publisher.py is out of scope), so this is
# purely about publishing a value at all once a connection exists.
# Supervisor._connect_loop_iteration() is one iteration of the connect/
# reconnect loop, factored out (mirrors _lightning_tick_once/_rain_obs_tick)
# so it's directly testable without a real thread or broker.


def _supervisor_for_connect(publisher) -> Supervisor:
    config = _config()
    return Supervisor(
        config,
        poller=FakePoller(),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=publisher,
    )


def test_connect_loop_iteration_publishes_connected_on_after_initial_connect() -> None:
    publisher = FakePublisher(connected=False)
    supervisor = _supervisor_for_connect(publisher)

    publisher.connected = True  # simulate the async on_connect callback firing
    supervisor._connect_loop_iteration()

    assert publisher.last_state("connected") == (True, None)


def test_connect_loop_iteration_does_not_republish_connected_while_still_connected() -> None:
    publisher = FakePublisher(connected=False)
    supervisor = _supervisor_for_connect(publisher)
    publisher.connected = True
    supervisor._connect_loop_iteration()  # publishes once, on the rising edge

    supervisor._connect_loop_iteration()
    supervisor._connect_loop_iteration()

    assert publisher.state_values("connected") == [True]


def test_connect_loop_iteration_republishes_connected_on_after_a_reconnect() -> None:
    publisher = FakePublisher(connected=False)
    supervisor = _supervisor_for_connect(publisher)
    publisher.connected = True
    supervisor._connect_loop_iteration()  # initial connect
    assert publisher.state_values("connected") == [True]

    publisher.connected = False  # connection drops
    supervisor._connect_loop_iteration()
    assert publisher.state_values("connected") == [True]  # no new publish while down

    publisher.connected = True  # reconnects
    supervisor._connect_loop_iteration()

    assert publisher.state_values("connected") == [True, True]


def test_connect_loop_iteration_publishes_nothing_while_never_connected() -> None:
    publisher = FakePublisher(connected=False)
    supervisor = _supervisor_for_connect(publisher)

    supervisor._connect_loop_iteration()
    supervisor._connect_loop_iteration()

    assert publisher.state_values("connected") == []


# --- main()'s production wiring helper -----------------------------------------


def test_make_blitzortung_client_defers_supervisor_lookup_until_a_strike_arrives() -> None:
    config = _config()
    supervisor = Supervisor(
        config,
        poller=FakePoller(),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=FakePublisher(),
    )

    client = _make_blitzortung_client(config, supervisor)
    # Constructing the client must not have touched supervisor internals yet.
    client._on_strike(8.0, 90.0, 0.0, 34.1, -84.1)  # simulate what BlitzortungClient would call

    assert supervisor._lightning.state == "CLOSED"
