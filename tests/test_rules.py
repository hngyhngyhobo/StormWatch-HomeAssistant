"""Tests for stormwatch.rules (DESIGN.md §7).

Covers: the env-var rule layer built from Config.alerts_*, the
/config/alerts.yaml layer (match semantics, enabled flag, schema
validation with retain-previous-on-error), generate_default()'s broad
shipped default ruleset (owner decision 2026-08-09: emit events for
essentially every NWS warning/watch/advisory, prioritized -- filtering
into notifications is Home Assistant's job, not this file's), and
AlertTracker's issued/cleared lifecycle including quiet-hours suppression.

NWS alerts are GeoJSON features; fixtures below mirror the real shape
(feature["properties"][...]) per the task brief.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from stormwatch.config import Config
from stormwatch.rules import PRIORITY_ORDER, AlertTracker, RuleEngine, priority_rank

FIXTURES = Path(__file__).parent / "fixtures"
VALID_YAML = FIXTURES / "alerts_valid.yaml"
INVALID_YAML = FIXTURES / "alerts_invalid.yaml"


def _config(**overrides: object) -> Config:
    base: dict[str, object] = {
        "latitude": 34.0234,
        "longitude": -84.6155,
        "mqtt_host": "192.168.1.10",
        "nws_contact": "you@example.com",
        "alerts_critical": ("Tornado Warning", "Flash Flood Emergency"),
        "alerts_high": ("Severe Thunderstorm Warning", "Flash Flood Warning"),
        "alerts_normal": ("Tornado Watch", "Severe Thunderstorm Watch"),
        "quiet_hours": (22, 7),
    }
    base.update(overrides)
    return Config(**base)


def _alert(
    *,
    id: str = "urn:oid:nws.A1",  # noqa: A002 - mirrors NWS GeoJSON property name
    event: str = "Tornado Warning",
    severity: str = "Extreme",
    urgency: str = "Immediate",
    certainty: str = "Observed",
    response: str = "Shelter",
    headline: str = "",
    description: str = "",
) -> dict:
    """A minimal NWS GeoJSON alert feature."""
    return {
        "properties": {
            "id": id,
            "event": event,
            "severity": severity,
            "urgency": urgency,
            "certainty": certainty,
            "response": response,
            "headline": headline,
            "description": description,
        }
    }


# --- env layer -------------------------------------------------------------


def test_env_layer_matches_configured_event_to_priority() -> None:
    engine = RuleEngine(_config())

    assert engine.evaluate(_alert(event="Tornado Warning")) == "critical"
    assert engine.evaluate(_alert(event="Severe Thunderstorm Warning")) == "high"
    assert engine.evaluate(_alert(event="Tornado Watch")) == "normal"


def test_env_layer_unmatched_event_returns_none() -> None:
    engine = RuleEngine(_config())

    assert engine.evaluate(_alert(event="Special Weather Statement")) is None


# --- yaml layer: overrides env entirely -------------------------------------


def test_yaml_layer_overrides_env_layer_entirely_when_loaded() -> None:
    # Env layer's critical list deliberately does NOT include "Tornado
    # Warning" so we can prove the yaml layer fully replaces it rather than
    # merging.
    config = _config(alerts_critical=("Special Weather Statement",))
    engine = RuleEngine(config)

    assert engine.evaluate(_alert(event="Special Weather Statement")) == "critical"

    assert engine.load(str(VALID_YAML)) is True

    # Env-only match no longer fires...
    assert engine.evaluate(_alert(event="Special Weather Statement")) is None
    # ...while the yaml rule now does, even though it was absent from env config.
    assert engine.evaluate(_alert(event="Tornado Warning")) == "critical"


# --- yaml layer: match semantics --------------------------------------------


def test_match_event_regex() -> None:
    engine = RuleEngine(_config())
    assert engine.load(str(VALID_YAML)) is True

    alert = _alert(event="Flood Watch", severity="Severe")
    assert engine.evaluate(alert) == "normal"


def test_match_severity_floor_via_defaults_min_severity() -> None:
    engine = RuleEngine(_config())
    assert engine.load(str(VALID_YAML)) is True

    # Watches rule has no explicit match.severity, so defaults.min_severity
    # (Moderate) applies as a floor.
    below_floor = _alert(event="Flood Watch", severity="Minor")
    at_floor = _alert(event="Flood Watch", severity="Severe")

    assert engine.evaluate(below_floor) is None
    assert engine.evaluate(at_floor) == "normal"


def test_match_urgency_list() -> None:
    engine = RuleEngine(_config())
    assert engine.load(str(VALID_YAML)) is True

    matching = _alert(event="Severe Thunderstorm Warning", urgency="Immediate")
    non_matching = _alert(event="Severe Thunderstorm Warning", urgency="Past")

    assert engine.evaluate(matching) == "high"
    assert engine.evaluate(non_matching) is None


def test_disabled_rule_is_skipped() -> None:
    engine = RuleEngine(_config())
    assert engine.load(str(VALID_YAML)) is True

    # Winter weather rule ships enabled: false in the design-doc example fixture.
    assert engine.evaluate(_alert(event="Winter Storm Warning", severity="Severe")) is None


# --- yaml layer: schema validation ------------------------------------------


def test_invalid_yaml_keeps_previous_env_rules_and_sets_last_error() -> None:
    engine = RuleEngine(_config())

    assert engine.load(str(INVALID_YAML)) is False
    assert engine.last_error is not None
    assert "priority" in engine.last_error

    # Previous (env-layer) rules are untouched.
    assert engine.evaluate(_alert(event="Tornado Warning")) == "critical"


def test_invalid_yaml_keeps_previously_loaded_good_yaml() -> None:
    engine = RuleEngine(_config())
    assert engine.load(str(VALID_YAML)) is True
    assert engine.last_error is None

    assert engine.load(str(INVALID_YAML)) is False
    assert engine.last_error is not None

    # Still evaluating against the previously loaded good yaml, not reverted
    # to env layer and not broken.
    assert engine.evaluate(_alert(event="Tornado Warning")) == "critical"


# --- load(): regression coverage for retain-previous-on-error ---------------


def test_load_missing_file_returns_false_keeps_previous_rules_and_sets_last_error(
    tmp_path: Path,
) -> None:
    engine = RuleEngine(_config())
    missing_path = tmp_path / "does_not_exist.yaml"

    assert engine.load(str(missing_path)) is False
    assert engine.last_error is not None

    # Previous (env-layer) rules are untouched and still evaluate correctly.
    assert engine.evaluate(_alert(event="Tornado Warning")) == "critical"


def test_load_malformed_yaml_syntax_returns_false_keeps_previous_rules_and_sets_last_error(
    tmp_path: Path,
) -> None:
    engine = RuleEngine(_config())
    path = tmp_path / "broken.yaml"
    path.write_text("rules: [unclosed", encoding="utf-8")

    assert engine.load(str(path)) is False
    assert engine.last_error is not None

    # Previous (env-layer) rules are untouched and still evaluate correctly.
    assert engine.evaluate(_alert(event="Tornado Warning")) == "critical"


# --- include_description ------------------------------------------------------


def test_evaluate_detail_returns_three_tuple_with_include_description() -> None:
    engine = RuleEngine(_config())
    assert engine.load(str(VALID_YAML)) is True

    priority, quiet_flag, include_description = engine.evaluate_detail(
        _alert(event="Tornado Warning")
    )
    assert priority == "critical"
    assert quiet_flag is False
    assert include_description is True  # absent from fixture rule -> defaults True


def test_env_layer_rules_always_include_description_true() -> None:
    engine = RuleEngine(_config())

    _, _, include_description = engine.evaluate_detail(_alert(event="Tornado Warning"))
    assert include_description is True


def test_include_description_false_blanks_description_on_issued_event(
    tmp_path: Path,
) -> None:
    yaml_text = """\
