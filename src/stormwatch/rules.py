"""Alert rule engine: env-var layer + /config/alerts.yaml with hot reload
(DESIGN.md §7).

Invalid config is rejected and the previous good config retained — a typo
must never silently disable tornado warnings. Dedup by NWS id; emit only on
new, upgraded, or cleared alerts (§7.2).

NWS alerts are GeoJSON features; all matching happens on
``feature["properties"]`` keys: event, severity, urgency, certainty,
response, id, headline, description.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime

import yaml

from .config import Config

logger = logging.getLogger("stormwatch.rules")

_VALID_PRIORITIES = {"critical", "high", "normal", "ignore"}
_SEVERITY_RANK = {"Unknown": 0, "Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}
_MATCH_LIST_FIELDS = ("event", "severity", "urgency", "certainty", "response")
_KNOWN_MATCH_KEYS = set(_MATCH_LIST_FIELDS) | {"event_regex"}
_KNOWN_RULE_KEYS = {"name", "match", "priority", "enabled", "quiet_hours", "include_description"}

# Priorities in descending severity order (lower rank = more severe). Excludes
# 'ignore', which is not an emitted priority.
PRIORITY_ORDER: tuple[str, ...] = ("critical", "high", "normal")


def priority_rank(p: str) -> int:
    """Rank a priority for severity comparisons; lower is more severe.

    Unknown/unrecognized priorities (including 'ignore') sort last, past
    every real priority, via ``len(PRIORITY_ORDER)``.
    """
    try:
        return PRIORITY_ORDER.index(p)
    except ValueError:
        return len(PRIORITY_ORDER)


# Default /config/alerts.yaml (owner decision 2026-08-09): broad coverage by
# design -- essentially every NWS warning/watch/advisory emits an event,
# prioritized by severity. Deciding what becomes a notification (vs. just an
# entity/attribute update) is Home Assistant's job, not this file's; trim
# rules here only if you want an alert type to never emit at all.
_DEFAULT_ALERTS_YAML = """\
version: 1

defaults:
  priority: ignore          # statements/outlooks not matched below are dropped from EVENTS
                            # (they still appear in sensor.stormwatch_active_alerts attributes)
  min_severity: Minor

rules:
  - name: Catastrophic warnings
    match:
      event: ["Tornado Warning", "Flash Flood Emergency", "Extreme Wind Warning",
              "Hurricane Warning"]
    priority: critical      # bypasses Do Not Disturb in HA
    include_description: true

  - name: Major warnings
    match:
      event: ["Severe Thunderstorm Warning", "Flash Flood Warning", "Winter Storm Warning",
              "Ice Storm Warning", "Blizzard Warning", "Tropical Storm Warning",
              "Storm Surge Warning"]
    priority: high

  - name: Cold protection          # freeze/frost — plants, pipes, pets
    match:
      event: ["Freeze Warning", "Frost Advisory", "Freeze Watch",
              "Hard Freeze Warning", "Hard Freeze Watch"]
    priority: normal

  - name: All other warnings       # flood, wind, heat, snow squall, dust, etc.
    match:
      event_regex: ".*Warning$"
    priority: normal

  - name: Watches and advisories
    match:
      event_regex: ".*(Watch|Advisory)$"
    priority: normal
    quiet_hours: true       # suppressed 22:00-07:00
