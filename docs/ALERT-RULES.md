# Alert rules

How StormWatch decides which NWS events become notifications, at what priority, and how to change
that.

For the related environment variables (`ALERTS_CRITICAL` / `ALERTS_HIGH` / `ALERTS_NORMAL`,
`QUIET_HOURS`), see [CONFIGURATION.md](CONFIGURATION.md#common).

## Precedence

When more than one setting covers the same event, StormWatch uses the first of these:

1. **Home Assistant dropdowns.** A level chosen in a `select.stormwatch_<event name>` dropdown is
   saved in `/config/alert_levels.json` and wins for that event.
2. **`/config/alerts.yaml` rules.** Used for every event without a dropdown choice.
3. **`ALERTS_CRITICAL` / `ALERTS_HIGH` / `ALERTS_NORMAL`.** Fallback only, used when
   `alerts.yaml` is missing or invalid.

Use the dropdowns for everyday changes, such as making Flood Watch louder or turning off an event
you do not want. Use `alerts.yaml` for advanced matching: regex, severity filters and quiet hours.
An event set from a dropdown ignores quiet hours and the minimum severity setting.

To hand an event back to `alerts.yaml`, remove its line from `alert_levels.json` and restart the
container. See
[Change alert levels from Home Assistant](HOME-ASSISTANT.md#change-alert-levels-from-home-assistant).

## How alert levels are controlled

**`/config/alerts.yaml` is the rule file.** StormWatch writes this file on first start if it is
missing. Once it loads, it replaces the environment-variable lists entirely. Edit it to change
which events go to which level. Changes are hot-reloaded: the container checks the file every 30
seconds, so no restart is needed.

**`ALERTS_CRITICAL` / `ALERTS_HIGH` / `ALERTS_NORMAL` are a fallback only.** They are
comma-separated NWS event names, and they are used only when `/config/alerts.yaml` is missing or
fails validation. Typing an event into one of these variables does **not** add it to a working
install. Leave them blank (the default) to use the built-in lists, which match the default
`alerts.yaml` below.

### Default levels at a glance

A fresh install ships with three levels. What each one does on your phone depends on the Home
Assistant blueprint; the descriptions below are for the StormWatch severe-alerts blueprint (see
[HOME-ASSISTANT.md](HOME-ASSISTANT.md#what-each-level-does-on-your-phone)).

**Critical: "WAKE ME UP"** (13 events). Loud critical alert. Breaks through Do Not Disturb and the
silent switch.

Civil Danger Warning, Earthquake Warning, Extreme Wind Warning, Flash Flood Emergency, Hazardous Materials Warning, Hurricane Warning, Nuclear Power Plant Warning, Radiological Hazard Warning, Shelter In Place Warning, Storm Surge Warning, Tornado Warning, Tsunami Warning, Typhoon Warning

**High: "Heads-up"** (27 events). Pops up on screen with no sound by default.

Ashfall Warning, Avalanche Warning, Blizzard Warning, Blowing Dust Warning, Coastal Flood Warning, Dust Storm Warning, Extreme Cold Warning, Extreme Heat Warning, Fire Warning, Flash Flood Warning, Flash Flood Watch, Flood Warning, High Surf Warning, High Wind Warning, Hurricane Force Wind Warning, Hurricane Watch, Ice Storm Warning, Lake Effect Snow Warning, Lakeshore Flood Warning, Severe Thunderstorm Warning, Severe Thunderstorm Watch, Snow Squall Warning, Tornado Watch, Tropical Storm Warning, Tropical Storm Watch, Volcano Warning, Winter Storm Warning

**Normal: "Silent push"** (26 named events). Goes straight to the notification list, with no
pop-up and no sound.

Avalanche Watch, Coastal Flood Watch, Extreme Cold Watch, Extreme Heat Watch, Fire Weather Watch, Flood Watch, Freeze Warning, Freeze Watch, Gale Warning, Gale Watch, Hazardous Seas Warning, Hazardous Seas Watch, Heavy Freezing Spray Warning, Heavy Freezing Spray Watch, High Wind Watch, Hurricane Force Wind Watch, Lakeshore Flood Watch, Law Enforcement Warning, Red Flag Warning, Special Marine Warning, Storm Surge Watch, Storm Warning, Storm Watch, Tsunami Watch, Typhoon Watch, Winter Storm Watch

Also normal:

- Cold protection: Frost Advisory, Hard Freeze Warning, Hard Freeze Watch.
- Any other warning or watch NWS issues that is not listed above (the name ends in "Warning" or
  "Watch").
- Any advisory (the name ends in "Advisory"). Advisories are dropped during `QUIET_HOURS` (default
  22:00-07:00). Nothing else is dropped overnight: warnings and watches always get through.

Not emitted: statements and outlooks. They still appear in the attributes of
`sensor.stormwatch_active_alerts`.

### Default `alerts.yaml`

This is the file StormWatch writes on first start. Rules are evaluated first-match, top to bottom.

```yaml
version: 1

# Alert levels (owner decision 2026-10-09). Home Assistant decides how each level reaches the phone;
# with the StormWatch severe-alerts blueprint:
#   critical = WAKE ME UP: loud, breaks through Do Not Disturb and silent mode
#   high     = Heads-up: pops up on screen, no sound (sound is a blueprint option)
#   normal   = Silent push: goes straight to the notification list, no pop-up, no sound
# First matching rule wins. Edit freely -- this file is hot-reloaded within 30 seconds.

defaults:
  priority: ignore          # statements/outlooks not matched below are dropped from EVENTS
                            # (they still appear in sensor.stormwatch_active_alerts attributes)
  min_severity: Minor

rules:
  - name: Wake me up
    match:
      event: [
        "Civil Danger Warning", "Earthquake Warning", "Extreme Wind Warning",
        "Flash Flood Emergency", "Hazardous Materials Warning", "Hurricane Warning",
        "Nuclear Power Plant Warning", "Radiological Hazard Warning",
        "Shelter In Place Warning", "Storm Surge Warning", "Tornado Warning",
        "Tsunami Warning", "Typhoon Warning"
      ]
    priority: critical
    include_description: true

  - name: Heads-up
    match:
      event: [
        "Ashfall Warning", "Avalanche Warning", "Blizzard Warning", "Blowing Dust Warning",
        "Coastal Flood Warning", "Dust Storm Warning", "Extreme Cold Warning",
        "Extreme Heat Warning", "Fire Warning", "Flash Flood Warning", "Flash Flood Watch",
        "Flood Warning", "High Surf Warning", "High Wind Warning",
        "Hurricane Force Wind Warning", "Hurricane Watch", "Ice Storm Warning",
        "Lake Effect Snow Warning", "Lakeshore Flood Warning", "Severe Thunderstorm Warning",
        "Severe Thunderstorm Watch", "Snow Squall Warning", "Tornado Watch",
        "Tropical Storm Warning", "Tropical Storm Watch", "Volcano Warning",
        "Winter Storm Warning"
      ]
    priority: high

  - name: Silent push
    match:
      event: [
        "Avalanche Watch", "Coastal Flood Watch", "Extreme Cold Watch", "Extreme Heat Watch",
        "Fire Weather Watch", "Flood Watch", "Freeze Warning", "Freeze Watch", "Gale Warning",
        "Gale Watch", "Hazardous Seas Warning", "Hazardous Seas Watch",
        "Heavy Freezing Spray Warning", "Heavy Freezing Spray Watch", "High Wind Watch",
        "Hurricane Force Wind Watch", "Lakeshore Flood Watch", "Law Enforcement Warning",
        "Red Flag Warning", "Special Marine Warning", "Storm Surge Watch", "Storm Warning",
        "Storm Watch", "Tsunami Watch", "Typhoon Watch", "Winter Storm Watch"
      ]
    priority: normal

  - name: Cold protection          # frost - plants, pipes, pets
    match:
      event: ["Frost Advisory", "Hard Freeze Warning", "Hard Freeze Watch"]
    priority: normal

  - name: Any other warning or watch   # safety net for new NWS event names
    match:
      event_regex: ".*(Warning|Watch)$"
    priority: normal

  - name: Advisories                # silent; dropped during QUIET_HOURS (default 22:00-07:00)
    match:
      event_regex: ".*Advisory$"
    priority: normal
    quiet_hours: true
```

Because the first matching rule wins, the named lists come before the catch-all rules. A Tornado
Warning matches "Wake me up" and stops there. It can never fall through to the broader
`.*(Warning|Watch)$` rule and be demoted to `normal`.

To change a level, move an event name from one rule's `event` list to another's, or add a new rule
above the catch-alls. You can also add rules, flip `enabled` on a rule, or raise `min_severity`.
Do not edit the `ALERTS_*` variables for this; they are ignored while `alerts.yaml` is valid.

### Updating an existing install

`alerts.yaml` is only written when it is missing, so an existing install keeps its current file
when you update the container. To adopt the new defaults:

1. Update the container to the new image.
2. Rename `/config/alerts.yaml` (on Unraid, the file is in `/mnt/user/appdata/stormwatch/`), for
   example to `alerts.yaml.old`. Keep it if you customized the old file.
3. Restart the container. A fresh default `alerts.yaml` is written.

Fresh installs get the new defaults automatically once they run the new image version.

## Match fields

Each rule's `match` block can specify any combination of:

- `event` — a list of exact NWS event names (e.g. `["Tornado Warning"]`)
- `event_regex` — a regular expression against the event name (e.g. `".*Watch$"`)
- `severity` — NWS severity (`Extreme`, `Severe`, `Moderate`, `Minor`, `Unknown`)
- `urgency` — NWS urgency (`Immediate`, `Expected`, `Future`, `Past`, `Unknown`)
- `certainty` — NWS certainty (`Observed`, `Likely`, `Possible`, `Unlikely`, `Unknown`)
- `response` — NWS recommended response type

All fields are optional. **Every field you do specify must match. It is AND, not OR.** For example,
a rule with `event: ["Tornado Warning"]` and `urgency: ["Immediate"]` only fires when the event
name matches *and* urgency is `Immediate`. An alert that fails one field falls through to the next
rule, or to `defaults.priority: ignore` if nothing else matches. The default rules match on event
name only.

`defaults.min_severity` is a floor applied per rule: for a rule whose `match` block does **not**
specify `severity`, an alert below that floor (`Minor` in the shipped default — barely a floor at
all, by design) fails that rule. A rule that **does** specify an explicit `severity` list is exempt
from the floor entirely — the explicit list is the whole check for that field, overriding
`min_severity` rather than adding to it. This lets a rule deliberately reach below the floor (e.g.
`severity: ["Unknown"]`) without lowering `min_severity` globally for every other rule.

## Priorities

| Priority | Name | What it does |
|---|---|---|
| `critical` | WAKE ME UP | Bypasses Do Not Disturb in Home Assistant. The iOS critical-alert push (`critical: 1` in the notification payload) plays a sound through Do Not Disturb and a muted ringer. Reserved for events that must never be missed, e.g. Tornado Warning. See [HOME-ASSISTANT.md](HOME-ASSISTANT.md#ios-critical-alerts) for the full payload and the companion-app setting it requires. |
| `high` | Heads-up | With the severe-alerts blueprint: pops up on screen with no sound by default. The blueprint input "Heads-up alerts make a sound" turns the sound on. Does not bypass Do Not Disturb. |
| `normal` | Silent push | With the severe-alerts blueprint: goes straight to the notification list, with no pop-up and no sound. Also covers the lightning all-clear event (`stormwatch/event/all_clear`). An all-clear should never wake anyone up. |
| `ignore` | Off | Dropped. Nothing is published for it. This is `defaults.priority`, so any alert that does not match a rule is ignored by design. |

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