version: 1
defaults:
  priority: ignore
  min_severity: Moderate
rules:
  - name: Tornado Warning
    match:
      event: ["Tornado Warning"]
    priority: critical
    include_description: false
"""
    path = tmp_path / "alerts.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    engine = RuleEngine(_config())
    assert engine.load(str(path)) is True

    tracker = AlertTracker()
    alert = _alert(
        id="A1", event="Tornado Warning", severity="Extreme", description="Take shelter now"
    )
    events = tracker.diff([alert], engine, NOON)

    assert len(events) == 1
    assert events[0].kind == "issued"
    assert events[0].description == ""


def test_include_description_default_true_populates_description(tmp_path: Path) -> None:
    yaml_text = """\
version: 1
defaults:
  priority: ignore
  min_severity: Moderate
rules:
  - name: Tornado Warning
    match:
      event: ["Tornado Warning"]
    priority: critical
"""
    path = tmp_path / "alerts.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    engine = RuleEngine(_config())
    assert engine.load(str(path)) is True

    tracker = AlertTracker()
    alert = _alert(
        id="A1", event="Tornado Warning", severity="Extreme", description="Take shelter now"
    )
    events = tracker.diff([alert], engine, NOON)

    assert len(events) == 1
    assert events[0].kind == "issued"
    assert events[0].description == "Take shelter now"


def test_include_description_rejects_non_bool_value(tmp_path: Path) -> None:
    yaml_text = """\
