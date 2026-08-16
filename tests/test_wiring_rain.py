"""Tests for the rain/watering wiring in stormwatch.__main__ (DESIGN.md
§4, §8, task D2).

Two layers, mirroring tests/test_wiring_lightning.py:

- ``RainWiring`` unit tests: poll-cycle in, publish calls out, driven
  directly with a fake RainSource/RainStore/Publisher -- no Supervisor, no
  threads, no sockets, no real clock (``now`` is always passed in).
- Supervisor-level tests: entity registration, the exact rain startup log
  lines, /healthz rain fields, and the disabled-mode no-op -- using
  FakePoller/FakePublisher/FakeRainSource/FakeRainStore. Every test that
  calls ``Supervisor.start()`` monkeypatches ``start_health_server`` (mirrors
  tests/test_supervisor.py) so no real socket opens, and always calls
  ``.stop()`` afterwards so the fixed health port and background threads
  never leak between tests.

No real MQTT broker and no network to api.weather.gov anywhere in this
file -- see tests/integration/test_e2e_rain.py for the full stack.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from stormwatch.__main__ import (
    _ALWAYS_ENTITIES,
    _ENTITIES,
    RainWiring,
    Supervisor,
    _entity_specs,
    _format_rain_mm,
    _rain_entities,
)
from stormwatch.config import Config
from stormwatch.rules import AlertTracker, RuleEngine

NOW = datetime(2026, 8, 9, 18, 0, 0, tzinfo=UTC)


def _config(**overrides: object) -> Config:
    base: dict[str, object] = dict(
        latitude=34.0234,
        longitude=-84.6155,
        mqtt_host="192.168.1.10",
        nws_contact="you@example.com",
    )
    base.update(overrides)
    return Config(**base)


class FakePublisher:
    """Duck-typed stand-in for Publisher -- records calls, no network."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.states: list[tuple[str, object, dict | None]] = []
        self.discovery: list[object] = []

    def publish_discovery(self, entities) -> None:
        self.discovery.extend(entities)

    def publish_state(self, key, value, attrs=None) -> None:
        self.states.append((key, value, attrs))

    def publish_event(self, name, payload) -> None:
        pass

    def connect(self) -> None:
        pass

    def offline(self) -> None:
        pass

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


class FakeRainSource:
    """Duck-typed stand-in for RainSource -- no network.

    ``station_on_call`` optionally sets ``_station_id`` (mirroring the real
    RainSource's private discovery cache) starting from the given call
    number, so tests can exercise the "station just resolved" log line.
    """

    def __init__(
        self,
        forecast: dict | None = "unset",  # type: ignore[assignment]
        observations: list[tuple[str, float]] | None = "unset",  # type: ignore[assignment]
        available: bool = True,
        station_on_call: int | None = None,
        station_id: str = "KFFC",
    ) -> None:
        self.available = available
        self._forecast = {} if forecast == "unset" else forecast
        self._observations = [] if observations == "unset" else observations
        self._station_id: str | None = None
        self._station_on_call = station_on_call
        self._station_id_value = station_id
        self.forecast_calls = 0
        self.obs_calls = 0

    def poll_forecast(self) -> dict | None:
        self.forecast_calls += 1
        return self._forecast

    def poll_observations(self) -> list[tuple[str, float]] | None:
        self.obs_calls += 1
        if self._station_on_call is not None and self.obs_calls >= self._station_on_call:
            self._station_id = self._station_id_value
        return self._observations


class FakeRainStore:
    """Duck-typed stand-in for RainStore -- no disk I/O."""

    def __init__(self, totals: dict | None = None, hourly: dict | None = None) -> None:
        self.ingested: list[list[tuple[str, float]]] = []
        self._totals = totals if totals is not None else {"h24_mm": 0.0, "d7_mm": 0.0}
        self._hourly = hourly if hourly is not None else {}

    def ingest(self, buckets: list[tuple[str, float]]) -> None:
        self.ingested.append(buckets)

    def totals(self, now: datetime) -> dict:
        return self._totals

    def hourly_24(self, now: datetime) -> dict:
        return self._hourly


