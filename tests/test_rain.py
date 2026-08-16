"""Tests for stormwatch.sources.rain (DESIGN.md §4, watering alerts / task D1).

Covers RainSource (gridpoint QPF forecast math incl. UTC-day and 48h-window
boundary splitting, /points caching, observation-station discovery +
caching, null-observation skipping, User-Agent/timeout contract shared with
NwsPoller) and RainStore (idempotent hour-bucket ingest, 24h/7d totals
across boundaries, hourly_24 capped at 24 entries, >7d pruning, atomic
persistence round-trip, corrupt-file recovery). No network — a duck-typed
fake session stands in for requests.Session, and RainSource takes an
injected clock so forecast-window math is deterministic.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from stormwatch import __version__
from stormwatch.config import Config
from stormwatch.sources.rain import RainSource, RainStore

_FIXTURES = Path(__file__).parent / "fixtures"
_NOW = datetime(2026, 8, 9, 18, 0, tzinfo=UTC)


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _make_config(**overrides: object) -> Config:
    defaults: dict[str, object] = dict(
        latitude=33.3849,
        longitude=-84.5697,
        mqtt_host="192.168.1.10",
        nws_contact="you@example.com",
        nws_api_base="https://api.weather.gov",
    )
    defaults.update(overrides)
    return Config(**defaults)


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


def _fixed_clock(when: datetime = _NOW):
    return lambda: when


def _points_response() -> FakeResponse:
    return FakeResponse(200, _fixture("nws_points.json"))


def _gridpoint_response() -> FakeResponse:
    return FakeResponse(200, _fixture("nws_gridpoint.json"))


def _stations_response() -> FakeResponse:
    return FakeResponse(200, _fixture("nws_stations.json"))


def _observations_response() -> FakeResponse:
    return FakeResponse(200, _fixture("nws_observations.json"))


def _all_null_observations() -> dict:
    """The KKFFC2 station's data: reports the property, never a value."""
    data = _fixture("nws_observations.json")
    for feature in data["features"]:
        feature["properties"]["precipitationLastHour"] = {"unitCode": "wmoUnit:mm", "value": None}
    return data


# ---------------------------------------------------------------------------
# RainSource — forecast
# ---------------------------------------------------------------------------


def test_poll_forecast_returns_today_and_h48_mm() -> None:
    config = _make_config()
    session = _session_returning(_points_response(), _gridpoint_response())
    source = RainSource(config, session=session, clock=_fixed_clock())

    result = source.poll_forecast()

    # Hand-verified against the fixture's QPF periods: today_mm sums the
    # UTC-calendar-day-of-`now` periods, splitting the 21:00-03:00 period
    # that straddles the day boundary 50/50; h48_mm is the rolling 48h
    # forward from `now`, splitting the period straddling that window's end.
    assert result == {"today_mm": 7.2, "h48_mm": 14.75}
    assert source.available is True


def test_points_url_and_gridpoint_url_requested_in_order() -> None:
    config = _make_config(latitude=33.3849, longitude=-84.5697)
    session = _session_returning(_points_response(), _gridpoint_response())
    source = RainSource(config, session=session, clock=_fixed_clock())

    source.poll_forecast()

    assert session.calls[0]["url"] == "https://api.weather.gov/points/33.3849,-84.5697"
    assert session.calls[1]["url"] == "https://api.weather.gov/gridpoints/FFC/60,66"


def test_points_lookup_is_cached_across_forecast_polls() -> None:
    config = _make_config()
    session = _session_returning(_points_response(), _gridpoint_response(), _gridpoint_response())
    source = RainSource(config, session=session, clock=_fixed_clock())

    source.poll_forecast()
    source.poll_forecast()

    points_calls = [c for c in session.calls if "/points/" in str(c["url"])]
    assert len(points_calls) == 1
    assert len(session.calls) == 3  # 1x /points + 2x gridpoint


def test_sends_exact_user_agent_header() -> None:
    config = _make_config(nws_contact="you@example.com")
    session = _session_returning(_points_response(), _gridpoint_response())
    source = RainSource(config, session=session, clock=_fixed_clock())

    source.poll_forecast()

    expected = (
        f"StormWatch/{__version__} "
        "(https://github.com/hngyhngyhobo/WeatherAlert-HomeAssistant, you@example.com)"
    )
    assert session.calls[0]["headers"]["User-Agent"] == expected
    assert session.calls[1]["headers"]["User-Agent"] == expected


