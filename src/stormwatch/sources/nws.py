"""NWS api.weather.gov poller (DESIGN.md §4.2).

No API key; identifies via User-Agent with the user's contact. Poll floor
30 s enforced. Exponential backoff on 429/503/connection errors flips the
availability flag — HA must know when alert data is stale.
"""

from __future__ import annotations

import logging

import requests

from stormwatch import __version__
from stormwatch.config import Config

logger = logging.getLogger("stormwatch.sources.nws")

_REQUEST_TIMEOUT_SECONDS = 10
_BACKOFF_CAP_SECONDS = 900
_REPO_URL = "https://github.com/hngyhngyhobo/StormWatch-HomeAssistant"


class NwsPoller:
    """Polls /alerts/active for the configured point.

    No /points lookup is needed — ``point=`` on /alerts/active filters
    directly, so there's no zone to cache. A failed request (429, 503, or
    a connection error) clears ``available`` and doubles ``backoff_seconds``
    from the configured poll interval, capped at 900 s; a subsequent
    success resets it. The supervisor loop is responsible for sleeping —
    this class only computes the interval.
    """

    def __init__(self, config: Config, session: requests.Session | None = None) -> None:
        self._config = config
        self._session = session if session is not None else requests.Session()
        self._user_agent = f"StormWatch/{__version__} ({_REPO_URL}, {config.nws_contact})"
        self._base_backoff_seconds = config.nws_poll_seconds
        self.backoff_seconds = config.nws_poll_seconds
        self.available = False

    def poll_once(self) -> list[dict] | None:
        """Fetch current active alerts for the configured point.

        Returns the GeoJSON ``features`` list on success. Returns ``None``
        on failure, having cleared ``available`` and doubled
        ``backoff_seconds`` (capped at 900 s).
        """
        url = f"{self._config.nws_api_base}/alerts/active"
        params = {"point": f"{self._config.latitude},{self._config.longitude}"}
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/geo+json",
        }

        try:
            response = self._session.get(
                url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("NWS alerts request failed: %s", exc)
            self._on_failure()
            return None

        if response.status_code != 200:
            logger.warning("NWS alerts request returned HTTP %s", response.status_code)
            self._on_failure()
            return None

        try:
            features = response.json()["features"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("NWS alerts response was not valid GeoJSON: %s", exc)
            self._on_failure()
            return None

        self.available = True
        self.backoff_seconds = self._base_backoff_seconds
        return features

    def _on_failure(self) -> None:
        self.available = False
        self.backoff_seconds = min(self.backoff_seconds * 2, _BACKOFF_CAP_SECONDS)
