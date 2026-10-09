"""Tests for stormwatch.levels: alert-level labels, event list, and the
persisted override store (/config/alert_levels.json)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from stormwatch import config as config_module
from stormwatch.levels import (
    ALERT_LEVEL_EVENTS,
    LABEL_TO_PRIORITY,
    LEVEL_OPTIONS,
    MARINE_ONLY_EVENTS,
    PRIORITY_TO_LABEL,
    LevelOverrides,
    level_key,
)


def test_level_options_and_maps() -> None:
    assert LEVEL_OPTIONS == ("WAKE ME UP", "Heads-up", "Silent push", "Off")
    assert LABEL_TO_PRIORITY == {
        "WAKE ME UP": "critical",
        "Heads-up": "high",
        "Silent push": "normal",
        "Off": "ignore",
    }
    assert {v: k for k, v in LABEL_TO_PRIORITY.items()} == PRIORITY_TO_LABEL


def test_level_key_slug() -> None:
    assert level_key("Tornado Warning") == "alert_level_tornado_warning"
    assert level_key("Special Marine Warning") == "alert_level_special_marine_warning"


def test_alert_level_events_is_sorted_union_of_66() -> None:
    expected = set(
        config_module._DEFAULT_ALERTS_CRITICAL
        + config_module._DEFAULT_ALERTS_HIGH
        + config_module._DEFAULT_ALERTS_NORMAL
    )
    assert set(ALERT_LEVEL_EVENTS) == expected
    assert len(ALERT_LEVEL_EVENTS) == 66
    assert list(ALERT_LEVEL_EVENTS) == sorted(ALERT_LEVEL_EVENTS)
    for event in ("Tornado Warning", "Hurricane Warning", "Flood Watch", "Tornado Watch"):
        assert event in ALERT_LEVEL_EVENTS
    keys = [level_key(e) for e in ALERT_LEVEL_EVENTS]
    assert len(set(keys)) == 66


def test_marine_only_events_subset() -> None:
    assert len(MARINE_ONLY_EVENTS) == 11
    assert MARINE_ONLY_EVENTS <= set(ALERT_LEVEL_EVENTS)
    assert "Gale Warning" in MARINE_ONLY_EVENTS
    assert "Special Marine Warning" in MARINE_ONLY_EVENTS
    assert "Hurricane Force Wind Warning" in MARINE_ONLY_EVENTS
    assert "Tornado Warning" not in MARINE_ONLY_EVENTS


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    ov = LevelOverrides(str(tmp_path / "alert_levels.json"))
    ov.load()
    assert ov.get("Tornado Warning") is None
    assert ov.last_error is None


def test_load_corrupt_file_is_empty_with_error(tmp_path: Path, caplog) -> None:
    path = tmp_path / "alert_levels.json"
    path.write_text("{not json", encoding="utf-8")
    ov = LevelOverrides(str(path))
    with caplog.at_level(logging.WARNING):
        ov.load()
    assert ov.get("Tornado Warning") is None
    assert ov.last_error is not None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("body", ["[]", '{"version": 1}', '{"levels": []}', '"x"'])
def test_load_wrong_shape_is_empty_with_error(tmp_path: Path, body: str) -> None:
    path = tmp_path / "alert_levels.json"
    path.write_text(body, encoding="utf-8")
    ov = LevelOverrides(str(path))
    ov.load()
    assert ov.get("Tornado Warning") is None
    assert ov.last_error is not None


def test_load_drops_invalid_entries_keeps_valid(tmp_path: Path) -> None:
    path = tmp_path / "alert_levels.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "levels": {
                    "Tornado Watch": "ignore",
                    "Flood Watch": "bogus",
                    "Not A Real Event": "critical",
                    "High Wind Warning": 5,
                },
            }
        ),
        encoding="utf-8",
    )
    ov = LevelOverrides(str(path))
    ov.load()
    assert ov.get("Tornado Watch") == "ignore"
    assert ov.get("Flood Watch") is None
    assert ov.get("Not A Real Event") is None
    assert ov.get("High Wind Warning") is None


def test_set_persists_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "alert_levels.json"
    ov = LevelOverrides(str(path))
    ov.set("Tornado Watch", "ignore")
    assert ov.get("Tornado Watch") == "ignore"

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 1, "levels": {"Tornado Watch": "ignore"}}
    assert [p.name for p in tmp_path.iterdir()] == ["alert_levels.json"]

    again = LevelOverrides(str(path))
    again.load()
    assert again.get("Tornado Watch") == "ignore"


def test_set_rejects_unknown_event_and_bad_priority(tmp_path: Path) -> None:
    ov = LevelOverrides(str(tmp_path / "alert_levels.json"))
    with pytest.raises(ValueError):
        ov.set("Not A Real Event", "critical")
    with pytest.raises(ValueError):
        ov.set("Tornado Watch", "loud")
    assert ov.get("Tornado Watch") is None
    assert not (tmp_path / "alert_levels.json").exists()