def test_all_requests_use_ten_second_timeout() -> None:
    config = _make_config()
    session = _session_returning(_points_response(), _gridpoint_response())
    source = RainSource(config, session=session, clock=_fixed_clock())

    source.poll_forecast()

    assert all(call["timeout"] == 10 for call in session.calls)


def test_available_false_before_first_poll() -> None:
    config = _make_config()
    source = RainSource(config, session=_session_returning())

    assert source.available is False


def test_points_non_200_returns_none_and_clears_available() -> None:
    config = _make_config()
    session = _session_returning(FakeResponse(503))
    source = RainSource(config, session=session, clock=_fixed_clock())

    result = source.poll_forecast()

    assert result is None
    assert source.available is False


def test_gridpoint_non_200_returns_none_and_clears_available() -> None:
    config = _make_config()
    session = _session_returning(_points_response(), FakeResponse(500))
    source = RainSource(config, session=session, clock=_fixed_clock())

    result = source.poll_forecast()

    assert result is None
    assert source.available is False


def test_connection_error_returns_none_and_clears_available() -> None:
    config = _make_config()
    session = _session_returning(requests.exceptions.ConnectionError("boom"))
    source = RainSource(config, session=session, clock=_fixed_clock())

    result = source.poll_forecast()

    assert result is None
    assert source.available is False


def test_available_flips_back_to_false_after_a_failure_following_success() -> None:
    config = _make_config()
    session = _session_returning(_points_response(), _gridpoint_response(), FakeResponse(503))
    source = RainSource(config, session=session, clock=_fixed_clock())

    source.poll_forecast()
    assert source.available is True

    source.poll_forecast()
    assert source.available is False


def test_default_session_is_created_when_none_injected() -> None:
    config = _make_config()
    source = RainSource(config)

    assert isinstance(source._session, requests.Session)


def test_module_importable() -> None:
    assert callable(RainSource)
    assert callable(RainStore)


# ---------------------------------------------------------------------------
# RainSource — observations / station discovery
# ---------------------------------------------------------------------------


def test_poll_observations_discovers_first_precip_reporting_station() -> None:
    config = _make_config()
    session = _session_returning(
        _points_response(),
        _stations_response(),
        FakeResponse(200, _all_null_observations()),  # KKFFC2 — no precip sensor
        _observations_response(),  # KFFC — real data
    )
    source = RainSource(config, session=session, clock=_fixed_clock())

    buckets = source.poll_observations()

    assert buckets is not None
    assert len(buckets) > 0
    assert source.available is True

    obs_calls = [str(c["url"]) for c in session.calls if "/observations" in str(c["url"])]
    assert obs_calls == [
        "https://api.weather.gov/stations/KKFFC2/observations",
        "https://api.weather.gov/stations/KFFC/observations",
    ]


def test_station_discovery_is_cached_across_observation_polls() -> None:
    config = _make_config()
    session = _session_returning(
        _points_response(),
        _stations_response(),
        FakeResponse(200, _all_null_observations()),
        _observations_response(),
        _observations_response(),  # second poll: straight to KFFC
    )
    source = RainSource(config, session=session, clock=_fixed_clock())

    source.poll_observations()
    source.poll_observations()

    points_calls = [c for c in session.calls if "/points/" in str(c["url"])]
    stations_list_calls = [c for c in session.calls if str(c["url"]).endswith("/stations")]
    assert len(points_calls) == 1
    assert len(stations_list_calls) == 1
    assert len(session.calls) == 5
    assert session.calls[-1]["url"] == "https://api.weather.gov/stations/KFFC/observations"


def test_observations_request_uses_limit_48() -> None:
    config = _make_config()
    session = _session_returning(
        _points_response(),
        _stations_response(),
        FakeResponse(200, _all_null_observations()),
        _observations_response(),
    )
    source = RainSource(config, session=session, clock=_fixed_clock())

    source.poll_observations()

    obs_call = next(c for c in session.calls if str(c["url"]).endswith("/KFFC/observations"))
    assert obs_call["params"] == {"limit": 48}