class _FakeHealthServer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _rain_supervisor(
    tmp_path: Path,
    rain_source=None,
    rain_store=None,
    publisher=None,
    **config_overrides: object,
) -> Supervisor:
    config = _config(config_dir=str(tmp_path), **config_overrides)
    return Supervisor(
        config,
        poller=FakePoller(),
        tracker=AlertTracker(),
        engine=RuleEngine(config),
        publisher=publisher if publisher is not None else FakePublisher(),
        rain_source=rain_source,
        rain_store=rain_store,
    )


# --- unit conversion -----------------------------------------------------------


def test_format_rain_mm_imperial_two_decimal_places() -> None:
    assert _format_rain_mm(12.7, "imperial") == "0.50"


def test_format_rain_mm_metric_one_decimal_place() -> None:
    assert _format_rain_mm(12.7, "metric") == "12.7"


# --- RainWiring.run_forecast_cycle ----------------------------------------------


def test_forecast_cycle_publishes_converted_values_imperial() -> None:
    publisher = FakePublisher()
    source = FakeRainSource(forecast={"today_mm": 12.7, "h48_mm": 25.4})
    wiring = RainWiring(_config(units="imperial"), source, FakeRainStore(), publisher)

    wiring.run_forecast_cycle()

    assert publisher.last_state("rain_forecast_today") == ("0.50", None)
    assert publisher.last_state("rain_forecast_48h") == ("1.00", None)
    assert publisher.last_state("rain_available") == (True, None)


def test_forecast_cycle_publishes_converted_values_metric() -> None:
    publisher = FakePublisher()
    source = FakeRainSource(forecast={"today_mm": 12.7, "h48_mm": 25.4})
    wiring = RainWiring(_config(units="metric"), source, FakeRainStore(), publisher)

    wiring.run_forecast_cycle()

    assert publisher.last_state("rain_forecast_today") == ("12.7", None)
    assert publisher.last_state("rain_forecast_48h") == ("25.4", None)


def test_forecast_cycle_poll_none_publishes_none_and_rain_available_off() -> None:
    publisher = FakePublisher()
    source = FakeRainSource(forecast=None, available=False)
    wiring = RainWiring(_config(), source, FakeRainStore(), publisher)

    result = wiring.run_forecast_cycle()

    assert result is None
    assert publisher.last_state("rain_forecast_today") == ("None", None)
    assert publisher.last_state("rain_forecast_48h") == ("None", None)
    assert publisher.last_state("rain_available") == (False, None)


# --- RainWiring.run_obs_cycle ----------------------------------------------------


def test_obs_cycle_ingests_buckets_and_publishes_totals_with_hourly_attrs() -> None:
    publisher = FakePublisher()
    buckets = [("2026-08-09T17:00:00+00:00", 0.3)]
    hourly = {"2026-08-09T17:00:00+00:00": 0.3}
    store = FakeRainStore(totals={"h24_mm": 12.7, "d7_mm": 25.4}, hourly=hourly)
    source = FakeRainSource(observations=buckets)
    wiring = RainWiring(_config(units="imperial"), source, store, publisher)

    result = wiring.run_obs_cycle(NOW)

    assert store.ingested == [buckets]
    assert result == {"h24_mm": 12.7, "d7_mm": 25.4}
    assert publisher.last_state("rain_last_24h") == ("0.50", hourly)
    assert publisher.last_state("rain_last_7d") == ("1.00", None)
    assert publisher.last_state("rain_available") == (True, None)


def test_obs_cycle_poll_none_publishes_none_and_rain_available_off() -> None:
    publisher = FakePublisher()
    store = FakeRainStore()
    source = FakeRainSource(observations=None, available=False)
    wiring = RainWiring(_config(), source, store, publisher)

    result = wiring.run_obs_cycle(NOW)

    assert result is None
    assert store.ingested == []
    assert publisher.last_state("rain_last_24h") == ("None", None)
    assert publisher.last_state("rain_last_7d") == ("None", None)
    assert publisher.last_state("rain_available") == (False, None)


