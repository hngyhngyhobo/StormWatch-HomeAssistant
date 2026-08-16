"""Lightning decision state machine: CLEAR / WATCH / CLOSED with the
30-minute all-clear timer (DESIGN.md §6).

Three zones, two thresholds, one timer:

- A strike inside ``close_km`` closes the pool immediately (from any state)
  and (re)sets the timer to ``all_clear_minutes`` from now.
- A strike inside ``watch_km`` enters WATCH (unless already CLOSED, which is
  the more severe state) and (re)sets its timer the same way.
- The timer resets on every qualifying strike -- it counts down from the
  *last* strike, not the first. That is the whole point of the product.
- Hysteresis widens the WATCH reset threshold to ``watch_km + hysteresis_km``
  so a storm sitting on the watch boundary can't flap the timer in and out
  of relevance; it does not change the CLEAR->WATCH entry threshold.
- The all-clear is an event, emitted exactly once by ``tick()`` when the
  timer expires, dropping the state back to CLEAR.

Stale-feed handling (holding state when the lightning source is
unavailable) is the caller's responsibility -- this class is pure logic
driven by ``on_strike()``/``tick()`` calls; it does no I/O and runs no
threads/timers of its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable

_LIGHTNING_CLOSE = "lightning_close"
_ALL_CLEAR = "all_clear"


class LightningStateMachine:
    """Three zones, two thresholds, one timer."""

    def __init__(
        self,
        close_km: float,
        watch_km: float,
        all_clear_minutes: int,
        hysteresis_km: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._close_km = close_km
        self._watch_km = watch_km
        self._all_clear_seconds = all_clear_minutes * 60
        self._hysteresis_km = hysteresis_km
        self._clock = clock

        self.state: str = "CLEAR"
        self.all_clear_at: float | None = None
        self.nearest_recent_km: float | None = None

    def on_strike(self, distance_km: float) -> list[str]:
        """Ingest a strike and update state. Returns emitted event names."""
        events: list[str] = []
        now = self._clock()

        if distance_km <= self._close_km:
            entering_closed = self.state != "CLOSED"
            self.state = "CLOSED"
            self.all_clear_at = now + self._all_clear_seconds
            self._track_nearest(distance_km)
            if entering_closed:
                events.append(_LIGHTNING_CLOSE)
        elif distance_km <= self._watch_km:
            if self.state != "CLOSED":
                self.state = "WATCH"
                self.all_clear_at = now + self._all_clear_seconds
                self._track_nearest(distance_km)
        elif distance_km <= self._watch_km + self._hysteresis_km:
            # Hysteresis zone: only relevant once already in WATCH -- it
            # resets that timer but never newly promotes CLEAR to WATCH,
            # and never affects CLOSED (which resets only on close_km
            # strikes).
            if self.state == "WATCH":
                self.all_clear_at = now + self._all_clear_seconds
                self._track_nearest(distance_km)
        # else: beyond watch_km + hysteresis_km -- not a qualifying strike.

        return events

    def restart_timer(self) -> None:
        """Reset the all-clear timer to a fresh full window starting now.

        For a caller (``LightningWiring``) that needs to guarantee an
        all-clear only ever follows a full window of LIVE, no-strike data --
        e.g. after a stale-feed interruption, where merely resuming
        ``tick()`` calls would let whatever was left of the *old* deadline
        fire almost immediately. Deliberately dumb: it doesn't know or care
        *why* the caller wants a fresh window, keeping this class free of any
        availability/feed-health concept (that decision belongs to the
        caller -- see the module docstring).

        No-op when CLEAR (``all_clear_at`` is None) -- there is no timer
        running to restart.
        """
        if self.state == "CLEAR" or self.all_clear_at is None:
            return
        self.all_clear_at = self._clock() + self._all_clear_seconds

    def tick(self) -> list[str]:
        """Check the all-clear timer. Returns ['all_clear'] exactly once
        on expiry, else []."""
        if self.state == "CLEAR" or self.all_clear_at is None:
            return []

        if self._clock() >= self.all_clear_at:
            self.state = "CLEAR"
            self.all_clear_at = None
            self.nearest_recent_km = None
            return [_ALL_CLEAR]

        return []

    def _track_nearest(self, distance_km: float) -> None:
        if self.nearest_recent_km is None or distance_km < self.nearest_recent_km:
            self.nearest_recent_km = distance_km