version: 1
defaults:
  priority: ignore
  min_severity: Moderate
rules:
  - name: Tornado Warning
    match:
      event: ["Tornado Warning"]
    priority: critical
    include_description: "yes"
"""
    path = tmp_path / "alerts.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    engine = RuleEngine(_config())
    assert engine.load(str(path)) is False
    assert engine.last_error is not None


# --- PRIORITY_ORDER / priority_rank -------------------------------------------


def test_priority_order_constant() -> None:
    assert PRIORITY_ORDER == ("critical", "high", "normal")


def test_priority_rank_lower_is_more_severe() -> None:
    assert priority_rank("critical") == 0
    assert priority_rank("high") == 1
    assert priority_rank("normal") == 2
    assert priority_rank("critical") < priority_rank("high") < priority_rank("normal")


def test_priority_rank_unknown_priority_returns_len_priority_order() -> None:
    assert priority_rank("ignore") == len(PRIORITY_ORDER)
    assert priority_rank("bogus") == len(PRIORITY_ORDER)


# --- generate_default --------------------------------------------------------


def _load_default_engine(tmp_path: Path) -> RuleEngine:
    """Write generate_default()'s output to disk and load it into a fresh engine."""
    engine = RuleEngine(_config())
    path = tmp_path / "alerts.yaml"
    engine.generate_default(str(path))
    loaded = RuleEngine(_config())
    assert loaded.load(str(path)) is True
    return loaded


def test_generate_default_round_trips(tmp_path: Path) -> None:
    engine = RuleEngine(_config())
    path = tmp_path / "alerts.yaml"

    engine.generate_default(str(path))

    assert path.exists()
    loaded = RuleEngine(_config())
    assert loaded.load(str(path)) is True


def test_generate_default_priorities(tmp_path: Path) -> None:
    # Broad-coverage default (owner decision 2026-08-09): essentially every
    # NWS warning/watch/advisory emits an event, prioritized -- filtering
    # into a notification is Home Assistant's job, not this file's.
    loaded = _load_default_engine(tmp_path)

    cases = {
        "Tornado Warning": "critical",
        "Hurricane Warning": "critical",
        "Winter Storm Warning": "high",
        "Freeze Warning": "normal",
        "Frost Advisory": "normal",
        "Flood Warning": "normal",  # regex catch-all, not individually listed
        "Wind Advisory": "normal",
        "Special Weather Statement": None,  # not a warning/watch/advisory -> ignored
    }
    for event, expected in cases.items():
        assert loaded.evaluate(_alert(event=event, severity="Severe")) == expected, event


def test_generate_default_wind_advisory_is_normal_and_quiet_hours(tmp_path: Path) -> None:
    loaded = _load_default_engine(tmp_path)

    priority, quiet_flag, _include_description = loaded.evaluate_detail(
        _alert(event="Wind Advisory", severity="Severe")
    )
    assert priority == "normal"
    assert quiet_flag is True


def test_generate_default_tornado_warning_matches_before_generic_warning_catch_all(
    tmp_path: Path,
) -> None:
    """Rule evaluation is first-match (RuleEngine.evaluate_detail): the
    'Catastrophic warnings' rule must be matched before the later
    '.*Warning$' catch-all rule, or Tornado Warning would silently degrade
    from critical to normal.
    """
    loaded = _load_default_engine(tmp_path)

    assert loaded.evaluate(_alert(event="Tornado Warning", severity="Extreme")) == "critical"


def test_generate_default_quiet_hours_behavior_cold_vs_catchall(tmp_path: Path) -> None:
    """Frost Advisory and Freeze Watch match the Cold protection rule (not
    quiet-hours-enabled), while Wind Advisory matches the catch-all with
    quiet-hours enabled. Verify first-match ensures cold alerts don't
    get quiet-flagged by the later Watch/Advisory catch-all.
    """
    loaded = _load_default_engine(tmp_path)

    # Cold-specific rules: quiet_flag=False (no quiet-hours suppression)
    frost_advisory_priority, frost_quiet, _ = loaded.evaluate_detail(
        _alert(event="Frost Advisory", severity="Severe")
    )
    assert frost_advisory_priority == "normal"
    assert frost_quiet is False

    freeze_watch_priority, freeze_quiet, _ = loaded.evaluate_detail(
        _alert(event="Freeze Watch", severity="Severe")
    )
    assert freeze_watch_priority == "normal"
    assert freeze_quiet is False

    # Catch-all rule: quiet_flag=True (proves catch-all still quiet-hours-enabled)
    wind_advisory_priority, wind_quiet, _ = loaded.evaluate_detail(
        _alert(event="Wind Advisory", severity="Severe")
    )
    assert wind_advisory_priority == "normal"
    assert wind_quiet is True


