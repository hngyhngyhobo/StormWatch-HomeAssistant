# Alert rules

How StormWatch decides which NWS events become notifications, at what priority, and how to change
that. Applies to the Phase 1 alert engine, which is shipping today.

For the underlying environment variables (`ALERTS_CRITICAL` / `ALERTS_HIGH` / `ALERTS_NORMAL`,
`QUIET_HOURS`), see [CONFIGURATION.md](CONFIGURATION.md#common).

## Two layers of configuration

**Layer 1 — environment variables**, for the 90% case:

```
ALERTS_CRITICAL = Tornado Warning,Flash Flood Emergency
ALERTS_HIGH     = Severe Thunderstorm Warning,Flash Flood Warning
ALERTS_NORMAL   = Tornado Watch,Severe Thunderstorm Watch
```

Comma-separated NWS event names. Add a new alert type by typing it into the Unraid template field
(or your `.env` file) and restarting. No file editing required.

**Layer 2 — `/config/alerts.yaml`**, for full control. This file is auto-generated with sensible
defaults on first run, and **hot-reloaded on change** (the container polls its modification time
every 30 seconds) — no restart needed to pick up an edit.

### Default `alerts.yaml`

This is the file StormWatch generates on first run — it matches the container's actual generated
config (`src/stormwatch/rules.py`). The philosophy (owner decision 2026-08-09): the default emits
events for every warning/watch/advisory — filter what becomes a notification in Home Assistant (by
priority or event type); trim rules here only if you want alerts to never emit at all.

```yaml
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
    quiet_hours: true       # suppressed 22:00–07:00
```

Rules are evaluated first-match, top to bottom, which is why the catastrophic and major warnings
are listed explicitly before the broader `.*Warning$` and `.*(Watch|Advisory)$` catch-alls further
down — a Tornado Warning matches "Catastrophic warnings" and stops there, so it can never fall
through and get demoted to `normal` by the general warning catch-all.

Edit `/config/alerts.yaml` directly to change this — add rules, change priorities, flip `enabled`
on any rule, or tighten `min_severity`. There's no need to also touch the Layer 1 env vars;
whichever layer last defines a given rule wins for that rule.

## Match fields

Each rule's `match` block can specify any combination of:

- `event` — a list of exact NWS event names (e.g. `["Tornado Warning"]`)
- `event_regex` — a regular expression against the event name (e.g. `".*Watch$"`)
- `severity` — NWS severity (`Extreme`, `Severe`, `Moderate`, `Minor`, `Unknown`)
- `urgency` — NWS urgency (`Immediate`, `Expected`, `Future`, `Past`, `Unknown`)
- `certainty` — NWS certainty (`Observed`, `Likely`, `Possible`, `Unlikely`, `Unknown`)
- `response` — NWS recommended response type

All fields are optional. **Every field you do specify must match — it's AND, not OR.** The "Severe
Thunderstorm Warning" rule above only fires when the event name matches *and* urgency is
`Immediate` or `Expected`; a thunderstorm warning issued with some other urgency value falls
through to the next rule (or to `defaults.priority: ignore` if nothing else matches).

`defaults.min_severity` is a floor applied per rule: for a rule whose `match` block does **not**
specify `severity`, an alert below that floor (`Minor` in the shipped default — barely a floor at
all, by design) fails that rule. A rule that **does** specify an explicit `severity` list is exempt
from the floor entirely — the explicit list is the whole check for that field, overriding
`min_severity` rather than adding to it. This lets a rule deliberately reach below the floor (e.g.
`severity: ["Unknown"]`) without lowering `min_severity` globally for every other rule.

## Priorities

| Priority | What it does |
|---|---|
| `critical` | Bypasses Do Not Disturb in Home Assistant — the iOS critical-alert push (`critical: 1` in the notification payload) plays a sound through Do Not Disturb and a muted ringer. Reserved for events that must never be missed, e.g. Tornado Warning. See [HOME-ASSISTANT.md](HOME-ASSISTANT.md#ios-critical-alerts) for the full payload and the companion-app setting it requires. |
| `high` | A normal push notification with a distinct, audible sound. Does not bypass Do Not Disturb. |
| `normal` | Silent — no sound, no Do Not Disturb bypass. Used for watches, and also covers the lightning all-clear event (`stormwatch/event/all_clear`) — an all-clear should never wake anyone up. |
| `ignore` | Dropped. Nothing is published for it. This is `defaults.priority`, so any alert that doesn't match a rule is silently ignored by design. |

## Validation

On load, `alerts.yaml` is schema-checked. **If it's invalid — a YAML syntax error, an unknown
field, a bad priority value — the file is rejected and the previous good configuration is kept
running.** The error is published to a diagnostic sensor under the StormWatch device in Home
Assistant so you can see what's wrong without digging through container logs. A typo at 2 a.m. must
never silently disable your tornado warnings.

## Dedup and lifecycle (§7.2)

NWS reissues and updates alerts constantly — a long-running severe thunderstorm warning can be
republished by NWS a dozen times with no real change. StormWatch tracks alerts by their NWS `id`
and only emits an event on:

- **New** — an alert matching a rule that wasn't previously active → `stormwatch/event/alert_issued`
- **Upgraded** — an existing tracked alert's severity increases → re-emitted on
  `stormwatch/event/alert_issued`
- **Expired or cancelled** → `stormwatch/event/alert_cleared`

Reissues with identical content are suppressed — you get notified once per real change, not once
per NWS polling cycle.