def test_null_precip_observations_are_skipped() -> None:
    config = _make_config()
    session = _session_returning(
        _points_response(),
        _stations_response(),
        FakeResponse(200, _all_null_observations()),
        _observations_response(),
    )
    source = RainSource(config, session=session, clock=_fixed_clock())

    buckets = source.poll_observations()

    raw = _fixture("nws_observations.json")
    null_count = sum(
        1
        for feature in raw["features"]
        if feature["properties"]["precipitationLastHour"]["value"] is None
    )
    assert len(buckets) == len(raw["features"]) - null_count


def test_observations_bucketed_by_utc_hour() -> None:
    config = _make_config()
    session = _session_returning(
        _points_response(),
        _stations_response(),
        FakeResponse(200, _all_null_observations()),
        _observations_response(),
    )
    source = RainSource(config, session=session, clock=_fixed_clock())

    buckets = dict(source.poll_observations())

    assert buckets["2026-08-09T17:00:00+00:00"] == 0.0
    assert buckets["2026-08-09T15:00:00+00:00"] == 0.3
    assert buckets["2026-08-09T13:00:00+00:00"] == 2.5
    assert buckets["2026-08-09T06:00:00+00:00"] == 0.8
    # the null-valued hour (index 5, ts 12:53) must not appear at all
    assert "2026-08-09T12:00:00+00:00" not in buckets


def test_no_precip_reporting_station_returns_none() -> None:
    config = _make_config()
    session = _session_returning(
        _points_response(),
        _stations_response(),
        FakeResponse(200, _all_null_observations()),  # KKFFC2 — no precip sensor
        FakeResponse(200, _all_null_observations()),  # KFFC — also no data this time
    )
    source = RainSource(config, session=session, clock=_fixed_clock())

    result = source.poll_observations()

    assert result is None
    assert source.available is False


def test_stations_list_non_200_returns_none_and_clears_available() -> None:
    config = _make_config()
    session = _session_returning(_points_response(), FakeResponse(500))
    source = RainSource(config, session=session, clock=_fixed_clock())

    result = source.poll_observations()

    assert result is None
    assert source.available is False


# ---------------------------------------------------------------------------
# RainStore
# ---------------------------------------------------------------------------


def test_ingest_and_totals_basic(tmp_path) -> None:
    store = RainStore(str(tmp_path / "rain_history.json"))

    store.ingest(
        [
            ("2026-08-09T17:00:00+00:00", 1.0),
            ("2026-08-09T16:00:00+00:00", 2.0),
        ]
    )

    totals = store.totals(_NOW)
    assert totals == {"h24_mm": 3.0, "d7_mm": 3.0}


def test_ingest_is_idempotent(tmp_path) -> None:
    store = RainStore(str(tmp_path / "rain_history.json"))

    store.ingest([("2026-08-09T17:00:00+00:00", 1.0)])
    store.ingest([("2026-08-09T17:00:00+00:00", 1.0)])
    store.ingest([("2026-08-09T17:00:00+00:00", 1.0)])

    assert store.totals(_NOW)["h24_mm"] == 1.0


def test_ingest_upserts_same_hour_bucket_does_not_add(tmp_path) -> None:
    store = RainStore(str(tmp_path / "rain_history.json"))

    store.ingest([("2026-08-09T17:00:00+00:00", 1.0)])
    store.ingest([("2026-08-09T17:00:00+00:00", 2.5)])  # corrected/updated reading

    assert store.totals(_NOW)["h24_mm"] == 2.5


def test_totals_24h_boundary_is_exclusive(tmp_path) -> None:
    store = RainStore(str(tmp_path / "rain_history.json"))

    store.ingest(
        [
            ("2026-08-08T18:00:00+00:00", 5.0),  # exactly 24h before now -> excluded
            ("2026-08-08T19:00:00+00:00", 3.0),  # 23h before now -> included
        ]
    )

    totals = store.totals(_NOW)
    assert totals["h24_mm"] == 3.0
    assert totals["d7_mm"] == 8.0


def test_totals_7d_boundary_is_exclusive(tmp_path) -> None:
    store = RainStore(str(tmp_path / "rain_history.json"))

    store.ingest(
        [
            ("2026-08-02T18:00:00+00:00", 4.0),  # exactly 7d before now -> excluded
            ("2026-08-02T19:00:00+00:00", 2.0),  # just inside 7d -> included
        ]
    )

    assert store.totals(_NOW)["d7_mm"] == 2.0


