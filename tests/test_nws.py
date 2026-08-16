"""Tests for stormwatch.sources.nws (DESIGN.md §4.2).

Covers: point-query request shape, exact User-Agent contract, the
available flag transition, and exponential backoff on 429/503/connection
errors (doubling from the configured poll interval, capped at 900s,
resetting on the next success). No network — a duck-typed fake session
stands in for requests.Session.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from stormwatch import __version__
from stormwatch.config import Config
from stormwatch.sources.nws import NwsPoller

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nws_alerts_active.json"


def _fixture_data() -> dict:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _make_config(**overrides: object) -> Config:
    defaults: dict[str, object] = dict(
        latitude=34.0234,
        longitude=-84.6155,
        mqtt_host="192.168.1.10",
        nws_contact="you@example.com",
        nws_api_base="https://api.weather.gov",
        nws_poll_seconds=60,
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
        # Each entry is either a FakeResponse or an Exception instance to raise.
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


def test_poll_once_returns_features_from_fixture() -> None:
    config = _make_config()
    session = _session_returning(FakeResponse(200, _fixture_data()))
    poller = NwsPoller(config, session=session)

    features = poller.poll_once()

    assert isinstance(features, list)
    assert len(features) == 2
    assert features[0]["properties"]["event"] == "Severe Thunderstorm Warning"
    assert features[0]["properties"]["urgency"] == "Immediate"
    assert features[1]["properties"]["event"] == "Winter Storm Warning"


def test_requests_alerts_active_by_point_no_points_lookup() -> None:
    config = _make_config(latitude=34.0234, longitude=-84.6155)
    session = _session_returning(FakeResponse(200, _fixture_data()))
    poller = NwsPoller(config, session=session)

    poller.poll_once()

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://api.weather.gov/alerts/active"
    assert call["params"] == {"point": "34.0234,-84.6155"}


def test_sends_exact_user_agent_header() -> None:
    config = _make_config(nws_contact="you@example.com")
    session = _session_returning(FakeResponse(200, _fixture_data()))
    poller = NwsPoller(config, session=session)

    poller.poll_once()

    expected = (
        f"StormWatch/{__version__} "
        "(https://github.com/hngyhngyhobo/StormWatch-HomeAssistant, you@example.com)"
    )
    assert session.calls[0]["headers"]["User-Agent"] == expected


def test_timeout_is_ten_seconds() -> None:
    config = _make_config()
    session = _session_returning(FakeResponse(200, _fixture_data()))
    poller = NwsPoller(config, session=session)

    poller.poll_once()

    assert session.calls[0]["timeout"] == 10


def test_available_false_before_first_poll() -> None:
    config = _make_config()
    poller = NwsPoller(config, session=_session_returning())

    assert poller.available is False


def test_available_true_after_success() -> None:
    config = _make_config()
    session = _session_returning(FakeResponse(200, _fixture_data()))
    poller = NwsPoller(config, session=session)

    poller.poll_once()

    assert poller.available is True


def test_503_returns_none_and_clears_available() -> None:
    config = _make_config()
    session = _session_returning(FakeResponse(503))
    poller = NwsPoller(config, session=session)

    result = poller.poll_once()

    assert result is None
    assert poller.available is False


def test_429_returns_none_and_clears_available() -> None:
    config = _make_config()
    session = _session_returning(FakeResponse(429))
    poller = NwsPoller(config, session=session)

    result = poller.poll_once()

    assert result is None
    assert poller.available is False


def test_connection_error_returns_none_and_clears_available() -> None:
    config = _make_config()
    session = _session_returning(requests.exceptions.ConnectionError("boom"))
    poller = NwsPoller(config, session=session)

    result = poller.poll_once()

    assert result is None
    assert poller.available is False


def test_available_flips_back_to_false_after_a_failure_following_success() -> None:
    config = _make_config()
    session = _session_returning(FakeResponse(200, _fixture_data()), FakeResponse(503))
    poller = NwsPoller(config, session=session)

    poller.poll_once()
    assert poller.available is True

    poller.poll_once()
    assert poller.available is False


def test_backoff_seconds_starts_at_poll_interval() -> None:
    config = _make_config(nws_poll_seconds=60)
    poller = NwsPoller(config, session=_session_returning())

    assert poller.backoff_seconds == 60


def test_backoff_doubles_on_failure() -> None:
    config = _make_config(nws_poll_seconds=60)
    session = _session_returning(FakeResponse(503))
    poller = NwsPoller(config, session=session)

    poller.poll_once()

    assert poller.backoff_seconds == 120


def test_backoff_doubles_again_on_consecutive_failures() -> None:
    config = _make_config(nws_poll_seconds=60)
    session = _session_returning(FakeResponse(503), FakeResponse(503), FakeResponse(503))
    poller = NwsPoller(config, session=session)

    poller.poll_once()
    poller.poll_once()
    poller.poll_once()

    assert poller.backoff_seconds == 480


def test_backoff_capped_at_900_seconds() -> None:
    config = _make_config(nws_poll_seconds=60)
    session = _session_returning(*(FakeResponse(503) for _ in range(6)))
    poller = NwsPoller(config, session=session)

    for _ in range(6):
        poller.poll_once()

    assert poller.backoff_seconds == 900


def test_backoff_resets_to_poll_interval_after_success() -> None:
    config = _make_config(nws_poll_seconds=60)
    session = _session_returning(
        FakeResponse(503), FakeResponse(503), FakeResponse(200, _fixture_data())
    )
    poller = NwsPoller(config, session=session)

    poller.poll_once()
    poller.poll_once()
    assert poller.backoff_seconds == 240

    poller.poll_once()

    assert poller.backoff_seconds == 60


def test_default_session_is_created_when_none_injected() -> None:
    config = _make_config()
    poller = NwsPoller(config)

    assert isinstance(poller._session, requests.Session)


def test_module_importable() -> None:
    assert callable(NwsPoller)