"""


class RuleSchemaError(ValueError):
    """alerts.yaml failed schema validation."""


def _severity_rank(value: str | None) -> int:
    return _SEVERITY_RANK.get(value or "Unknown", 0)


@dataclass(frozen=True)
class _CompiledRule:
    name: str
    match: dict
    priority: str
    enabled: bool
    quiet_hours: bool
    include_description: bool = True


@dataclass(frozen=True)
class _RuleSet:
    rules: tuple[_CompiledRule, ...]
    default_priority: str
    min_severity: str | None  # None => no floor enforced (env layer)


def _build_env_rules(config: Config) -> list[_CompiledRule]:
    """One rule per configured event name, in critical/high/normal order.

    Env-layer rules always include the alert description in full — there is
    no env-var knob for ``include_description``, only the yaml layer.
    """
    rules: list[_CompiledRule] = []
    for event_name in config.alerts_critical:
        rules.append(
            _CompiledRule(
                name=event_name,
                match={"event": [event_name]},
                priority="critical",
                enabled=True,
                quiet_hours=False,
                include_description=True,
            )
        )
    for event_name in config.alerts_high:
        rules.append(
            _CompiledRule(
                name=event_name,
                match={"event": [event_name]},
                priority="high",
                enabled=True,
                quiet_hours=False,
                include_description=True,
            )
        )
    for event_name in config.alerts_normal:
        rules.append(
            _CompiledRule(
                name=event_name,
                match={"event": [event_name]},
                priority="normal",
                enabled=True,
                quiet_hours=False,
                include_description=True,
            )
        )
    return rules


def _rule_matches(rule: _CompiledRule, properties: dict, min_severity: str | None) -> bool:
    match = rule.match
    event = properties.get("event")

    if "event" in match and event not in match["event"]:
        return False
    if "event_regex" in match and (not event or not match["event_regex"].search(event)):
        return False

    severity = properties.get("severity") or "Unknown"
    if "severity" in match:
        if severity not in match["severity"]:
            return False
    elif min_severity is not None and _severity_rank(severity) < _severity_rank(min_severity):
        return False

    for field_name in ("urgency", "certainty", "response"):
        if field_name in match and properties.get(field_name) not in match[field_name]:
            return False

    return True


def _compile_match(match_raw: object, rule_name: str) -> dict:
    if not isinstance(match_raw, dict):
        raise RuleSchemaError(f"rule {rule_name!r}: 'match' must be a mapping.")

    unknown = set(match_raw) - _KNOWN_MATCH_KEYS
    if unknown:
        raise RuleSchemaError(f"rule {rule_name!r}: unknown match field(s) {sorted(unknown)}.")

    compiled: dict = {}
    for field_name in _MATCH_LIST_FIELDS:
        if field_name not in match_raw:
            continue
        value = match_raw[field_name]
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            raise RuleSchemaError(
                f"rule {rule_name!r}: match.{field_name} must be a string or list of strings."
            )
        compiled[field_name] = value

    if "event_regex" in match_raw:
        pattern_str = match_raw["event_regex"]
        if not isinstance(pattern_str, str):
            raise RuleSchemaError(f"rule {rule_name!r}: match.event_regex must be a string.")
        try:
            compiled["event_regex"] = re.compile(pattern_str)
        except re.error as exc:
            raise RuleSchemaError(
                f"rule {rule_name!r}: match.event_regex is not valid regex: {exc}"
            ) from exc

    return compiled


def _compile_ruleset(data: object) -> _RuleSet:
    if not isinstance(data, dict):
        raise RuleSchemaError("alerts.yaml must be a mapping at the top level.")

    rules_raw = data.get("rules")
    if not isinstance(rules_raw, list):
        raise RuleSchemaError("alerts.yaml must define 'rules' as a list.")

    defaults_raw = data.get("defaults", {})
    if not isinstance(defaults_raw, dict):
        raise RuleSchemaError("'defaults' must be a mapping.")

    default_priority = defaults_raw.get("priority", "ignore")
    if default_priority not in _VALID_PRIORITIES:
        raise RuleSchemaError(f"defaults.priority {default_priority!r} is not a valid priority.")

    min_severity = defaults_raw.get("min_severity", "Moderate")
    if min_severity not in _SEVERITY_RANK:
        raise RuleSchemaError(f"defaults.min_severity {min_severity!r} is not a valid severity.")

    compiled_rules: list[_CompiledRule] = []
    for index, raw_rule in enumerate(rules_raw):
        if not isinstance(raw_rule, dict):
            raise RuleSchemaError(f"rules[{index}] must be a mapping.")

        unknown_keys = set(raw_rule) - _KNOWN_RULE_KEYS
        if unknown_keys:
            raise RuleSchemaError(f"rules[{index}]: unknown field(s) {sorted(unknown_keys)}.")

        name = raw_rule.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RuleSchemaError(f"rules[{index}] is missing a valid 'name'.")

        priority = raw_rule.get("priority")
        if priority not in _VALID_PRIORITIES:
            raise RuleSchemaError(f"rule {name!r}: priority {priority!r} is not a valid priority.")

        match_compiled = _compile_match(raw_rule.get("match", {}), name)

        enabled = raw_rule.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RuleSchemaError(f"rule {name!r}: 'enabled' must be true or false.")

        quiet_hours_flag = raw_rule.get("quiet_hours", False)
        if not isinstance(quiet_hours_flag, bool):
            raise RuleSchemaError(f"rule {name!r}: 'quiet_hours' must be true or false.")

        include_description = raw_rule.get("include_description", True)
        if not isinstance(include_description, bool):
            raise RuleSchemaError(f"rule {name!r}: 'include_description' must be true or false.")

        compiled_rules.append(
            _CompiledRule(
                name=name,
                match=match_compiled,
                priority=priority,
                enabled=enabled,
                quiet_hours=quiet_hours_flag,
                include_description=include_description,
            )
        )

    return _RuleSet(
        rules=tuple(compiled_rules),
        default_priority=default_priority,
        min_severity=min_severity,
    )


class RuleEngine:
    """Matches NWS alerts to priorities via layered, hot-reloadable rules."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.last_error: str | None = None
        self._env_ruleset = _RuleSet(
            rules=tuple(_build_env_rules(config)),
            default_priority="ignore",
            min_severity=None,
        )
        self._yaml_ruleset: _RuleSet | None = None

    @property
    def _active_ruleset(self) -> _RuleSet:
        return self._yaml_ruleset if self._yaml_ruleset is not None else self._env_ruleset

    def load(self, path: str) -> bool:
        """Load and schema-validate alerts.yaml; keep previous config on error.

        Returns True and replaces the active (YAML) rule set entirely on
        success. On any read, parse, or schema error, the previously active
        rules are left untouched, ``last_error`` is set, and False is
        returned — a config typo must never silently disable tornado
        warnings.
        """
        try:
            with open(path, encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except OSError as exc:
            self.last_error = f"could not read {path}: {exc}"
            logger.warning(self.last_error)
            return False
        except yaml.YAMLError as exc:
            self.last_error = f"{path} has invalid YAML syntax: {exc}"
            logger.warning(self.last_error)
            return False

        try:
            ruleset = _compile_ruleset(data)
        except RuleSchemaError as exc:
            self.last_error = f"{path} failed schema validation: {exc}"
            logger.warning(self.last_error)
            return False

        self._yaml_ruleset = ruleset
        self.last_error = None
        return True

    def evaluate(self, alert: dict) -> str | None:
        """Return priority ('critical'|'high'|'normal') or None for ignore."""
        priority, _quiet_flag, _include_description = self.evaluate_detail(alert)
        return priority

    def evaluate_detail(self, alert: dict) -> tuple[str | None, bool, bool]:
        """Return (priority, quiet_hours_flag, include_description) for the
        first matching rule.

        ``quiet_hours_flag`` reflects the matched rule's ``quiet_hours``
        setting so AlertTracker can suppress normal-priority issues during
        configured quiet hours. ``include_description`` reflects the matched
        rule's ``include_description`` setting (default True when absent)
        so AlertTracker can blank the alert description when a rule opts
        out of forwarding it.
        """
        properties = (alert.get("properties") or {}) if isinstance(alert, dict) else {}
        ruleset = self._active_ruleset

        for rule in ruleset.rules:
            if not rule.enabled:
                continue
            if _rule_matches(rule, properties, ruleset.min_severity):
                priority = None if rule.priority == "ignore" else rule.priority
                return priority, rule.quiet_hours, rule.include_description

        priority = None if ruleset.default_priority == "ignore" else ruleset.default_priority
        return priority, False, True

    def generate_default(self, path: str) -> None:
        """Write the default alerts.yaml (broad coverage, owner decision 2026-08-09)."""
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_DEFAULT_ALERTS_YAML)


