"""Alert levels adjustable from Home Assistant.

Each NWS warning/watch in the default lists gets a Home Assistant ``select``
entity. A choice made there is stored as an override in
``/config/alert_levels.json`` and wins over alerts.yaml / env rules for that
event (see ``RuleEngine.evaluate_detail``).

Fail-safe: a missing, unreadable, or corrupt overrides file never raises --
it loads as "no overrides" (alerts.yaml applies) and records ``last_error``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading

from . import config as _config

logger = logging.getLogger("stormwatch.levels")

LEVEL_OPTIONS: tuple[str, ...] = ("WAKE ME UP", "Heads-up", "Silent push", "Off")

LABEL_TO_PRIORITY: dict[str, str] = {
    "WAKE ME UP": "critical",
    "Heads-up": "high",
    "Silent push": "normal",
    "Off": "ignore",
}
PRIORITY_TO_LABEL: dict[str, str] = {v: k for k, v in LABEL_TO_PRIORITY.items()}

_VALID_PRIORITIES = frozenset(PRIORITY_TO_LABEL)

ALERT_LEVEL_EVENTS: tuple[str, ...] = tuple(
    sorted(
        set(_config._DEFAULT_ALERTS_CRITICAL)
        | set(_config._DEFAULT_ALERTS_HIGH)
        | set(_config._DEFAULT_ALERTS_NORMAL)
    )
)
_EVENT_SET = frozenset(ALERT_LEVEL_EVENTS)

# Marine-only events: their select entities are created disabled in HA.
MARINE_ONLY_EVENTS: frozenset[str] = frozenset(
    {
        "Gale Warning",
        "Gale Watch",
        "Storm Warning",
        "Storm Watch",
        "Hazardous Seas Warning",
        "Hazardous Seas Watch",
        "Special Marine Warning",
        "Heavy Freezing Spray Warning",
        "Heavy Freezing Spray Watch",
        "Hurricane Force Wind Warning",
        "Hurricane Force Wind Watch",
    }
)

_FILE_VERSION = 1


def level_key(event: str) -> str:
    """Discovery/state key for an event: ``alert_level_<slug>``."""
    return "alert_level_" + re.sub(r"[^a-z0-9]", "_", event.lower())


class LevelOverrides:
    """Persisted per-event priority overrides (thread-safe)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.last_error: str | None = None
        self._levels: dict[str, str] = {}
        self._lock = threading.Lock()

    def load(self) -> None:
        """Read the overrides file. Never raises; on any problem the overrides
        are empty (normal rules apply) and ``last_error`` is set."""
        levels: dict[str, str] = {}
        error: str | None = None
        try:
            try:
                with open(self.path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except FileNotFoundError:
                data = None
            except (OSError, ValueError) as exc:
                error = f"could not read {self.path}: {exc}"
                data = None
            if data is not None and error is None:
                raw = data.get("levels") if isinstance(data, dict) else None
                if not isinstance(raw, dict):
                    error = f"{self.path} has an unexpected shape (no 'levels' mapping)"
                else:
                    for event, priority in raw.items():
                        if event in _EVENT_SET and priority in _VALID_PRIORITIES:
                            levels[event] = priority
                        else:
                            logger.warning(
                                "Ignoring invalid alert level entry %r: %r in %s",
                                event,
                                priority,
                                self.path,
                            )
        except Exception as exc:  # load() must never raise
            error = f"could not load {self.path}: {exc}"
            levels = {}
        if error is not None:
            logger.warning("%s; running with no alert level overrides", error)
            levels = {}
        with self._lock:
            self._levels = levels
            self.last_error = error

    def get(self, event: str) -> str | None:
        with self._lock:
            return self._levels.get(event)

    def set(self, event: str, priority: str) -> None:
        """Validate, persist atomically, then apply. Raises ValueError on bad
        input; OSError if the file cannot be written (nothing is applied)."""
        if event not in _EVENT_SET:
            raise ValueError(f"unknown alert event {event!r}")
        if priority not in _VALID_PRIORITIES:
            raise ValueError(f"invalid priority {priority!r}")
        with self._lock:
            updated = dict(self._levels)
            updated[event] = priority
            self._write(updated)
            self._levels = updated

    def _write(self, levels: dict[str, str]) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = os.path.join(directory, os.path.basename(self.path) + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump({"version": _FILE_VERSION, "levels": levels}, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
