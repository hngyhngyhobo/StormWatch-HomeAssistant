"""Tests for stormwatch.state (DESIGN.md §6).

Three zones (CLEAR/WATCH/CLOSED), two thresholds (close_km, watch_km), one
timer (all_clear_minutes) that resets on every qualifying strike. The
all-clear is an event, emitted exactly once on expiry. Hysteresis on the
watch threshold prevents flapping when a storm sits on the boundary.

All tests inject a fake, settable clock -- no real sleeps.
"""

from __future__ import annotations

import pytest

from stormwatch.state import LightningStateMachine

CLOSE_KM = 16.09
WATCH_KM = 40.23
ALL_CLEAR_MINUTES = 30
ALL_CLEAR_SECONDS = ALL_CLEAR_MINUTES * 60
HYSTERESIS_KM = 2.0  # default


class FakeClock:
    """Settable monotonic-like clock for deterministic timer tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _machine(clock: FakeClock, **overrides: object) -> LightningStateMachine:
    kwargs: dict[str, object] = dict(
        close_km=CLOSE_KM,
        watch_km=WATCH_KM,
        all_clear_minutes=ALL_CLEAR_MINUTES,
        clock=clock,
    )
    kwargs.update(overrides)
    return LightningStateMachine(**kwargs)


# --- initial state -----------------------------------------------------------


def test_initial_state_is_clear_with_no_deadline_or_nearest() -> None:
    machine = _machine(FakeClock())

    assert machine.state == "CLEAR"
    assert machine.all_clear_at is None
    assert machine.nearest_recent_km is None


def test_tick_while_clear_is_a_noop() -> None:
    machine = _machine(FakeClock())

    assert machine.tick() == []
    assert machine.state == "CLEAR"


# --- CLEAR -> CLOSED (direct) --------------------------------------------------


def test_clear_plus_close_strike_enters_closed_and_emits_event() -> None:
    clock = FakeClock()
    machine = _machine(clock)

    events = machine.on_strike(8.0)

    assert events == ["lightning_close"]
    assert machine.state == "CLOSED"
    assert machine.all_clear_at == pytest.approx(ALL_CLEAR_SECONDS)


def test_second_close_strike_no_duplicate_event_but_deadline_extended() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(8.0)

    clock.advance(600)  # 10 minutes later, well inside the window
    events = machine.on_strike(5.0)

    assert events == []
    assert machine.state == "CLOSED"
    assert machine.all_clear_at == pytest.approx(600 + ALL_CLEAR_SECONDS)


def test_closed_tick_before_deadline_returns_no_events() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(8.0)

    clock.advance(ALL_CLEAR_SECONDS - 1)

    assert machine.tick() == []
    assert machine.state == "CLOSED"


def test_watch_range_strike_while_closed_is_ignored() -> None:
    # CLOSED is the more severe state; a watch-range strike while already
    # CLOSED must not touch the timer or the nearest-strike tracking -- only
    # a close_km strike resets CLOSED's deadline.
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(8.0)  # enters CLOSED, deadline = ALL_CLEAR_SECONDS, nearest = 8.0

    clock.advance(60)
    events = machine.on_strike(30.0)  # watch-range strike while CLOSED

    assert events == []
    assert machine.state == "CLOSED"
    assert machine.all_clear_at == pytest.approx(ALL_CLEAR_SECONDS)
    assert machine.nearest_recent_km == pytest.approx(8.0)


def test_closed_tick_after_deadline_emits_all_clear_exactly_once() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(8.0)

    clock.advance(ALL_CLEAR_SECONDS)

    assert machine.tick() == ["all_clear"]
    assert machine.state == "CLEAR"
    assert machine.all_clear_at is None

    # Exactly once: a further tick with no new strike stays quiet.
    assert machine.tick() == []


# --- CLEAR -> WATCH ------------------------------------------------------------


def test_clear_plus_watch_strike_enters_watch_with_no_event() -> None:
    clock = FakeClock()
    machine = _machine(clock)

    events = machine.on_strike(30.0)

    assert events == []
    assert machine.state == "WATCH"
    assert machine.all_clear_at == pytest.approx(ALL_CLEAR_SECONDS)


# --- WATCH -> CLOSED -------------------------------------------------------------


def test_watch_plus_close_strike_transitions_to_closed_with_event() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(30.0)

    clock.advance(60)
    events = machine.on_strike(8.0)

    assert events == ["lightning_close"]
    assert machine.state == "CLOSED"
    assert machine.all_clear_at == pytest.approx(60 + ALL_CLEAR_SECONDS)


# --- WATCH -> CLEAR (quiet window) ----------------------------------------------


def test_watch_quiet_for_window_emits_all_clear_once() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(30.0)

    clock.advance(ALL_CLEAR_SECONDS)

    assert machine.tick() == ["all_clear"]
    assert machine.state == "CLEAR"
    assert machine.all_clear_at is None
    assert machine.tick() == []


# --- hysteresis on WATCH ---------------------------------------------------------


def test_hysteresis_strike_within_margin_resets_watch_timer() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(30.0)  # enters WATCH, deadline = ALL_CLEAR_SECONDS

    clock.advance(ALL_CLEAR_SECONDS - 10)
    events = machine.on_strike(41.0)  # within watch(40.23) + hysteresis(2.0) = 42.23

    assert events == []
    assert machine.state == "WATCH"
    assert machine.all_clear_at == pytest.approx((ALL_CLEAR_SECONDS - 10) + ALL_CLEAR_SECONDS)

    # Original deadline would have expired here; it didn't because the
    # hysteresis-zone strike reset the timer.
    clock.advance(10)
    assert machine.tick() == []


def test_hysteresis_strike_beyond_margin_does_not_reset_watch_timer() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(30.0)  # enters WATCH, deadline = ALL_CLEAR_SECONDS

    clock.advance(ALL_CLEAR_SECONDS - 10)
    events = machine.on_strike(43.0)  # beyond watch(40.23) + hysteresis(2.0) = 42.23

    assert events == []
    assert machine.state == "WATCH"
    assert machine.nearest_recent_km == pytest.approx(30.0)  # unaffected: not qualifying

    clock.advance(10)
    assert machine.tick() == ["all_clear"]
    assert machine.state == "CLEAR"


def test_strike_at_exact_watch_plus_hysteresis_boundary_while_watch_resets_timer() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(30.0)  # enters WATCH, deadline = ALL_CLEAR_SECONDS

    clock.advance(ALL_CLEAR_SECONDS - 10)
    boundary = WATCH_KM + HYSTERESIS_KM  # 42.23, exact edge of the hysteresis band
    events = machine.on_strike(boundary)

    assert events == []
    assert machine.state == "WATCH"
    assert machine.all_clear_at == pytest.approx((ALL_CLEAR_SECONDS - 10) + ALL_CLEAR_SECONDS)

    # Original deadline would have expired here; it didn't because the
    # exact-boundary strike reset the timer.
    clock.advance(10)
    assert machine.tick() == []


def test_hysteresis_band_strike_while_clear_does_not_promote_to_watch() -> None:
    # The hysteresis band (watch_km, watch_km + hysteresis_km] only resets an
    # already-WATCH timer -- it must never newly promote CLEAR to WATCH.
    machine = _machine(FakeClock())

    events = machine.on_strike(41.0)  # inside the hysteresis band, but state is CLEAR

    assert events == []
    assert machine.state == "CLEAR"
    assert machine.all_clear_at is None
    assert machine.nearest_recent_km is None


def test_strike_beyond_watch_plus_hysteresis_in_clear_stays_clear() -> None:
    clock = FakeClock()
    machine = _machine(clock)

    events = machine.on_strike(43.0)

    assert events == []
    assert machine.state == "CLEAR"
    assert machine.all_clear_at is None
    assert machine.nearest_recent_km is None


# --- boundary inclusivity --------------------------------------------------------


def test_boundary_strike_at_exact_close_radius_enters_closed() -> None:
    machine = _machine(FakeClock())

    assert machine.on_strike(CLOSE_KM) == ["lightning_close"]
    assert machine.state == "CLOSED"


def test_boundary_strike_at_exact_watch_radius_enters_watch() -> None:
    machine = _machine(FakeClock())

    assert machine.on_strike(WATCH_KM) == []
    assert machine.state == "WATCH"


# --- nearest_recent_km -----------------------------------------------------------


def test_nearest_recent_km_tracks_minimum_since_last_clear_window() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    assert machine.nearest_recent_km is None

    machine.on_strike(35.0)
    assert machine.nearest_recent_km == pytest.approx(35.0)

    machine.on_strike(20.0)  # closer, still watch-range
    assert machine.nearest_recent_km == pytest.approx(20.0)

    machine.on_strike(8.0)  # enters CLOSED, closer still
    assert machine.nearest_recent_km == pytest.approx(8.0)

    machine.on_strike(12.0)  # farther, must not move the minimum back up
    assert machine.nearest_recent_km == pytest.approx(8.0)

    clock.advance(ALL_CLEAR_SECONDS)
    machine.tick()

    assert machine.state == "CLEAR"
    assert machine.nearest_recent_km is None  # fresh window after all-clear


# --- restart_timer (Fix 1: stale-feed handling, caller-driven) -------------------
#
# LightningWiring (the caller) uses this to guarantee that an all-clear only
# ever follows a full window of LIVE no-strike data: after a stale-feed
# interruption, merely resuming tick() calls would let whatever was left of
# the *old* deadline fire almost immediately. restart_timer() gives the
# caller a way to set a fresh full window from "now" instead, without state.py
# itself knowing anything about feed availability (it stays pure/caller-driven
# per the module docstring).


def test_restart_timer_resets_closed_deadline_to_a_fresh_full_window() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(8.0)  # CLOSED, deadline = ALL_CLEAR_SECONDS
    clock.advance(ALL_CLEAR_SECONDS - 5)  # nearly expired

    machine.restart_timer()

    assert machine.state == "CLOSED"
    assert machine.all_clear_at == pytest.approx((ALL_CLEAR_SECONDS - 5) + ALL_CLEAR_SECONDS)


def test_restart_timer_resets_watch_deadline_to_a_fresh_full_window() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(30.0)  # WATCH, deadline = ALL_CLEAR_SECONDS
    clock.advance(ALL_CLEAR_SECONDS - 5)

    machine.restart_timer()

    assert machine.state == "WATCH"
    assert machine.all_clear_at == pytest.approx((ALL_CLEAR_SECONDS - 5) + ALL_CLEAR_SECONDS)


def test_restart_timer_does_not_shorten_or_skip_the_window() -> None:
    # The whole point: after restart_timer(), a full ALL_CLEAR_SECONDS must
    # elapse (from the restart point) before tick() fires all_clear -- not
    # whatever was left of the old deadline.
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(8.0)
    clock.advance(ALL_CLEAR_SECONDS - 1)  # old deadline is 1 second away

    machine.restart_timer()

    clock.advance(ALL_CLEAR_SECONDS - 1)  # would have fired under the old deadline
    assert machine.tick() == []
    assert machine.state == "CLOSED"

    clock.advance(1)
    assert machine.tick() == ["all_clear"]
    assert machine.state == "CLEAR"


def test_restart_timer_is_a_noop_when_clear() -> None:
    machine = _machine(FakeClock())

    machine.restart_timer()  # must not raise

    assert machine.state == "CLEAR"
    assert machine.all_clear_at is None


def test_restart_timer_is_a_noop_after_all_clear_already_fired() -> None:
    clock = FakeClock()
    machine = _machine(clock)
    machine.on_strike(8.0)
    clock.advance(ALL_CLEAR_SECONDS)
    machine.tick()  # -> CLEAR, all_clear_at = None
    assert machine.state == "CLEAR"

    machine.restart_timer()

    assert machine.state == "CLEAR"
    assert machine.all_clear_at is None
