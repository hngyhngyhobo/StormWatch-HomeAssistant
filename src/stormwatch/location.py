"""City-name location resolution with an Atlanta default (task LOC).

Resolution order, checked in ``resolve_location``:

1. **Explicit coordinates** -- ``config.latitude``/``config.longitude`` both
   set -- used as-is. Highest priority regardless of ``LOCATION``.
2. **Default location** -- ``config.location`` matches ``DEFAULT_LOCATION``
   (case-insensitive, stripped) -- ``DEFAULT_COORDS``. This branch NEVER
   calls the geocoder, so a from-scratch install with nothing configured
   never makes an outbound request.
3. **Cache** -- a JSON file at ``cache_path`` keyed by the location string;
   a hit for the *current* (case-insensitive, stripped) location string
   returns its cached coordinates. A changed location string is a cache
   miss (falls through to geocoding), not an error.
4. **Geocode** -- one call to Open-Meteo's free, no-key geocoding API
   (same User-Agent convention as ``sources/nws.py``); on success, the
   result is cached atomically (temp file + ``os.replace``, mirroring
   ``sources/rain.py``'s ``RainStore._save``) and returned.
5. **Failure** -- geocoding failed (network error, non-200, or no results)
   and there's no cache to fall back on -- raises ``ConfigError`` with a
   plain-English message pointing the user at LATITUDE/LONGITUDE.

Only the "coordinates" and "default" branches are network-free by
construction; "cache" is network-free because a hit short-circuits before
any request is made.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from stormwatch import __version__
from stormwatch.config import ConfigError

if TYPE_CHECKING:
    from stormwatch.config import Config

logger = logging.getLogger("stormwatch.location")

DEFAULT_LOCATION = "Atlanta, GA"
DEFAULT_COORDS: tuple[float, float] = (33.749, -84.388)

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_REQUEST_TIMEOUT_SECONDS = 10
_REPO_URL = "https://github.com/hngyhngyhobo/StormWatch-HomeAssistant"


def _normalize(location: str) -> str:
    return location.strip().casefold()


def resolve_location(
    config: Config, cache_path: str, session: requests.Session | None = None
) -> tuple[float, float, str]:
    """Resolve the effective (latitude, longitude, source) for ``config``.

    ``source`` is one of "coordinates" | "cache" | "geocoded" | "default" --
    see the module docstring for the full resolution order. Raises
    ``ConfigError`` only in the geocode-failure-with-no-cache case (5).
    """
    if config.latitude is not None and config.longitude is not None:
        return config.latitude, config.longitude, "coordinates"

    location = (config.location or DEFAULT_LOCATION).strip()

    if _normalize(location) == _normalize(DEFAULT_LOCATION):
        return DEFAULT_COORDS[0], DEFAULT_COORDS[1], "default"

    cached = _read_cache(cache_path, location)
    if cached is not None:
        return cached[0], cached[1], "cache"

    geocoded = _geocode(location, config.nws_contact, session)
    if geocoded is None:
        raise ConfigError(
            f"Could not determine coordinates for LOCATION={location!r}: geocoding failed "
            "and no cached coordinates were found. Set LATITUDE and LONGITUDE to your exact "
            "decimal-degree coordinates instead."
        )

    lat, lon = geocoded
    _write_cache(cache_path, location, lat, lon)
    return lat, lon, "geocoded"


def _read_cache(cache_path: str, location: str) -> tuple[float, float] | None:
    path = Path(cache_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Location cache %s is unreadable (%s); ignoring.", cache_path, exc)
        return None
    if not isinstance(data, dict):
        return None

    cached_location = data.get("location")
    if not isinstance(cached_location, str) or _normalize(cached_location) != _normalize(location):
        return None

    try:
        return float(data["latitude"]), float(data["longitude"])
    except (KeyError, TypeError, ValueError):
        return None


def _write_cache(cache_path: str, location: str, latitude: float, longitude: float) -> None:
    """Atomically write the location cache (temp file + os.replace),
    mirroring ``sources/rain.py``'s ``RainStore._save`` convention."""
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    payload = {"location": location, "latitude": latitude, "longitude": longitude}
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp_path, path)


def _geocode(
    location: str, contact: str, session: requests.Session | None
) -> tuple[float, float] | None:
    sess = session if session is not None else requests.Session()
    headers = {"User-Agent": f"StormWatch/{__version__} ({_REPO_URL}, {contact})"}

    try:
        response = sess.get(
            _GEOCODE_URL,
            params={"name": location, "count": 1},
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("Geocoding request for %r failed: %s", location, exc)
        return None

    if response.status_code != 200:
        logger.warning("Geocoding request for %r returned HTTP %s", location, response.status_code)
        return None

    try:
        results = response.json()["results"]
        first = results[0]
        latitude = float(first["latitude"])
        longitude = float(first["longitude"])
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        logger.warning("Geocoding response for %r had no usable result: %s", location, exc)
        return None

    return latitude, longitude