@dataclass(frozen=True)
class AlertEvent:
    """A single issued/cleared transition emitted by AlertTracker.diff().

    ``severity``/``urgency``/``certainty`` mirror the matching NWS
    GeoJSON properties verbatim (task LOC feature B) -- "" when the
    property was absent from the alert, never a placeholder like
    "Unknown".
    """

    kind: str  # 'issued' | 'cleared'
    priority: str
    alert_id: str
    headline: str
    event: str
    description: str
    severity: str
    urgency: str
    certainty: str


@dataclass(frozen=True)
class _TrackedAlert:
    priority: str
    severity: str
    headline: str
    event: str
    description: str
    include_description: bool
    urgency: str
    certainty: str


def _hour_in_quiet_window(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight, e.g. (22, 7)


class AlertTracker:
    """Tracks active NWS alerts across polls; emits issued/cleared events (§7.2)."""

    def __init__(self) -> None:
        self._active: dict[str, _TrackedAlert] = {}

    def diff(self, current: list[dict], engine: RuleEngine, now: datetime) -> list[AlertEvent]:
        """Compare ``current`` alerts against last-seen state.

        Emits 'issued' on a new matched id or a severity upgrade on an
        existing id, and 'cleared' when a previously tracked id disappears.
        Identical reissues (same id, same-or-lower severity) are suppressed.
        Normal-priority issues on a quiet_hours-flagged rule are suppressed
        when ``now``'s local hour falls within ``engine.config.quiet_hours``;
        cleared events are never suppressed.
        """
        events: list[AlertEvent] = []
        seen_ids: set[str] = set()

        for alert in current:
            properties = alert.get("properties") or {} if isinstance(alert, dict) else {}
            alert_id = properties.get("id")
            if not alert_id:
                continue

            priority, quiet_flag, include_description = engine.evaluate_detail(alert)
            if priority is None:
                continue

            seen_ids.add(alert_id)
            # "" (not "Unknown") when absent -- _severity_rank() below still
            # normalizes a falsy value to "Unknown" for ranking purposes, so
            # this doubles as both the severity-upgrade comparison input and
            # the verbatim value forwarded on AlertEvent/_TrackedAlert.
            severity = properties.get("severity") or ""
            urgency = properties.get("urgency") or ""
            certainty = properties.get("certainty") or ""
            headline = properties.get("headline") or ""
            event_name = properties.get("event") or ""
            description = properties.get("description") or ""

            previous = self._active.get(alert_id)
            is_new = previous is None
            is_upgrade = previous is not None and _severity_rank(severity) > _severity_rank(
                previous.severity
            )

            self._active[alert_id] = _TrackedAlert(
                priority=priority,
                severity=severity,
                headline=headline,
                event=event_name,
                description=description,
                include_description=include_description,
                urgency=urgency,
                certainty=certainty,
            )

            if is_new or is_upgrade:
                suppressed = (
                    priority == "normal"
                    and quiet_flag
                    and _hour_in_quiet_window(now.hour, *engine.config.quiet_hours)
                )
                if suppressed:
                    continue
                events.append(
                    AlertEvent(
                        kind="issued",
                        priority=priority,
                        alert_id=alert_id,
                        headline=headline,
                        event=event_name,
                        description=description if include_description else "",
                        severity=severity,
                        urgency=urgency,
                        certainty=certainty,
                    )
                )

        for alert_id in list(self._active):
            if alert_id in seen_ids:
                continue
            stale = self._active.pop(alert_id)
            events.append(
                AlertEvent(
                    kind="cleared",
                    priority=stale.priority,
                    alert_id=alert_id,
                    headline=stale.headline,
                    event=stale.event,
                    description=stale.description if stale.include_description else "",
                    severity=stale.severity,
                    urgency=stale.urgency,
                    certainty=stale.certainty,
                )
            )

        return events
