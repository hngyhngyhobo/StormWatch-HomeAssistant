"""Tests for stormwatch.location (city-name location resolution, task LOC).

Covers the full resolution-order matrix: explicit coordinates -> LOCATION
matching the Atlanta default (never geocodes) -> cached coordinates for an
unchanged location string -> geocode via Open-Meteo (no key, fake session,
no network) -> ConfigError when geocoding fails and no cache exists.

No network: a duck-typed fake session stands in for requests.Session,
mirroring tests/test_nws.py's pattern.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stormwatch.config import Config, ConfigError
from stormwatch.location import DEFAULT_COORDS, DEFAULT_LOCATION, resolve_location


def _config(**overrides: object) -> Config:
    base: dict[str, object] = dict(
        mqtt_host="192.168.1.10",
        nws_contact="you@example.com",
    )
    base.update(overrides)
    return Config(**base)


class FakeResponse:
    """Duck-typed stand-in for requests.Response."""

    def __init__(self, status_code: int, json_data: object = None) -> None:
        self.status_code = status_code
        self._json_data = json_data

    def json(self) -> object:
        return self._json_data


class FakeSession:
    """Duck-typed stand-in for requests.Session — records calls, no network."""

    def __init__(self, responses: list[object] | None = None) -> None:
        self._responses = list(responses) if responses is not None else []
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _session_returning(*results: object) -> FakeSession:
    return FakeSession(list(results))


_GEOCODE_SUCCESS_BODY = {
    "results": [{"latitude": 40.7128, "longitude": -74.006, "name": "New York"}]
}


# --- explicit coordinates always win, and never touch the cache/network -----


def test_explicit_coordinates_used_regardless_of_location(tmp_path: Path) -> None:
    config = _config(latitude=12.34, longitude=-56.78, location="Custom City, XY")
    cache_path = str(tmp_path / "location.json")
    session = _session_returning()  # would raise IndexError if ever called

    lat, lon, source = resolve_location(config, cache_path, session=session)

    assert (lat, lon, source) == (12.34, -56.78, "coordinates")
    assert session.calls == []
    assert not Path(cache_path).exists()


def test_explicit_coordinates_win_even_with_stale_cache_present(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    cache_path.write_text(
        json.dumps({"location": "Somewhere", "latitude": 1.0, "longitude": 2.0}),
        encoding="utf-8",
    )
    config = _config(latitude=12.34, longitude=-56.78, location="Somewhere")

    lat, lon, source = resolve_location(config, str(cache_path), session=_session_returning())

    assert (lat, lon, source) == (12.34, -56.78, "coordinates")


# --- default location: never geocodes, case-insensitive/stripped match -----


def test_default_location_returns_default_coords_without_network() -> None:
    config = _config(location=DEFAULT_LOCATION)
    session = _session_returning()  # zero calls expected

    lat, lon, source = resolve_location(config, "unused/path/location.json", session=session)

    assert (lat, lon, source) == (*DEFAULT_COORDS, "default")
    assert session.calls == []


def test_default_location_match_is_case_insensitive_and_stripped() -> None:
    config = _config(location="  atlanta, ga  ")
    session = _session_returning()

    lat, lon, source = resolve_location(config, "unused/path/location.json", session=session)

    assert (lat, lon, source) == (*DEFAULT_COORDS, "default")
    assert session.calls == []


def test_default_location_never_calls_geocoder_even_with_queued_response() -> None:
    # Regression guard: even if a session has a response queued, the default
    # path must never dequeue/call it.
    config = _config(location=DEFAULT_LOCATION)
    session = _session_returning(FakeResponse(200, _GEOCODE_SUCCESS_BODY))

    resolve_location(config, "unused/path/location.json", session=session)

    assert session.calls == []


# --- cache hit / invalidation on changed location string --------------------


def test_cache_hit_returns_cached_coords_without_network(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    cache_path.write_text(
        json.dumps({"location": "New York, NY", "latitude": 40.7128, "longitude": -74.006}),
        encoding="utf-8",
    )
    config = _config(location="New York, NY")
    session = _session_returning()

    lat, lon, source = resolve_location(config, str(cache_path), session=session)

    assert (lat, lon, source) == (40.7128, -74.006, "cache")
    assert session.calls == []


def test_cache_hit_matches_ignoring_case_and_surrounding_whitespace(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    cache_path.write_text(
        json.dumps({"location": "New York, NY", "latitude": 40.7128, "longitude": -74.006}),
        encoding="utf-8",
    )
    config = _config(location="  new york, ny  ")
    session = _session_returning()

    lat, lon, source = resolve_location(config, str(cache_path), session=session)

    assert (lat, lon, source) == (40.7128, -74.006, "cache")
    assert session.calls == []


def test_changed_location_string_invalidates_cache_and_geocodes(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    cache_path.write_text(
        json.dumps({"location": "Old City, OC", "latitude": 1.0, "longitude": 2.0}),
        encoding="utf-8",
    )
    config = _config(location="New York, NY")
    session = _session_returning(FakeResponse(200, _GEOCODE_SUCCESS_BODY))

    lat, lon, source = resolve_location(config, str(cache_path), session=session)

    assert (lat, lon, source) == (40.7128, -74.006, "geocoded")
    assert len(session.calls) == 1


# --- geocode success ----------------------------------------------------------


def test_geocode_success_calls_open_meteo_with_expected_request(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    config = _config(location="New York, NY", nws_contact="you@example.com")
    session = _session_returning(FakeResponse(200, _GEOCODE_SUCCESS_BODY))

    lat, lon, source = resolve_location(config, str(cache_path), session=session)

    assert (lat, lon, source) == (40.7128, -74.006, "geocoded")
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://geocoding-api.open-meteo.com/v1/search"
    assert call["params"] == {"name": "New York, NY", "count": 1}
    assert call["timeout"] == 10
    assert "you@example.com" in call["headers"]["User-Agent"]


def test_geocode_success_writes_cache_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    config = _config(location="New York, NY")
    session = _session_returning(FakeResponse(200, _GEOCODE_SUCCESS_BODY))

    resolve_location(config, str(cache_path), session=session)

    assert cache_path.exists()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert data == {"location": "New York, NY", "latitude": 40.7128, "longitude": -74.006}


def test_geocode_success_cache_write_is_atomic_no_leftover_tmp_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    config = _config(location="New York, NY")
    session = _session_returning(FakeResponse(200, _GEOCODE_SUCCESS_BODY))

    resolve_location(config, str(cache_path), session=session)

    leftover_tmp_files = list(tmp_path.glob("*.tmp"))
    assert leftover_tmp_files == []


def test_second_call_with_same_location_uses_cache_not_geocoder(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    config = _config(location="New York, NY")
    first_session = _session_returning(FakeResponse(200, _GEOCODE_SUCCESS_BODY))
    resolve_location(config, str(cache_path), session=first_session)

    second_session = _session_returning()  # no network expected this time
    lat, lon, source = resolve_location(config, str(cache_path), session=second_session)

    assert (lat, lon, source) == (40.7128, -74.006, "cache")
    assert second_session.calls == []


# --- geocode failure ----------------------------------------------------------


def test_geocode_failure_no_cache_raises_config_error(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    config = _config(location="Nonexistent Place, ZZ")
    session = _session_returning(FakeResponse(200, {"results": []}))

    with pytest.raises(ConfigError, match="LATITUDE"):
        resolve_location(config, str(cache_path), session=session)


def test_geocode_http_error_no_cache_raises_config_error(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    config = _config(location="Nonexistent Place, ZZ")
    session = _session_returning(FakeResponse(503))

    with pytest.raises(ConfigError):
        resolve_location(config, str(cache_path), session=session)


def test_geocode_connection_error_no_cache_raises_config_error(tmp_path: Path) -> None:
    import requests

    cache_path = tmp_path / "location.json"
    config = _config(location="Nonexistent Place, ZZ")
    session = _session_returning(requests.exceptions.ConnectionError("boom"))

    with pytest.raises(ConfigError):
        resolve_location(config, str(cache_path), session=session)


def test_geocode_failure_does_not_write_cache_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "location.json"
    config = _config(location="Nonexistent Place, ZZ")
    session = _session_returning(FakeResponse(200, {"results": []}))

    with pytest.raises(ConfigError):
        resolve_location(config, str(cache_path), session=session)

    assert not cache_path.exists()


# --- module constants ---------------------------------------------------------


def test_default_location_constant() -> None:
    assert DEFAULT_LOCATION == "Atlanta, GA"


def test_default_coords_constant() -> None:
    assert DEFAULT_COORDS == (33.749, -84.388)


def test_default_session_created_when_none_injected(tmp_path: Path) -> None:
    # Only reachable when the default path is taken, so this never actually
    # performs network I/O.
    config = _config(location=DEFAULT_LOCATION)

    lat, lon, source = resolve_location(config, str(tmp_path / "location.json"))

    assert source == "default"
    assert (lat, lon) == DEFAULT_COORDS