def test_obs_cycle_logs_station_exactly_once_when_it_resolves(caplog) -> None:
    publisher = FakePublisher()
    source = FakeRainSource(observations=[], station_on_call=1, station_id="KFFC")
    wiring = RainWiring(_config(), source, FakeRainStore(), publisher)

    with caplog.at_level(logging.INFO):
        wiring.run_obs_cycle(NOW)
        wiring.run_obs_cycle(NOW)

    messages = [
        (r.name, r.levelname, r.getMessage())
        for r in caplog.records
        if "Using observation station" in r.getMessage()
    ]
    assert messages == [("stormwatch.sources.rain", "INFO", "Using observation station KFFC")]


def test_obs_cycle_does_not_log_station_line_while_still_unresolved(caplog) -> None:
    publisher = FakePublisher()
    source = FakeRainSource(observations=[], station_on_call=None)
    wiring = RainWiring(_config(), source, FakeRainStore(), publisher)

    with caplog.at_level(logging.INFO):
        wiring.run_obs_cycle(NOW)

    assert not any("Using observation station" in r.getMessage() for r in caplog.records)


# --- entity registry -------------------------------------------------------------


def test_rain_entities_cover_the_d_milestone_entity_map() -> None:
    entities = _rain_entities(_config())
    keys = {entity.key for entity in entities}

    assert keys == {
        "rain_forecast_today",
        "rain_forecast_48h",
        "rain_last_24h",
        "rain_last_7d",
        "rain_available",
    }
    by_key = {entity.key: entity for entity in entities}
    for key in ("rain_forecast_today", "rain_forecast_48h", "rain_last_24h", "rain_last_7d"):
        assert by_key[key].component == "sensor"
        assert by_key[key].state_class == "measurement"
        assert by_key[key].unit == "in"
    assert by_key["rain_last_24h"].value_is_json_attr is True
    assert by_key["rain_available"].component == "binary_sensor"
    assert by_key["rain_available"].device_class == "connectivity"
    assert by_key["rain_available"].entity_category == "diagnostic"


def test_rain_entities_use_mm_unit_when_metric() -> None:
    entities = _rain_entities(_config(units="metric"))
    by_key = {entity.key: entity for entity in entities}
    assert by_key["rain_forecast_today"].unit == "mm"


def test_entity_specs_includes_rain_only_when_active() -> None:
    # Fix 2: _entity_specs also always includes _ALWAYS_ENTITIES
    # ("connected" -- never gated by any source), on top of whatever else is
    # active here -- see tests/test_wiring_lightning.py's own
    # _ALWAYS_ENTITIES coverage for the dedicated tests.
    config = _config()
    assert {e.key for e in _entity_specs(config, lightning_active=False)} == {
        e.key for e in _ENTITIES
    } | {e.key for e in _ALWAYS_ENTITIES}
    with_rain = {e.key for e in _entity_specs(config, lightning_active=False, rain_active=True)}
    assert with_rain == {e.key for e in _ENTITIES} | {e.key for e in _ALWAYS_ENTITIES} | {
        e.key for e in _rain_entities(config)
    }


# --- Supervisor: registration + startup log lines --------------------------------


def test_supervisor_start_registers_rain_entities_when_active(tmp_path: Path, monkeypatch) -> None:
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    publisher = FakePublisher()
    supervisor = _rain_supervisor(
        tmp_path,
        rain_source=FakeRainSource(),
        rain_store=FakeRainStore(),
        publisher=publisher,
    )

    supervisor.start()
    supervisor.stop()

    discovered_keys = {e.key for e in publisher.discovery}
    assert discovered_keys == {e.key for e in _ENTITIES} | {e.key for e in _ALWAYS_ENTITIES} | {
        e.key for e in _rain_entities(supervisor.config)
    }