# --- AlertTracker ------------------------------------------------------------

NOON = datetime(2026, 8, 9, 12, 0, 0)
LATE_NIGHT = datetime(2026, 8, 9, 23, 0, 0)


def test_tracker_new_matched_alert_issued_once() -> None:
    engine = RuleEngine(_config())
    tracker = AlertTracker()
    alert = _alert(id="A1", event="Tornado Warning", severity="Extreme")

    first = tracker.diff([alert], engine, NOON)
    assert len(first) == 1
    assert first[0].kind == "issued"
    assert first[0].priority == "critical"
    assert first[0].alert_id == "A1"

    second = tracker.diff([alert], engine, NOON)
    assert second == []


def test_tracker_severity_upgrade_reissues() -> None:
    engine = RuleEngine(_config())
    tracker = AlertTracker()

    moderate = _alert(id="A1", event="Tornado Warning", severity="Moderate")
    extreme = _alert(id="A1", event="Tornado Warning", severity="Extreme")

    first = tracker.diff([moderate], engine, NOON)
    assert len(first) == 1 and first[0].kind == "issued"

    second = tracker.diff([extreme], engine, NOON)
    assert len(second) == 1
    assert second[0].kind == "issued"
    assert second[0].alert_id == "A1"


def test_tracker_disappeared_alert_clears() -> None:
    engine = RuleEngine(_config())
    tracker = AlertTracker()
    alert = _alert(id="A1", event="Tornado Warning", severity="Extreme")

    tracker.diff([alert], engine, NOON)
    events = tracker.diff([], engine, NOON)

    assert len(events) == 1
    assert events[0].kind == "cleared"
    assert events[0].alert_id == "A1"
    assert events[0].priority == "critical"


def test_tracker_issued_event_includes_severity_urgency_certainty() -> None:
    engine = RuleEngine(_config())
    tracker = AlertTracker()
    alert = _alert(
        id="A1",
        event="Tornado Warning",
        severity="Extreme",
        urgency="Immediate",
        certainty="Observed",
    )

    events = tracker.diff([alert], engine, NOON)

    assert len(events) == 1
    assert events[0].severity == "Extreme"
    assert events[0].urgency == "Immediate"
    assert events[0].certainty == "Observed"


def test_tracker_cleared_event_includes_severity_urgency_certainty() -> None:
    engine = RuleEngine(_config())
    tracker = AlertTracker()
    alert = _alert(
        id="A1",
        event="Tornado Warning",
        severity="Extreme",
        urgency="Immediate",
        certainty="Observed",
    )

    tracker.diff([alert], engine, NOON)
    events = tracker.diff([], engine, NOON)

    assert len(events) == 1
    assert events[0].kind == "cleared"
    assert events[0].severity == "Extreme"
    assert events[0].urgency == "Immediate"
    assert events[0].certainty == "Observed"


def test_tracker_issued_event_severity_urgency_certainty_blank_when_absent() -> None:
    engine = RuleEngine(_config())
    tracker = AlertTracker()
    alert = {
        "properties": {
            "id": "A1",
            "event": "Tornado Warning",
            "headline": "",
            "description": "",
            # severity/urgency/certainty deliberately absent
        }
    }

    events = tracker.diff([alert], engine, NOON)

    assert len(events) == 1
    assert events[0].severity == ""
    assert events[0].urgency == ""
    assert events[0].certainty == ""


def test_tracker_quiet_hours_suppresses_normal_issue_at_2300_and_emits_at_noon() -> None:
    config = _config()
    engine = RuleEngine(config)
    assert engine.load(str(VALID_YAML)) is True

    # Watches rule: priority normal, quiet_hours: true.
    alert = _alert(id="W1", event="Flood Watch", severity="Severe")

    suppressed = AlertTracker().diff([alert], engine, LATE_NIGHT)
    assert suppressed == []

    emitted = AlertTracker().diff([alert], engine, NOON)
    assert len(emitted) == 1
    assert emitted[0].kind == "issued"
    assert emitted[0].priority == "normal"
