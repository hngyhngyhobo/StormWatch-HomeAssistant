"""Container entrypoint and supervisor loop (DESIGN.md §5).

Wires config, sources, rule engine, and publisher together; a crash in one
source must never silently kill the container. ``run_alert_cycle`` is the
one poll-evaluate-publish step, factored out so it can be unit tested with
fakes; ``Supervisor`` just schedules it (and the other background loops) on
daemon threads and owns clean shutdown.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from stormwatch import __version__
from stormwatch.config import Config, ConfigError, load_config
from stormwatch.geo import bearing_to_compass, cells_for_radius
from stormwatch.health import HealthServer, start_health_server
from stormwatch.location import resolve_location
from stormwatch.publisher import EntitySpec, Publisher
from stormwatch.rules import AlertTracker, RuleEngine, priority_rank
from stormwatch.sources.blitzortung import BlitzortungClient
from stormwatch.sources.nws import NwsPoller
from stormwatch.sources.rain import RainSource, RainStore
from stormwatch.state import LightningStateMachine
from stormwatch.strikes import StrikeBuffer

if TYPE_CHECKING:
    from stormwatch.rules import AlertEvent

logger = logging.getLogger("stormwatch")
_nws_logger = logging.getLogger("stormwatch.sources.nws")
_blitzortung_logger = logging.getLogger("stormwatch.sources.blitzortung")
_rain_logger = logging.getLogger("stormwatch.sources.rain")

_RECONNECT_INITIAL_SECONDS = 5.0
_RECONNECT_MAX_SECONDS = 60.0
_RULES_RECHECK_SECONDS = 30
_CONNECTED_IDLE_SECONDS = 1.0
_ALERTS_YAML_FILENAME = "alerts.yaml"
_RAIN_HISTORY_FILENAME = "rain_history.json"
_LOCATION_CACHE_FILENAME = "location.json"
_HEALTH_PORT = 8099
_LIGHTNING_TICK_SECONDS = 1.0
_STRIKE_WINDOW_SECONDS = 900.0  # 15 minutes, for strike_count_15m
_REPUBLISH_EVERY_TICKS = 60  # ~60s at _LIGHTNING_TICK_SECONDS, retained-store-loss resilience
_KM_PER_MILE = 1.609344  # mirrors config.py's UNITS<->km conversion factor
_MM_PER_INCH = 25.4
# Watering-flag thresholds (mm, unit-agnostic internally). "Recent"/"imminent"
# gate (0.1 in) matches the watering-reminder automation; the weekly gate
# (0.5 in) keeps the flag OFF when a storm earlier in the week already watered.
_WATERING_RECENT_MM = 2.54
_WATERING_WEEK_MM = 12.7


def _format_rain_mm(mm: float, units: str) -> str:
    """Render a millimetre rainfall value as an MQTT state payload string,
    in the configured display units: inches at 2dp (imperial) or mm at 1dp
    (metric). Returns a formatted *string*, not a bare float --
    ``Publisher._format_value`` rounds every float state to 1dp uniformly,
    which would silently throw away the 2nd decimal place imperial rain
    values need (0.50in vs 0.51in matters for a watering decision)."""
    if units == "imperial":
        return f"{mm / _MM_PER_INCH:.2f}"
    return f"{mm:.1f}"


_ALWAYS_ENTITIES = (
    # binary_sensor.stormwatch_connected (DESIGN.md §8): the headline
    # connectivity entity -- not gated by NWS/lightning/rain being active
    # (unlike nws_available/lightning_available/rain_available, which are
    # each diagnostic and each gated by their own feature), and NOT
    # diagnostic itself. Its state is published by
    # Supervisor._connect_loop_iteration() on every successful MQTT connect
    # (initial + reconnects); HA shows it unavailable (not just OFF) via the
    # shared LWT availability topic once the container actually dies -- see
    # docs/TROUBLESHOOTING.md#unavailable-entities.
    EntitySpec(
        key="connected",
        name="Connected",
        component="binary_sensor",
        device_class="connectivity",
    ),
)

_ENTITIES = (
    EntitySpec(
        key="active_alerts",
        name="Active Alerts",
        component="sensor",
        state_class="measurement",
        value_is_json_attr=True,
    ),
    EntitySpec(
        key="highest_alert",
        name="Highest Alert",
        component="sensor",
        value_is_json_attr=True,
    ),
    EntitySpec(
        key="critical_alert",
        name="Critical Alert",
        component="binary_sensor",
        device_class="safety",
    ),
    EntitySpec(
        key="config_problem",
        name="Config problem",
        component="binary_sensor",
        device_class="problem",
        entity_category="diagnostic",
        value_is_json_attr=True,
    ),
    EntitySpec(
        key="nws_available",
        name="NWS Available",
        component="binary_sensor",
        device_class="connectivity",
        entity_category="diagnostic",
    ),
)


def _lightning_entities(config: Config) -> tuple[EntitySpec, ...]:
    """The pool/lightning entity set (DESIGN.md §8) -- only registered when
    Blitzortung wiring is actually active; see ``Supervisor._lightning_active``.
    """
    distance_unit = "mi" if config.units == "imperial" else "km"
    return (
        EntitySpec(
            key="swim_status",
            name="Swim Status",
            component="sensor",
            icon="mdi:pool",
        ),
        EntitySpec(
            key="nearest_strike_distance",
            name="Nearest Strike Distance",
            component="sensor",
            unit=distance_unit,
        ),
        EntitySpec(
            key="nearest_strike_bearing",
            name="Nearest Strike Bearing",
            component="sensor",
            value_is_json_attr=True,
        ),
        EntitySpec(
            key="strike_count_15m",
            name="Strike Count (15m)",
            component="sensor",
            state_class="measurement",
        ),
        EntitySpec(
            key="all_clear_at",
            name="All Clear At",
            component="sensor",
            device_class="timestamp",
        ),
        EntitySpec(
            key="lightning_nearby",
            name="Lightning Nearby",
            component="binary_sensor",
            device_class="safety",
        ),
        EntitySpec(
            key="lightning_available",
            name="Lightning Available",
            component="binary_sensor",
            device_class="connectivity",
            entity_category="diagnostic",
        ),
        EntitySpec(
            key="strikes",
            name="Lightning Strikes",
            component="sensor",
            state_class="measurement",
            icon="mdi:flash",
            value_is_json_attr=True,
        ),
    )


def _rain_entities(config: Config) -> tuple[EntitySpec, ...]:
    """The rainfall/watering entity set (DESIGN.md §8, task D) -- only
    registered when rain wiring is actually active; see
    ``Supervisor._rain_active``."""
    unit = "in" if config.units == "imperial" else "mm"
    return (
        EntitySpec(
            key="rain_forecast_today",
            name="Rain Forecast Today",
            component="sensor",
            unit=unit,
            state_class="measurement",
            icon="mdi:weather-rainy",
        ),
        EntitySpec(
            key="rain_forecast_48h",
            name="Rain Forecast 48h",
            component="sensor",
            unit=unit,
            state_class="measurement",
            icon="mdi:weather-rainy",
        ),
        EntitySpec(
            key="rain_last_24h",
            name="Rain Last 24h",
            component="sensor",
            unit=unit,
            state_class="measurement",
            icon="mdi:water",
            value_is_json_attr=True,
        ),
        EntitySpec(
            key="rain_last_7d",
            name="Rain Last 7d",
            component="sensor",
            unit=unit,
            state_class="measurement",
            icon="mdi:water",
        ),
        EntitySpec(
            key="rain_available",
            name="Rain Available",
            component="binary_sensor",
            device_class="connectivity",
            entity_category="diagnostic",
        ),
        EntitySpec(
            key="watering_needed",
            name="Watering Needed",
            component="binary_sensor",
            icon="mdi:watering-can",
        ),
    )


def _entity_specs(
    config: Config, lightning_active: bool, nws_active: bool = True, rain_active: bool = False
) -> list[EntitySpec]:
    """All entities this Supervisor instance should register: the always-on
    set (``_ALWAYS_ENTITIES`` -- "connected", never gated -- registered in
    every mode, including everything disabled), plus the NWS alert set
    (``_ENTITIES`` -- active_alerts, highest_alert, critical_alert,
    nws_available, config_problem) when NWS wiring is active, plus the
    lightning/pool set when Blitzortung wiring is active, plus the rain set
    when rain wiring is active, for this run. A lightning-only or rain-only
    deployment (``NWS_ENABLED=false``) must not register NWS-only entities
    it will never publish a real value for."""
    specs: list[EntitySpec] = list(_ALWAYS_ENTITIES)
    if nws_active:
        specs.extend(_ENTITIES)
    if lightning_active:
        specs.extend(_lightning_entities(config))
    if rain_active:
        specs.extend(_rain_entities(config))
    return specs


class LightningWiring:
    """Strike ingestion -> state machine -> Home Assistant publish for the
    pool/lightning feature (DESIGN.md §6, §8).

    Deliberately decoupled from ``BlitzortungClient`` and from
    ``Supervisor``'s other loops so it is directly unit-testable with fakes:
    ``on_strike`` is BlitzortungClient's callback (invoked on paho's own
    network thread), and ``tick`` is driven once a second by Supervisor's
    ticker thread -- both are lock-guarded since they mutate the same
    state. Publishes go through the injected Publisher only on change (a
    small last-published cache), except the ``lightning_available``
    heartbeat, which republishes every tick regardless so its retained
    value's timestamp stays fresh. Additionally, every entity is force-
    republished (bypassing that cache, but still updating it) once every
    ``_REPUBLISH_EVERY_TICKS`` ticks (~60s) -- resilience against the
    broker's retained store getting wiped (e.g. a Mosquitto restart with a
    fresh persistence file), which would otherwise leave a freshly
    (re)subscribed Home Assistant with no value until something changes.
    """

    def __init__(
        self,
        config: Config,
        publisher: Publisher,
        state_machine: LightningStateMachine | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._publisher = publisher
        self._clock = clock
        self._state = (
            state_machine
            if state_machine is not None
            else LightningStateMachine(
                config.close_radius_km,
                config.watch_radius_km,
                config.all_clear_minutes,
                clock=clock,
            )
        )
        self._lock = threading.Lock()
        self._strike_times: deque[float] = deque()
        self._strike_buffer = StrikeBuffer(config.strike_map_window_minutes * 60, clock=clock)
        # True (not False): the very first tick() must publish an initial
        # "strikes" value (count 0, empty geojson) even with zero strikes so
        # far -- otherwise sensor.stormwatch_lightning_strikes sits at HA's "unknown"
        # until either a real strike arrives or the 60s force-republish.
        self._strikes_dirty = True
        self._last_bearing_deg: float | None = None
        self._published: dict[str, tuple[object, str | None]] = {}
        self._tick_count = 0
        # Optimistic default (see _handle_availability_transition_locked): a
        # fresh wiring hasn't "lost" anything yet, so the first tick() call
        # -- whatever its `available` value -- must never itself look like a
        # recovery-from-loss transition.
        self._available = True

    @property
    def state(self) -> str:
        """Current CLEAR/WATCH/CLOSED state (exposed for /healthz).

        Lock-guarded: read from the healthz thread while ``on_strike``
        (paho's network thread) and ``tick`` (the ticker thread) mutate
        ``self._state`` concurrently.
        """
        with self._lock:
            return self._state.state

    def on_strike(
        self, distance_km: float, bearing_deg: float, age_s: float, lat: float, lon: float
    ) -> None:
        """BlitzortungClient's on_strike callback -- runs on paho's own
        network thread, so everything touching shared state is lock-guarded.

        Retains the strike's position in ``self._strike_buffer`` (for the
        ``strikes`` map entity) and marks it dirty, but deliberately does
        NOT publish the geojson itself -- that only happens from ``tick``,
        throttling republishes to <=1/sec even under a strike burst (see
        the class docstring and ``tick``'s).
        """
        with self._lock:
            self._strike_times.append(self._clock())
            self._strike_buffer.add(lat, lon, distance_km, bearing_deg)
            self._strikes_dirty = True
            self._last_bearing_deg = bearing_deg
            events = self._state.on_strike(distance_km)
            self._publish_state_locked()
        for event in events:
            self._publish_event(event, distance_km, bearing_deg)

    def tick(self, available: bool) -> None:
        """One second of the ticker thread: prune the strike-count ring
        buffer, advance the all-clear timer (unless the feed is down --
        SAFETY, see ``_handle_availability_transition_locked``), and
        republish anything that changed (plus the availability heartbeat,
        always). Every ``_REPUBLISH_EVERY_TICKS`` ticks, force-republishes
        every entity regardless of change, for retained-store-loss
        resilience (see the class docstring).

        Stale-feed handling (DESIGN.md §6 "stale data is never clear"): while
        ``available`` is False, the state machine's ``tick()`` is skipped
        entirely -- state and all_clear_at hold at their last values, so a
        dead feed mid-storm can never let the all-clear timer expire on
        silence alone. On recovery (the False->True transition), a not-CLEAR
        state gets its timer *reset* to a fresh full window rather than
        simply resumed -- otherwise whatever was left of the pre-outage
        monotonic deadline (or one already in the past after a long outage)
        would fire almost immediately, i.e. an all-clear must only ever
        follow a full window of LIVE, no-strike data.
        """
        with self._lock:
            self._prune_locked()
            if self._strike_buffer.prune():
                self._strikes_dirty = True
            self._handle_availability_transition_locked(available)
            events: list[str] = []
            if available:
                events = self._state.tick()
                if events:
                    # all_clear: the nearest strike/bearing no longer means
                    # anything, mirroring the state machine's own
                    # nearest_recent_km reset.
                    self._last_bearing_deg = None
            self._tick_count += 1
            force = self._tick_count % _REPUBLISH_EVERY_TICKS == 0
            self._publish_state_locked(force=force)
            self._publish_strikes_locked(force=force)
        self._publisher.publish_state("lightning_available", available)
        for event in events:
            self._publish_event(event, None, None)

    # -- internals ----------------------------------------------------------

    def _handle_availability_transition_locked(self, available: bool) -> None:
        """Detect an availability edge and act on it (SAFETY, see ``tick``'s
        docstring); always updates ``self._available`` for the next call.

        True->False (feed lost): nothing to *do* to the state machine (the
        caller in ``tick`` simply skips this cycle's ``state.tick()`` call)
        -- just log it, and only when it's actually consequential (a
        not-CLEAR state whose timer is now suspended).

        False->True (feed restored): a not-CLEAR state's timer is reset to a
        fresh full window via ``state.restart_timer()`` (itself a no-op when
        CLEAR) so a stale-feed interruption can never shortcut the all-clear
        window; logged only when a restart actually happened.
        """
        if available and not self._available:
            if self._state.state != "CLEAR":
                self._state.restart_timer()
                logger.info(
                    "Lightning feed restored — all-clear timer restarted (%s)",
                    self._state.state,
                )
        elif not available and self._available:
            if self._state.state != "CLEAR":
                logger.warning(
                    "Lightning feed lost — holding state %s, all-clear timer suspended",
                    self._state.state,
                )
        self._available = available

    def _prune_locked(self) -> None:
        cutoff = self._clock() - _STRIKE_WINDOW_SECONDS
        while self._strike_times and self._strike_times[0] < cutoff:
            self._strike_times.popleft()

    def _publish_state_locked(self, force: bool = False) -> None:
        self._publish_if_changed("swim_status", self._state.state, force=force)
        self._publish_if_changed("nearest_strike_distance", self._distance_value(), force=force)
        bearing_value, bearing_attrs = self._bearing_value()
        self._publish_if_changed(
            "nearest_strike_bearing", bearing_value, bearing_attrs, force=force
        )
        self._publish_if_changed("strike_count_15m", len(self._strike_times), force=force)
        self._publish_if_changed("all_clear_at", self._all_clear_at_iso(), force=force)
        self._publish_if_changed("lightning_nearby", self._state.state == "CLOSED", force=force)

    def _publish_strikes_locked(self, force: bool = False) -> None:
        """Publish ``strikes`` (count + geojson attrs) only when something
        actually changed (a new strike arrived, or pruning dropped one) or
        the periodic force-republish is happening -- unlike
        ``_publish_if_changed``'s per-key cache, this uses the dedicated
        ``self._strikes_dirty`` flag set by ``on_strike``/``tick``'s own
        prune, since the geojson blob itself is too large/variable to
        usefully compare for equality on every tick."""
        if not (self._strikes_dirty or force):
            return
        self._publisher.publish_state(
            "strikes",
            len(self._strike_buffer),
            attrs={
                "geojson": self._strike_buffer.to_geojson(
                    self._config.close_radius_km,
                    self._config.watch_radius_km,
                    self._config.units,
                )
            },
        )
        self._strikes_dirty = False

    def _distance_value(self) -> float | str:
        # Literal "None" (not "" and not skipping the publish): Home
        # Assistant's MQTT sensor treats a literal "None" state payload as a
        # reset to `unknown` (its built-in convention, not something
        # StormWatch has to implement) -- exactly the "no current value"
        # semantics wanted here, without the retained-delete side effect an
        # empty-string payload would have (see _all_clear_at_iso below for
        # that distinction spelled out). Every other "no current value"
        # sensor payload in this module (nearest_strike_bearing,
        # all_clear_at, rain_forecast_*/rain_last_* on a failed poll,
        # highest_alert when nothing matches) follows this same convention.
        # Confirmed against a real Home Assistant instance at C-milestone
        # acceptance, not just unit-tested against the literal string.
        km = self._state.nearest_recent_km
        if km is None:
            return "None"
        if self._config.units == "imperial":
            return round(km / _KM_PER_MILE, 1)
        return round(km, 1)

    def _bearing_value(self) -> tuple[str, dict]:
        if self._last_bearing_deg is None:
            return "None", {"degrees": None}
        return bearing_to_compass(self._last_bearing_deg), {"degrees": self._last_bearing_deg}

    def _all_clear_at_iso(self) -> str:
        deadline = self._state.all_clear_at
        if deadline is None:
            # Literal "None", not "": an empty-string MQTT payload is a
            # retained-delete (wipes the entity's retained value), whereas
            # "None" matches the nearest_strike_distance/bearing convention
            # for "no current value" (see _distance_value/_bearing_value).
            return "None"
        remaining_seconds = deadline - self._clock()
        # Whole-second precision: rounds away the sub-second skew between
        # reading the monotonic clock and datetime.now(UTC) a moment later,
        # so back-to-back calls with an unchanged deadline publish an
        # identical string (the change-suppression cache actually suppresses).
        wall_clock = (datetime.now(UTC) + timedelta(seconds=remaining_seconds)).replace(
            microsecond=0
        )
        return wall_clock.isoformat()

    def _publish_if_changed(
        self, key: str, value: object, attrs: dict | None = None, force: bool = False
    ) -> None:
        attrs_key = json.dumps(attrs, sort_keys=True) if attrs is not None else None
        cache_key = (value, attrs_key)
        if not force and self._published.get(key) == cache_key:
            return
        self._published[key] = cache_key
        self._publisher.publish_state(key, value, attrs)

    def _publish_event(
        self, name: str, distance_km: float | None, bearing_deg: float | None
    ) -> None:
        payload: dict[str, object] = {}
        if distance_km is not None:
            payload["distance_km"] = round(distance_km, 2)
        if bearing_deg is not None:
            payload["bearing_deg"] = round(bearing_deg, 1)
            payload["bearing_compass"] = bearing_to_compass(bearing_deg)
        self._publisher.publish_event(name, payload)


class RainWiring:
    """Rain-source poll -> ``RainStore`` ingest -> Home Assistant publish
    for the rainfall/watering feature (DESIGN.md §4, §8, task D2).

    Two independent poll cadences -- forecast (hourly by default) and
    observations (15 minutes by default) -- share one Supervisor thread via
    due-time tracking (``Supervisor._rain_loop``) rather than two threads;
    ``run_forecast_cycle``/``run_obs_cycle`` are the two pure(-ish) per-poll
    steps this wraps, directly unit-testable with fake
    RainSource/RainStore/Publisher -- mirroring ``run_alert_cycle``'s role
    for NWS. Unlike ``run_alert_cycle`` (which leaves stale alert state
    untouched on a failed poll), a failed rain poll actively republishes
    the literal string ``"None"`` to its affected sensors plus
    ``rain_available`` OFF -- project convention for this feature (task D2
    brief) so a stale forecast/total is never mistaken for a fresh one.

    Only ever driven by the single ``stormwatch-rain`` thread, so -- unlike
    ``LightningWiring`` (driven concurrently by paho's network thread and
    the 1s ticker thread) -- no locking is needed here.
    """

    def __init__(
        self,
        config: Config,
        source: RainSource,
        store: RainStore,
        publisher: Publisher,
    ) -> None:
        self._config = config
        self._source = source
        self._store = store
        self._publisher = publisher
        self._station_logged = False
        self.last_24h_mm: float | None = None
        self.last_7d_mm: float | None = None
        self.last_forecast_48h_mm: float | None = None

    def run_forecast_cycle(self) -> dict | None:
        """One gridpoint QPF poll -> publish cycle: ``rain_forecast_today``
        and ``rain_forecast_48h`` (each converted to the configured display
        units), plus ``rain_available`` (always)."""
        result = self._source.poll_forecast()
        if result is None:
            self.last_forecast_48h_mm = None
            self._publisher.publish_state("rain_forecast_today", "None")
            self._publisher.publish_state("rain_forecast_48h", "None")
        else:
            self.last_forecast_48h_mm = result["h48_mm"]
            self._publisher.publish_state(
                "rain_forecast_today",
                _format_rain_mm(result["today_mm"], self._config.units),
            )
            self._publisher.publish_state(
                "rain_forecast_48h",
                _format_rain_mm(result["h48_mm"], self._config.units),
            )
        self._publish_available()
        self._publish_watering_needed()
        return result

    def run_obs_cycle(self, now: datetime) -> dict | None:
        """One observation-station poll -> ingest -> publish cycle:
        ingests newly observed hourly buckets into the store, then
        publishes ``rain_last_24h`` (attrs: the last 24 hourly buckets) and
        ``rain_last_7d`` from the store's rolling totals as of ``now``,
        plus ``rain_available`` (always). Also logs the "Using observation
        station <id>" line exactly once, the first cycle after the
        underlying RainSource resolves which station it's using."""
        buckets = self._source.poll_observations()
        if buckets is None:
            self._publisher.publish_state("rain_last_24h", "None")
            self._publisher.publish_state("rain_last_7d", "None")
            self.last_24h_mm = None
            self.last_7d_mm = None
            self._publish_available()
            self._publish_watering_needed()
            return None

        self._maybe_log_station()
        self._store.ingest(buckets)
        totals = self._store.totals(now)
        hourly = self._store.hourly_24(now)
        self.last_24h_mm = totals["h24_mm"]
        self.last_7d_mm = totals["d7_mm"]
        self._publisher.publish_state(
            "rain_last_24h",
            _format_rain_mm(totals["h24_mm"], self._config.units),
            attrs=hourly,
        )
        self._publisher.publish_state(
            "rain_last_7d", _format_rain_mm(totals["d7_mm"], self._config.units)
        )
        self._publish_available()
        self._publish_watering_needed()
        return totals

    def _publish_available(self) -> None:
        self._publisher.publish_state("rain_available", self._source.available)

    def _publish_watering_needed(self) -> None:
        """Publish the derived watering flag. It draws on values cached by BOTH
        poll cycles (24h/7d observed from obs, 48h forecast), so it is
        republished at the end of each cycle; until both have run at least once
        it stays OFF (see _watering_needed)."""
        self._publisher.publish_state("watering_needed", self._watering_needed())

    def _watering_needed(self) -> bool:
        """True when watering is warranted: last-24h observed AND next-48h
        forecast are both under _WATERING_RECENT_MM (~0.1 in) AND the 7-day
        observed total is under _WATERING_WEEK_MM (~0.5 in). Any missing input
        (a cycle that hasn't run yet or whose poll failed) yields False -- the
        flag asserts a need only when it is sure, never from incomplete data.
        Rain is a non-safety feature, so 'unknown' collapses to 'no need to
        water', not a guess."""
        v24, v48, v7 = self.last_24h_mm, self.last_forecast_48h_mm, self.last_7d_mm
        if v24 is None or v48 is None or v7 is None:
            return False
        return v24 < _WATERING_RECENT_MM and v48 < _WATERING_RECENT_MM and v7 < _WATERING_WEEK_MM

    def _maybe_log_station(self) -> None:
        # RainSource resolves its observation station lazily (station
        # discovery needs its own successful poll) and doesn't expose it as
        # a public attribute -- sources/rain.py is out of scope for this
        # task, so this reads the private cache directly rather than adding
        # a public accessor there.
        station_id = getattr(self._source, "_station_id", None)
        if station_id and not self._station_logged:
            _rain_logger.info("Using observation station %s", station_id)
            self._station_logged = True


def _alert_properties(alert: dict) -> dict:
    return (alert.get("properties") or {}) if isinstance(alert, dict) else {}


def _event_payload(event: AlertEvent) -> dict:
    return {
        "priority": event.priority,
        "id": event.alert_id,
        "headline": event.headline,
        "event": event.event,
        "description": event.description,
        "severity": event.severity,
        "urgency": event.urgency,
        "certainty": event.certainty,
    }


def run_alert_cycle(
    poller: NwsPoller,
    tracker: AlertTracker,
    engine: RuleEngine,
    publisher: Publisher,
    now: datetime,
) -> int | None:
    """One NWS poll -> rule-match -> publish cycle.

    Every collaborator is injected (duck-typed), so this is the one function
    that needs no threads, sockets, or real broker to unit test — it is also
    exactly what both the NWS poll thread and ``Supervisor.poll_now()`` call,
    so a triggered-by-timer cycle and a test-triggered cycle behave
    identically.

    Always publishes 'nws_available' (poller.available) and 'config_problem'
    (True means "there IS a problem" — HA's device_class=problem convention
    — i.e. engine.last_error is not None; attrs carry that last_error text).

    When the poll succeeds, also publishes 'active_alerts' (the raw NWS
    alert count for the point, regardless of whether any configured rule
    matches it — "what's active near me right now"; attrs list every alert's
    id/event/headline/severity/urgency/certainty/expires, straight from NWS
    properties, "" when a property is absent), 'highest_alert' (the event
    name of the highest-priority *rule-matched* alert, ranked via
    rules.priority_rank; "None" when nothing matches), and 'critical_alert'
    (ON if any matched alert is 'critical'). Forwards every tracker.diff()
    issued/cleared transition as a stormwatch/event/alert_<kind> publish
    (payload: priority/id/headline/event/description/severity/urgency/
    certainty).

    Returns the raw active-alert count on a successful poll, or None when
    the poll failed — in which case the alert-derived states above are left
    untouched (we have no fresh data to publish) while availability/config
    state is still refreshed.
    """
    current = poller.poll_once()
    active_count: int | None = None

    if current is not None:
        active_count = len(current)

        for event in tracker.diff(current, engine, now):
            publisher.publish_event(f"alert_{event.kind}", _event_payload(event))

        alerts_summary: list[dict] = []
        matched: list[dict] = []
        for alert in current:
            properties = _alert_properties(alert)
            alerts_summary.append(
                {
                    "id": properties.get("id", ""),
                    "event": properties.get("event", ""),
                    "headline": properties.get("headline", ""),
                    "severity": properties.get("severity", ""),
                    "urgency": properties.get("urgency", ""),
                    "certainty": properties.get("certainty", ""),
                    "expires": properties.get("expires", ""),
                }
            )
            priority = engine.evaluate(alert)
            if priority is not None:
                matched.append(
                    {
                        "priority": priority,
                        "event": properties.get("event", ""),
                        "headline": properties.get("headline", ""),
                        "description": properties.get("description", ""),
                    }
                )

        publisher.publish_state("active_alerts", active_count, attrs={"alerts": alerts_summary})

        if matched:
            highest = min(matched, key=lambda a: priority_rank(a["priority"]))
            publisher.publish_state(
                "highest_alert",
                highest["event"],
                attrs={"headline": highest["headline"], "description": highest["description"]},
            )
        else:
            publisher.publish_state(
                "highest_alert", "None", attrs={"headline": "", "description": ""}
            )

        critical_active = any(a["priority"] == "critical" for a in matched)
        publisher.publish_state("critical_alert", critical_active)

    publisher.publish_state("nws_available", poller.available)
    publisher.publish_state(
        "config_problem",
        engine.last_error is not None,
        attrs={"last_error": engine.last_error},
    )

    return active_count


def _maybe_reconnect(publisher: Publisher, backoff_seconds: float) -> float:
    """One step of the MQTT reconnect loop.

    ``Publisher`` is built with ``reconnect_on_failure=False``, so nothing
    reconnects it automatically — this is that logic. If already connected,
    returns the floor (so a later drop starts backing off from 5s again). If
    not, attempts one ``connect()`` call (swallowing any exception it
    raises — an unreachable broker must never kill this thread) and returns
    the next backoff: the floor if that attempt already flipped
    ``connected`` true, otherwise double the previous backoff capped at 60s.
    """
    if publisher.connected:
        return _RECONNECT_INITIAL_SECONDS
    try:
        publisher.connect()
    except Exception:
        logger.exception("MQTT connect failed")
    if publisher.connected:
        return _RECONNECT_INITIAL_SECONDS
    return min(backoff_seconds * 2, _RECONNECT_MAX_SECONDS)


class Supervisor:
    """Owns the background threads and coordinates clean shutdown.

    Threads: MQTT connect/reconnect loop, NWS poll loop (runs
    ``run_alert_cycle`` immediately on start, then every
    ``config.nws_poll_seconds``), alerts.yaml hot-reload loop (mtime poll
    every 30s), and -- only when Blitzortung wiring is active, see
    ``_lightning_active`` -- a 1s lightning ticker loop. All are daemons
    and are additionally joined by ``stop()``. Every collaborator can be
    injected, so tests can swap in fakes/stubs (unit tests) or point a
    real ``NwsPoller``/``Publisher`` at a stub HTTP server / local
    Mosquitto (the E2E test).

    ``blitzortung_client`` is deliberately *not* auto-created here the way
    ``poller``/``publisher`` are: constructing a ``Supervisor`` must never
    by itself risk opening a real connection to blitzortung.ha.sed.pl (a
    volunteer-run broker -- see DESIGN.md §4.1.1), so building and
    attaching the real client is left to ``main()`` (via
    ``_make_blitzortung_client``), *after* this object exists. Lightning
    wiring (entities, ticker thread, the client's own ``start()``/``stop()``,
    and the /healthz lightning fields) only activates when
    ``config.blitzortung_enabled`` AND a client has actually been attached
    -- see ``_lightning_active``. ``rain_source``/``rain_store`` follow the
    same deliberately-not-auto-created rule (see ``_rain_active``) -- even
    though building them doesn't risk an outbound connection the way
    Blitzortung does, a bare ``Supervisor(config)`` must still come up
    identically whether or not RAIN_ENABLED defaults true, for any caller
    that never attached rain wiring in the first place.
    """

    def __init__(
        self,
        config: Config,
        poller: NwsPoller | None = None,
        tracker: AlertTracker | None = None,
        engine: RuleEngine | None = None,
        publisher: Publisher | None = None,
        blitzortung_client: BlitzortungClient | None = None,
        rain_source: RainSource | None = None,
        rain_store: RainStore | None = None,
    ) -> None:
        self.config = config
        self.engine = engine if engine is not None else RuleEngine(config)
        self.tracker = tracker if tracker is not None else AlertTracker()
        self.publisher = publisher if publisher is not None else Publisher(config)
        self.poller = poller if poller is not None else NwsPoller(config)
        self.blitzortung_client = blitzortung_client
        self._lightning = LightningWiring(config, self.publisher)
        self._rain_source: RainSource | None = None
        self._rain_store: RainStore | None = None
        self._rain: RainWiring | None = None
        self.rain_source = rain_source
        self.rain_store = rain_store

        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._health: HealthServer | None = None
        self._rules_path = os.path.join(config.config_dir, _ALERTS_YAML_FILENAME)
        self._rules_mtime: float | None = None
        self._active_alerts_count = 0
        self._mqtt_backoff = _RECONNECT_INITIAL_SECONDS
        self._mqtt_was_connected = False

    @property
    def rain_source(self) -> RainSource | None:
        """The attached RainSource, or None (rain wiring inactive).

        A property (not a plain attribute) so assigning it -- either via
        the constructor or, mirroring ``blitzortung_client``'s
        post-construction attachment in ``main()``, after the fact --
        keeps ``self._rain`` (the actual ``RainWiring`` collaborator)
        in sync with whatever source+store are currently attached."""
        return self._rain_source

    @rain_source.setter
    def rain_source(self, value: RainSource | None) -> None:
        self._rain_source = value
        self._rebuild_rain_wiring()

    @property
    def rain_store(self) -> RainStore | None:
        """The attached RainStore, or None (rain wiring inactive). See
        ``rain_source``'s docstring for why this is a property."""
        return self._rain_store

    @rain_store.setter
    def rain_store(self, value: RainStore | None) -> None:
        self._rain_store = value
        self._rebuild_rain_wiring()

    def _rebuild_rain_wiring(self) -> None:
        if self._rain_source is not None and self._rain_store is not None:
            self._rain = RainWiring(
                self.config, self._rain_source, self._rain_store, self.publisher
            )
        else:
            self._rain = None

    @property
    def _lightning_active(self) -> bool:
        """True once lightning wiring should actually run this instance:
        BLITZORTUNG_ENABLED and a client has been attached (see the class
        docstring). Both conditions are required -- a disabled config wins
        even if a client object happens to be attached anyway."""
        return self.config.blitzortung_enabled and self.blitzortung_client is not None

    @property
    def _nws_active(self) -> bool:
        """True when NWS polling wiring should run for this instance.

        Mirrors ``_lightning_active``'s role as the single gate for a
        feature's threads/entities/healthz fields, but NWS has no
        "attached client" concept to additionally check -- NWS_ENABLED
        alone decides it. A lightning-only or rain-only deployment sets
        this false: no NWS poll thread, no alerts.yaml hot-reload thread
        (nothing evaluates rules against NWS alerts), no NWS entities in
        discovery, and no ``sources.nws`` in /healthz.
        """
        return self.config.nws_enabled

    @property
    def _rain_active(self) -> bool:
        """True once rain wiring should actually run this instance:
        RAIN_ENABLED and a source+store have both been attached (mirrors
        ``_lightning_active``'s "enabled AND attached" gate). A disabled
        config wins even if source/store objects happen to be attached
        anyway."""
        return self.config.rain_enabled and self._rain is not None

    def start(self) -> None:
        """Register entities, connect MQTT, and start every background loop.

        Non-blocking — all loops run on daemon threads; callers (real
        ``main()`` or a test) decide how to wait. The NWS poll loop and the
        alerts.yaml hot-reload loop only start when ``_nws_active`` (see its
        docstring) -- a lightning-only or rain-only deployment
        (``NWS_ENABLED=false``) must come up fully without either.
        """
        logger.info("StormWatch %s starting", __version__)

        self._ensure_alerts_yaml()
        self._reload_rules_if_changed()

        self.publisher.publish_discovery(
            _entity_specs(self.config, self._lightning_active, self._nws_active, self._rain_active)
        )
        self._start_thread(self._connect_loop, "stormwatch-mqtt")

        if self._nws_active:
            _nws_logger.info(
                "NWS poller started (contact: %s, poll interval %ds)",
                self.config.nws_contact,
                self.config.nws_poll_seconds,
            )
            self._start_thread(self._nws_loop, "stormwatch-nws")
            self._start_thread(self._rules_loop, "stormwatch-rules")

        if self._lightning_active:
            self._start_lightning()

        if self._rain_active:
            self._start_rain()

        self._health = start_health_server(self._status, port=_HEALTH_PORT)

    def poll_now(self) -> None:
        """Run one NWS poll/publish cycle immediately.

        Used by the NWS loop's first iteration, and available to tests that
        want a deterministic trigger instead of waiting out
        nws_poll_seconds.
        """
        result = run_alert_cycle(self.poller, self.tracker, self.engine, self.publisher, _now())
        if result is not None:
            self._active_alerts_count = result

    def stop(self) -> None:
        """Signal every loop to exit, join them, and go offline cleanly."""
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=5)
        if self._health is not None:
            self._health.stop()
        if self._lightning_active:
            try:
                self.blitzortung_client.stop()
            except Exception:
                logger.exception("error while disconnecting from Blitzortung during shutdown")
        if self.publisher.connected:
            self.publisher.offline()
        try:
            self.publisher.client.loop_stop()
            self.publisher.client.disconnect()
        except Exception:
            logger.exception("error while disconnecting from MQTT during shutdown")

    # -- setup helpers --------------------------------------------------

    def _ensure_alerts_yaml(self) -> None:
        if not os.path.exists(self._rules_path):
            self.engine.generate_default(self._rules_path)

    def _reload_rules_if_changed(self) -> None:
        try:
            mtime = os.path.getmtime(self._rules_path)
        except OSError:
            return
        if mtime != self._rules_mtime:
            self.engine.load(self._rules_path)
            self._rules_mtime = mtime

    def _start_thread(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    # -- background loops -------------------------------------------------

    def _connect_loop(self) -> None:
        while not self._stop_event.is_set():
            wait_seconds = self._connect_loop_iteration()
            if self._stop_event.wait(wait_seconds):
                break

    def _connect_loop_iteration(self) -> float:
        """One iteration of the MQTT connect/reconnect loop: attempt a
        reconnect if needed (``_maybe_reconnect``), then -- on the
        not-connected -> connected transition (the initial connect, or any
        reconnect after a drop) -- publish the 'connected' entity ON
        (retained; see the ``_ALWAYS_ENTITIES`` EntitySpec and Fix 2's
        docstring there). Factored out of ``_connect_loop`` for direct unit
        testing (mirrors ``_lightning_tick_once``/``_rain_obs_tick``).
        Returns the number of seconds to wait before the next iteration.
        """
        self._mqtt_backoff = _maybe_reconnect(self.publisher, self._mqtt_backoff)
        connected = self.publisher.connected
        if connected and not self._mqtt_was_connected:
            self.publisher.publish_state("connected", True)
        self._mqtt_was_connected = connected
        return _CONNECTED_IDLE_SECONDS if connected else self._mqtt_backoff

    def _nws_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_now()
            except Exception:
                logger.exception("NWS poll cycle failed")
            if self._stop_event.wait(self.config.nws_poll_seconds):
                break

    def _rules_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.wait(_RULES_RECHECK_SECONDS):
                break
            self._rules_loop_tick()

    def _rules_loop_tick(self) -> None:
        """One rules hot-reload check; must never raise (mirrors _nws_loop).

        An unexpected exception here (e.g. UnicodeDecodeError from a
        non-UTF-8 alerts.yaml) must not kill this daemon thread silently and
        permanently.
        """
        try:
            self._reload_rules_if_changed()
        except Exception:
            logger.exception("rules reload check failed")

    def _start_lightning(self) -> None:
        """Start the real Blitzortung client and its 1s ticker thread.

        Only called from ``start()`` when ``_lightning_active`` -- i.e. a
        client has actually been attached (see the class docstring). Logs
        the exact startup line documented in INSTALL-UNRAID.md /
        INSTALL-DOCKER.md, once, before the client connects.
        """
        cells = cells_for_radius(
            self.config.latitude, self.config.longitude, self.config.watch_radius_km
        )
        _blitzortung_logger.info(
            "Blitzortung client started (%d cells, host %s)",
            len(cells),
            self.config.blitzortung_mqtt_host,
        )
        self.blitzortung_client.start()
        self._start_thread(self._lightning_loop, "stormwatch-lightning")

    def _lightning_loop(self) -> None:
        while not self._stop_event.is_set():
            self._lightning_tick_once()
            if self._stop_event.wait(_LIGHTNING_TICK_SECONDS):
                break

    def _lightning_tick_once(self) -> None:
        """One lightning ticker iteration; must never raise (mirrors
        _rules_loop_tick / _nws_loop's per-iteration try/except)."""
        try:
            self._lightning.tick(self.blitzortung_client.available)
        except Exception:
            logger.exception("lightning tick failed")

    def _start_rain(self) -> None:
        """Start the single rain thread (both poll cadences).

        Only called from ``start()`` when ``_rain_active`` -- i.e. a
        source+store have actually been attached (see the class
        docstring). Logs the first of the two documented startup lines
        (task D2 brief); the second ("Using observation station <id>")
        logs once ``RainWiring`` discovers which station it's using -- see
        ``RainWiring._maybe_log_station``.
        """
        _rain_logger.info("Rain tracking started")
        self._start_thread(self._rain_loop, "stormwatch-rain")

    def _rain_loop(self) -> None:
        """Single background thread for both rain poll cadences (DESIGN.md
        §4, task D2): due-time tracking instead of two separate threads.
        Both cadences fire immediately on the first iteration (their due
        times start at "now"), mirroring ``_nws_loop``'s immediate first
        poll; afterwards each fires on its own configured interval
        (``rain_obs_poll_seconds`` / ``rain_forecast_poll_seconds``). Sleeps
        exactly until whichever due time comes next, rather than polling on
        a fixed short tick.
        """
        next_obs_due = time.monotonic()
        next_forecast_due = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_obs_due:
                self._rain_obs_tick()
                next_obs_due = time.monotonic() + self.config.rain_obs_poll_seconds
            if now >= next_forecast_due:
                self._rain_forecast_tick()
                next_forecast_due = time.monotonic() + self.config.rain_forecast_poll_seconds
            wait_seconds = max(min(next_obs_due, next_forecast_due) - time.monotonic(), 0.0)
            if self._stop_event.wait(wait_seconds):
                break

    def _rain_obs_tick(self) -> None:
        """One rain-observation poll/publish iteration; must never raise
        (mirrors _rules_loop_tick / _lightning_tick_once)."""
        try:
            self._rain.run_obs_cycle(_utc_now())
        except Exception:
            logger.exception("rain observation poll failed")

    def _rain_forecast_tick(self) -> None:
        """One rain-forecast poll/publish iteration; must never raise
        (mirrors _rules_loop_tick / _lightning_tick_once)."""
        try:
            self._rain.run_forecast_cycle()
        except Exception:
            logger.exception("rain forecast poll failed")

    def _status(self) -> dict:
        """Build the /healthz payload.

        'status' is 'ok' only when every required *safety-relevant* input
        checks out: the MQTT publisher is connected, the NWS source is
        available (or NWS is disabled entirely, in which case its
        availability is moot), alerts.yaml has no validation error, and --
        when lightning wiring is active -- the Blitzortung client is
        connected. Any one of those failing marks the whole endpoint
        'degraded' -- callers should check the 'sources' and 'config_ok'
        fields (and MQTT connectivity, which isn't broken out separately)
        to see which input is the culprit. Always HTTP 200 either way;
        'degraded' is a body field, not a status code.

        Rain is deliberately *not* one of those safety-relevant inputs:
        it's a watering-decision feature, not a "get out of the water"
        one, so ``sources.rain.available`` being false never flips
        'status' to 'degraded' the way NWS/lightning unavailability does
        (task D2 brief) -- callers that care about rain freshness should
        check ``sources.rain.available`` (and ``state.rain_last_24h``)
        directly instead of relying on the overall 'status' field.

        The 'nws' entry under 'sources' only appears when NWS wiring is
        active (NWS_ENABLED); the 'lightning' entry under 'sources' and
        'swim_status' under 'state' only appear when lightning wiring is
        active (BLITZORTUNG_ENABLED and a client attached); the 'rain'
        entry under 'sources' and 'rain_last_24h' under 'state' only
        appear when rain wiring is active (RAIN_ENABLED and a source+store
        attached) -- disabled, they're simply absent rather than reported
        as unavailable.
        """
        nws_ok = self.poller.available or not self.config.nws_enabled
        config_ok = self.engine.last_error is None
        sources: dict[str, dict] = {}
        if self._nws_active:
            sources["nws"] = {"available": self.poller.available}
        state: dict[str, object] = {"active_alerts": self._active_alerts_count}
        lightning_ok = True
        if self._lightning_active:
            lightning_available = self.blitzortung_client.available
            sources["lightning"] = {"available": lightning_available}
            state["swim_status"] = self._lightning.state
            lightning_ok = lightning_available
        if self._rain_active:
            sources["rain"] = {"available": self.rain_source.available}
            state["rain_last_24h"] = self._rain.last_24h_mm
        overall_ok = self.publisher.connected and nws_ok and config_ok and lightning_ok
        return {
            "status": "ok" if overall_ok else "degraded",
            "sources": sources,
            "state": state,
            "config_ok": config_ok,
            "version": __version__,
        }


def _make_blitzortung_client(config: Config, supervisor: Supervisor) -> BlitzortungClient:
    """Build the real BlitzortungClient for main()'s production wiring.

    The on_strike callback defers its ``supervisor._lightning`` lookup
    until a strike actually arrives, instead of binding
    ``supervisor._lightning.on_strike`` directly -- tests replace
    ``Supervisor`` itself with a bare test double (see
    tests/test_supervisor.py's ``test_main_stops_supervisor_when_shutdown_signal_fires``),
    so *constructing* this client must never eagerly dereference
    attributes that only the real Supervisor carries.
    """

    def _on_strike(
        distance_km: float, bearing_deg: float, age_s: float, lat: float, lon: float
    ) -> None:
        supervisor._lightning.on_strike(distance_km, bearing_deg, age_s, lat, lon)

    return BlitzortungClient(config, _on_strike)


def _now() -> datetime:
    """Local (not UTC) time — AlertTracker needs it for quiet-hours math."""
    return datetime.now().astimezone()


def _utc_now() -> datetime:
    """UTC time — RainStore's bucket keys and totals()/hourly_24() windows
    are UTC (internal units: kilometers + UTC everywhere, DESIGN.md)."""
    return datetime.now(UTC)


def _handle_shutdown_signal(stop_event: threading.Event, signum: int, frame: object) -> None:
    """SIGTERM/SIGINT handler: request the main loop to stop.

    Factored to module level (was an inline lambda closure in ``main()``) so
    it is directly unit-testable without sending a real OS signal.
    """
    logger.info("received signal %s, shutting down", signum)
    stop_event.set()


def main() -> None:
    """Start StormWatch: load config, resolve location, start the supervisor,
    wait for SIGTERM.

    Location resolution (task LOC) happens between config load and
    Supervisor construction: ``resolve_location`` picks explicit
    coordinates, the Atlanta default, a cached geocode, or a fresh geocode
    (see stormwatch.location's module docstring for the full order), and
    the result is folded back into ``config`` via ``dataclasses.replace``
    before anything else touches ``config.latitude``/``config.longitude``.
    A geocode failure with no cache raises ConfigError, handled exactly
    like a bad ``load_config()`` -- log and exit 1 -- so the Supervisor
    itself can keep assuming concrete coordinates are always present.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)

    try:
        latitude, longitude, source = resolve_location(
            config, os.path.join(config.config_dir, _LOCATION_CACHE_FILENAME)
        )
    except ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)
    config = dataclasses.replace(config, latitude=latitude, longitude=longitude)

    if source == "default":
        logger.warning(
            "Using default location Atlanta, GA — set LOCATION or LATITUDE/LONGITUDE for your area"
        )
    elif source in ("geocoded", "cache"):
        logger.info(
            "Location '%s' resolved to %s, %s (%s)", config.location, latitude, longitude, source
        )

    logging.getLogger().setLevel(config.log_level)

    supervisor = Supervisor(config)
    if config.blitzortung_enabled:
        supervisor.blitzortung_client = _make_blitzortung_client(config, supervisor)
    if config.rain_enabled:
        supervisor.rain_source = RainSource(config)
        supervisor.rain_store = RainStore(os.path.join(config.config_dir, _RAIN_HISTORY_FILENAME))
    supervisor.start()

    stop_event = threading.Event()
    handler = functools.partial(_handle_shutdown_signal, stop_event)
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    stop_event.wait()
    supervisor.stop()


if __name__ == "__main__":
    main()