def test_supervisor_start_logs_exact_rain_tracking_started_line(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    supervisor = _rain_supervisor(
        tmp_path, rain_source=FakeRainSource(), rain_store=FakeRainStore()
    )

    with caplog.at_level(logging.INFO):
        supervisor.start()
    supervisor.stop()

    messages = [(r.name, r.levelname, r.getMessage()) for r in caplog.records]
    assert ("stormwatch.sources.rain", "INFO", "Rain tracking started") in messages


def test_supervisor_start_starts_a_single_rain_thread(tmp_path: Path, monkeypatch) -> None:
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    supervisor = _rain_supervisor(
        tmp_path, rain_source=FakeRainSource(), rain_store=FakeRainStore()
    )

    supervisor.start()
    thread_names = [t.name for t in supervisor._threads]
    supervisor.stop()

    assert thread_names.count("stormwatch-rain") == 1


# --- Supervisor: healthz rain fields ----------------------------------------------


def test_status_includes_rain_source_and_last_24h_when_active(tmp_path: Path) -> None:
    store = FakeRainStore(totals={"h24_mm": 12.7, "d7_mm": 25.4})
    source = FakeRainSource(observations=[("2026-08-09T17:00:00+00:00", 0.3)], available=True)
    supervisor = _rain_supervisor(tmp_path, rain_source=source, rain_store=store)
    supervisor._rain.run_obs_cycle(NOW)

    status = supervisor._status()

    assert status["sources"]["rain"] == {"available": True}
    assert status["state"]["rain_last_24h"] == 12.7


def test_status_stays_ok_when_rain_source_unavailable(tmp_path: Path) -> None:
    # Rain is a non-safety, watering-decision feature -- its unavailability
    # must not flip the overall /healthz status to degraded the way NWS/
    # lightning unavailability does.
    source = FakeRainSource(available=False)
    supervisor = _rain_supervisor(
        tmp_path,
        rain_source=source,
        rain_store=FakeRainStore(),
        publisher=FakePublisher(connected=True),
    )

    status = supervisor._status()

    assert status["sources"]["rain"] == {"available": False}
    assert status["status"] == "ok"


# --- disabled mode: no entities, no thread, no healthz section -------------------


def test_disabled_mode_publishes_no_rain_entities_or_thread_or_healthz(
    tmp_path: Path, monkeypatch
) -> None:
    fake_health = _FakeHealthServer()
    monkeypatch.setattr(
        "stormwatch.__main__.start_health_server",
        lambda status_provider, port=8099: fake_health,
    )
    publisher = FakePublisher()
    # No rain_source/rain_store attached at all -- mirrors how a Supervisor
    # built without a blitzortung_client leaves lightning wiring inactive
    # even though RAIN_ENABLED/BLITZORTUNG_ENABLED both default true.
    supervisor = _rain_supervisor(tmp_path, publisher=publisher)

    supervisor.start()
    thread_names = {t.name for t in supervisor._threads}
    supervisor.stop()

    discovered_keys = {e.key for e in publisher.discovery}
    assert discovered_keys == {e.key for e in _ENTITIES} | {e.key for e in _ALWAYS_ENTITIES}
    assert "stormwatch-rain" not in thread_names
    status = supervisor._status()
    assert "rain" not in status["sources"]
    assert "rain_last_24h" not in status["state"]


def test_disabled_mode_ignored_even_when_rain_sources_attached_but_rain_disabled(
    tmp_path: Path,
) -> None:
    # RAIN_ENABLED=false must win even if source/store objects happen to be
    # attached -- the config flag is authoritative, not attachment alone.
    supervisor = _rain_supervisor(
        tmp_path,
        rain_source=FakeRainSource(),
        rain_store=FakeRainStore(),
        rain_enabled=False,
    )

    assert supervisor._rain_active is False


# --- per-iteration exception survival ---------------------------------------------


def test_rain_obs_tick_survives_and_logs_on_unexpected_exception(tmp_path: Path, caplog) -> None:
    supervisor = _rain_supervisor(
        tmp_path, rain_source=FakeRainSource(), rain_store=FakeRainStore()
    )

    def _boom(now):
        raise RuntimeError("boom")

    supervisor._rain.run_obs_cycle = _boom  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR):
        supervisor._rain_obs_tick()  # must not raise

    assert any(
        record.name == "stormwatch" and "rain observation poll failed" in record.getMessage()
        for record in caplog.records
    )


def test_rain_forecast_tick_survives_and_logs_on_unexpected_exception(
    tmp_path: Path, caplog
) -> None:
    supervisor = _rain_supervisor(
        tmp_path, rain_source=FakeRainSource(), rain_store=FakeRainStore()
    )

    def _boom():
        raise RuntimeError("boom")

    supervisor._rain.run_forecast_cycle = _boom  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR):
        supervisor._rain_forecast_tick()  # must not raise

    assert any(
        record.name == "stormwatch" and "rain forecast poll failed" in record.getMessage()
        for record in caplog.records
    )