def test_totals_excludes_future_buckets(tmp_path) -> None:
    store = RainStore(str(tmp_path / "rain_history.json"))

    store.ingest(
        [
            ("2026-08-09T17:00:00+00:00", 1.0),
            ("2026-08-09T19:00:00+00:00", 99.0),  # after `now` -> excluded
        ]
    )

    assert store.totals(_NOW) == {"h24_mm": 1.0, "d7_mm": 1.0}


def test_hourly_24_returns_at_most_24_entries_in_order(tmp_path) -> None:
    store = RainStore(str(tmp_path / "rain_history.json"))

    buckets = [((_NOW - timedelta(hours=i)).isoformat(), float(i)) for i in range(30)]
    store.ingest(buckets)

    hourly = store.hourly_24(_NOW)

    assert len(hourly) == 24
    keys = list(hourly.keys())
    assert keys == sorted(keys)
    assert keys[-1] == _NOW.isoformat()
    assert hourly[keys[-1]] == 0.0
    assert keys[0] == (_NOW - timedelta(hours=23)).isoformat()


def test_ingest_prunes_buckets_older_than_seven_days(tmp_path) -> None:
    path = tmp_path / "rain_history.json"
    store = RainStore(str(path))

    store.ingest(
        [
            ("2026-08-09T18:00:00+00:00", 1.0),  # latest bucket -> pruning reference point
            ("2026-08-02T17:00:00+00:00", 9.0),  # >7d before latest -> pruned
            ("2026-08-02T19:00:00+00:00", 2.0),  # <7d before latest -> kept
        ]
    )

    # Assert against the persisted file directly, so this proves the bucket
    # was physically dropped from the store rather than merely windowed out
    # by totals()'s own 7d cutoff.
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert "2026-08-02T17:00:00+00:00" not in persisted
    assert persisted["2026-08-02T19:00:00+00:00"] == 2.0
    assert persisted["2026-08-09T18:00:00+00:00"] == 1.0
    assert len(persisted) == 2


def test_atomic_round_trip_new_instance_same_totals(tmp_path) -> None:
    path = tmp_path / "rain_history.json"
    store = RainStore(str(path))

    store.ingest(
        [
            ("2026-08-09T17:00:00+00:00", 1.5),
            ("2026-08-09T16:00:00+00:00", 0.5),
        ]
    )

    assert path.exists()
    reloaded = RainStore(str(path))

    assert reloaded.totals(_NOW) == store.totals(_NOW)
    assert reloaded.hourly_24(_NOW) == store.hourly_24(_NOW)


def test_save_leaves_no_leftover_tmp_file(tmp_path) -> None:
    path = tmp_path / "rain_history.json"
    store = RainStore(str(path))

    store.ingest([("2026-08-09T17:00:00+00:00", 1.0)])

    assert not (tmp_path / "rain_history.json.tmp").exists()
    assert path.exists()


def test_missing_file_starts_empty(tmp_path) -> None:
    path = tmp_path / "does_not_exist.json"

    store = RainStore(str(path))

    assert store.totals(_NOW) == {"h24_mm": 0.0, "d7_mm": 0.0}
    assert store.hourly_24(_NOW) == {}


def test_corrupt_file_logs_warning_and_starts_empty(tmp_path, caplog) -> None:
    path = tmp_path / "rain_history.json"
    path.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="stormwatch.sources.rain"):
        store = RainStore(str(path))

    assert store.totals(_NOW) == {"h24_mm": 0.0, "d7_mm": 0.0}
    assert store.hourly_24(_NOW) == {}
    assert any("corrupt" in record.message.lower() for record in caplog.records)


def test_non_object_json_root_treated_as_corrupt(tmp_path) -> None:
    path = tmp_path / "rain_history.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    store = RainStore(str(path))

    assert store.totals(_NOW) == {"h24_mm": 0.0, "d7_mm": 0.0}


def test_corrupt_file_does_not_prevent_further_ingest(tmp_path) -> None:
    path = tmp_path / "rain_history.json"
    path.write_text("not json at all", encoding="utf-8")

    store = RainStore(str(path))
    store.ingest([("2026-08-09T17:00:00+00:00", 4.0)])

    assert store.totals(_NOW)["h24_mm"] == 4.0
    # and it's now a well-formed file again
    reloaded = RainStore(str(path))
    assert reloaded.totals(_NOW)["h24_mm"] == 4.0
