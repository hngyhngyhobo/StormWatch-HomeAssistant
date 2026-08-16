"""Rolling buffer of recent lightning strikes + a GeoJSON builder.

Pure logic, no network and no I/O: strikes are pushed in by whatever is
consuming the Blitzortung feed (DESIGN.md §4.1/§6), and this module tracks
only the most recent `window_seconds` of them (bounded additionally by
`max_strikes`) for rendering as an HA map-friendly GeoJSON
FeatureCollection.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from stormwatch.geo import bearing_to_compass

_KM_TO_MILES = 0.621371


@dataclass(frozen=True)
class Strike:
    ts: float  # clock() value at ingest (monotonic seconds) -- for windowing
    lat: float
    lon: float
    distance_km: float
    bearing_deg: float


class StrikeBuffer:
    """Bounded, time-windowed buffer of recent `Strike`s."""

    def __init__(
        self,
        window_seconds: float,
        max_strikes: int = 500,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = window_seconds
        self._max_strikes = max_strikes
        self._clock = clock
        self._strikes: deque[Strike] = deque()

    def add(self, lat: float, lon: float, distance_km: float, bearing_deg: float) -> None:
        self._strikes.append(
            Strike(
                ts=self._clock(),
                lat=lat,
                lon=lon,
                distance_km=distance_km,
                bearing_deg=bearing_deg,
            )
        )
        while len(self._strikes) > self._max_strikes:
            self._strikes.popleft()

    def prune(self) -> bool:
        cutoff = self._clock() - self._window_seconds
        removed = False
        while self._strikes and self._strikes[0].ts < cutoff:
            self._strikes.popleft()
            removed = True
        return removed

    def __len__(self) -> int:
        return len(self._strikes)

    def to_geojson(self, close_km: float, watch_km: float, units: str) -> dict:
        now = self._clock()
        features = []
        for strike in reversed(self._strikes):
            if units == "imperial":
                distance = round(strike.distance_km * _KM_TO_MILES, 1)
            else:
                distance = round(strike.distance_km, 1)

            if strike.distance_km <= close_km:
                range_label = "close"
            elif strike.distance_km <= watch_km:
                range_label = "watch"
            else:
                range_label = "beyond"

            age_seconds = max(0, int(now - strike.ts))

            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [strike.lon, strike.lat],
                    },
                    "properties": {
                        "distance": distance,
                        "bearing_deg": round(strike.bearing_deg, 1),
                        "bearing": bearing_to_compass(strike.bearing_deg),
                        "age_seconds": age_seconds,
                        "range": range_label,
                    },
                }
            )

        return {"type": "FeatureCollection", "features": features}
