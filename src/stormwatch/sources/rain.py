"""NWS rainfall forecast + observed accumulation (DESIGN.md §4, watering alerts).

Two free api.weather.gov products, no API key:

- ``RainSource`` — a gridpoint forecast poll (quantitativePrecipitation, the
  QPF layer) for "how much rain is forecast today / in the next 48h", and an
  observation-station poll (``precipitationLastHour``) for "how much rain has
  actually fallen, hour by hour". Both need a one-time ``/points/{lat},{lon}``
  lookup to discover the gridpoint and the observation-station list for the
  configured location; those URLs (and, for observations, the first station
  that actually reports precipitation) are cached for the life of the
  instance so steady-state polling never repeats that lookup.
- ``RainStore`` — a small persistent hour-bucketed history of observed
  accumulation, so 24h/7d totals survive a container restart.

Same User-Agent convention as ``sources/nws.py``: identifies the operator via
their configured contact address, per NWS's API usage policy.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from stormwatch import __version__
from stormwatch.config import Config

logger = logging.getLogger("stormwatch.sources.rain")

_REQUEST_TIMEOUT_SECONDS = 10
_REPO_URL = "https://github.com/hngyhngyhobo/StormWatch-HomeAssistant"
_OBSERVATIONS_LIMIT = 48
_STORE_RETENTION_DAYS = 7

_DURATION_RE = re.compile(r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?$")

_Period = tuple[datetime, datetime, float]


def _parse_duration(duration: str) -> timedelta:
    """Parse a (subset of) ISO8601 duration, e.g. 'PT6H', 'P1DT2H30M'."""
    match = _DURATION_RE.match(duration)
    if not match:
        raise ValueError(f"unsupported ISO8601 duration: {duration!r}")
    parts = match.groupdict()
    return timedelta(
        days=int(parts["days"] or 0),
        hours=int(parts["hours"] or 0),
        minutes=int(parts["minutes"] or 0),
    )


def _parse_valid_time(valid_time: str) -> tuple[datetime, datetime]:
    """Parse a gridpoint 'validTime' of the form '<ISO8601 start>/<duration>'."""
    start_str, duration_str = valid_time.split("/", 1)
    start = datetime.fromisoformat(start_str)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return start, start + _parse_duration(duration_str)


def _sum_overlap(periods: list[_Period], window_start: datetime, window_end: datetime) -> float:
    """Sum period values, splitting proportionally where a period only
    partially overlaps [window_start, window_end)."""
    total = 0.0
    for start, end, value in periods:
        period_seconds = (end - start).total_seconds()
        if period_seconds <= 0:
            continue
        overlap_start = max(start, window_start)
        overlap_end = min(end, window_end)
        if overlap_end <= overlap_start:
            continue
        fraction = (overlap_end - overlap_start).total_seconds() / period_seconds
        total += value * fraction
    return total


def _utc_day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _extract_precip_mm(properties: dict) -> float | None:
    """Pull a millimetre precipitation reading off an observation's
    properties, skipping stations/hours that reported nothing (null)."""
    precip = properties.get("precipitationLastHour") or properties.get("precipitationLast1Hour")
    if not isinstance(precip, dict):
        return None
    value = precip.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bucket_observations(features: list[dict]) -> list[tuple[str, float]]:
    """Bucket observation features by UTC hour, dropping null readings."""
    buckets: dict[str, float] = {}
    for feature in features:
        properties = feature.get("properties") or {}
        precip_mm = _extract_precip_mm(properties)
        if precip_mm is None:
            continue
        timestamp = properties.get("timestamp")
        if not timestamp:
            continue
        try:
            observed_at = datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        hour = observed_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        buckets[hour.isoformat()] = precip_mm
    return sorted(buckets.items())


class RainSource:
    """Polls api.weather.gov for QPF forecast and observed hourly rainfall.

    ``/points/{lat},{lon}`` is resolved once and cached (gridpoint URL +
    observation-station list URL); observation polling additionally
    discovers and caches the first station in that list that actually
    reports a non-null ``precipitationLastHour`` (many ASOS stations report
    the property but never a value). A failed request at any stage clears
    ``available`` and returns ``None``; a completed poll sets it back True.
    """

    def __init__(
        self,
        config: Config,
        session: requests.Session | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._session = session if session is not None else requests.Session()
        self._user_agent = f"StormWatch/{__version__} ({_REPO_URL}, {config.nws_contact})"
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self.available = False

        self._gridpoint_url: str | None = None
        self._stations_url: str | None = None
        self._station_id: str | None = None

    def poll_forecast(self) -> dict | None:
        """Fetch the gridpoint QPF layer and return today's / next-48h mm.

        'Today' is the UTC calendar day (documented v1 simplification —
        see task brief); periods that straddle either boundary are split
        proportionally by the fraction of the period inside the window.
        """
        if not self._ensure_points():
            return None

        response = self._get(self._gridpoint_url)
        if response is None:
            return None

        try:
            qpf_values = response.json()["properties"]["quantitativePrecipitation"]["values"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Rain gridpoint response missing quantitativePrecipitation: %s", exc)
            self.available = False
            return None

        periods: list[_Period] = []
        for entry in qpf_values:
            try:
                start, end = _parse_valid_time(entry["validTime"])
                raw_value = entry["value"]
                value = 0.0 if raw_value is None else float(raw_value)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping malformed QPF period %r: %s", entry, exc)
                continue
            periods.append((start, end, value))

        now = self._clock()
        today_start = _utc_day_start(now)
        today_mm = _sum_overlap(periods, today_start, today_start + timedelta(days=1))
        h48_mm = _sum_overlap(periods, now, now + timedelta(hours=48))

        self.available = True
        return {"today_mm": round(today_mm, 3), "h48_mm": round(h48_mm, 3)}

    def poll_observations(self) -> list[tuple[str, float]] | None:
        """Fetch the last 48 hourly observations from the cached (or newly
        discovered) precipitation-reporting station, bucketed by UTC hour.
        """
        if not self._ensure_points():
            return None

        if self._station_id is None:
            return self._discover_station()

        response = self._get(
            f"{self._config.nws_api_base}/stations/{self._station_id}/observations",
            params={"limit": _OBSERVATIONS_LIMIT},
        )
        if response is None:
            return None

        try:
            features = response.json()["features"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Rain observations response malformed: %s", exc)
            self.available = False
            return None

        buckets = _bucket_observations(features)
        self.available = True
        return buckets

    def _ensure_points(self) -> bool:
        if self._gridpoint_url is not None and self._stations_url is not None:
            return True

        url = f"{self._config.nws_api_base}/points/{self._config.latitude},{self._config.longitude}"
        response = self._get(url)
        if response is None:
            return False

        try:
            properties = response.json()["properties"]
            gridpoint_url = properties["forecastGridData"]
            stations_url = properties["observationStations"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Rain points response missing gridpoint/station URLs: %s", exc)
            self.available = False
            return False

        self._gridpoint_url = gridpoint_url
        self._stations_url = stations_url
        return True

    def _discover_station(self) -> list[tuple[str, float]] | None:
        response = self._get(self._stations_url)
        if response is None:
            return None

        try:
            stations = response.json()["features"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Rain stations response malformed: %s", exc)
            self.available = False
            return None

        for station in stations:
            try:
                station_id = station["properties"]["stationIdentifier"]
            except (KeyError, TypeError):
                continue

            obs_response = self._get(
                f"{self._config.nws_api_base}/stations/{station_id}/observations",
                params={"limit": _OBSERVATIONS_LIMIT},
            )
            if obs_response is None:
                continue
            try:
                features = obs_response.json()["features"]
            except (ValueError, KeyError, TypeError):
                continue

            buckets = _bucket_observations(features)
            if buckets:
                self._station_id = station_id
                self.available = True
                return buckets

        logger.warning("No observation station near the configured point reports precipitation")
        self.available = False
        return None

    def _get(self, url: str, params: dict | None = None) -> requests.Response | None:
        headers = {"User-Agent": self._user_agent, "Accept": "application/geo+json"}
        try:
            response = self._session.get(
                url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("Rain source request to %s failed: %s", url, exc)
            self.available = False
            return None

        if response.status_code != 200:
            logger.warning("Rain source request to %s returned HTTP %s", url, response.status_code)
            self.available = False
            return None

        return response


class RainStore:
    """Persistent hour-bucketed observed-rainfall history.

    Backed by a single small JSON file (``{"<iso hour>": <mm>, ...}``),
    written atomically (temp file + ``os.replace``) so a crash mid-write
    can't corrupt it. Buckets older than 7 days (relative to the most
    recent bucket on hand) are dropped on every ingest to keep the file
    bounded. A corrupt file on load is logged and treated as empty history
    rather than raising — losing a week of rainfall history is better than
    crash-looping the container.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._buckets: dict[str, float] = {}
        self._load()

    def ingest(self, buckets: list[tuple[str, float]]) -> None:
        """Upsert hour buckets by hour key. Re-ingesting the same
        (hour, mm) pair is a no-op — this is idempotent, not additive."""
        for hour_key, mm in buckets:
            self._buckets[hour_key] = float(mm)
        self._prune()
        self._save()

    def totals(self, now: datetime) -> dict:
        """Sum of observed mm in the trailing 24h and 7d windows ending
        at ``now`` (future-dated buckets, if any, are excluded)."""
        h24_start = now - timedelta(hours=24)
        d7_start = now - timedelta(days=7)
        h24_mm = 0.0
        d7_mm = 0.0
        for key, mm in self._buckets.items():
            timestamp = datetime.fromisoformat(key)
            if timestamp > now:
                continue
            if timestamp > d7_start:
                d7_mm += mm
                if timestamp > h24_start:
                    h24_mm += mm
        return {"h24_mm": round(h24_mm, 3), "d7_mm": round(d7_mm, 3)}

    def hourly_24(self, now: datetime) -> dict[str, float]:
        """The most recent 24 hourly buckets at or before ``now``, in
        chronological order, for publishing as sensor attributes."""
        keys = sorted(key for key in self._buckets if datetime.fromisoformat(key) <= now)
        return {key: self._buckets[key] for key in keys[-24:]}

    def _prune(self) -> None:
        if not self._buckets:
            return
        latest = max(datetime.fromisoformat(key) for key in self._buckets)
        cutoff = latest - timedelta(days=_STORE_RETENTION_DAYS)
        self._buckets = {
            key: mm for key, mm in self._buckets.items() if datetime.fromisoformat(key) > cutoff
        }

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root is not a JSON object")
            self._buckets = {str(key): float(value) for key, value in data.items()}
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                "Rain history file %s is corrupt (%s); starting with empty history.",
                self._path,
                exc,
            )
            self._buckets = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(json.dumps(self._buckets, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self._path)
