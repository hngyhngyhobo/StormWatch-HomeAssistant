# Home Assistant integration

How StormWatch's entities get into Home Assistant, what they are today, and how to route them into
notifications — including the iOS critical-alert path.

Install the container first if you haven't:
[INSTALL-UNRAID.md](INSTALL-UNRAID.md) or [INSTALL-DOCKER.md](INSTALL-DOCKER.md).

## Prerequisite: the MQTT integration

StormWatch talks to Home Assistant entirely through MQTT discovery, so Home Assistant's own
**MQTT** integration has to be configured and connected to the same broker StormWatch publishes to
before anything will appear:

1. Home Assistant → Settings → Devices & Services → **Add Integration** → search **MQTT**.
2. Point it at your broker (host, port, and credentials if your broker requires them — the same
   values you gave StormWatch as `MQTT_HOST` / `MQTT_PORT` / `MQTT_USERNAME` / `MQTT_PASSWORD`).
3. Confirm it shows as connected.

If this integration doesn't exist yet, StormWatch will run fine and publish to the broker, but
nothing will show up in Home Assistant until this step is done.

## What discovery does

Home Assistant's MQTT integration listens on a discovery prefix (default `homeassistant`, matching
StormWatch's `DISCOVERY_PREFIX` default — see [CONFIGURATION.md](CONFIGURATION.md#advanced)). The
moment the StormWatch container connects to the broker, it publishes a discovery config for every
entity under one device called **StormWatch**. Home Assistant picks these up automatically:

- No `configuration.yaml` editing.
- No custom integration or HACS install.
- No Home Assistant restart.
- The device (and its entities) simply appears under Settings → Devices & Services → MQTT →
  Devices.

<!-- screenshot: StormWatch device page listing its entities -->

## Entities

These are the entities that exist today. The rest of the entity map described in DESIGN.md §8 —
Xweather call tracking — is **planned for Phase 4 (Xweather)** and does not exist yet. See
[XWEATHER.md](XWEATHER.md) for the Xweather tier specifically.

| Entity | Type | Notes |
|---|---|---|
| `sensor.stormwatch_active_alerts` | count | Count of all active NWS alerts for the location; full alert list in attributes |
| `sensor.stormwatch_highest_alert` | string | Highest-priority active alert (rule-matched) headline; description in attributes |
| `binary_sensor.stormwatch_critical_alert` | binary (`safety`) | On when any rule-matched active alert is `critical` priority |
| `binary_sensor.stormwatch_config_problem` | binary (`problem`, diagnostic) | ON when the alert-rules config has a problem (previous good rules stay active; error text in attributes); OFF when valid. |
| `binary_sensor.stormwatch_nws_available` | binary (`connectivity`, diagnostic) | On when NWS data source is reachable |
| `binary_sensor.stormwatch_connected` | binary (`connectivity`) | LWT-backed availability — off if the container loses its connection |
| `sensor.stormwatch_swim_status` | enum | `CLEAR` / `WATCH` / `CLOSED` — the headline pool/lightning entity |
| `sensor.stormwatch_nearest_strike_distance` | distance | Nearest recent strike, in `mi` or `km` per `UNITS` |
| `sensor.stormwatch_nearest_strike_bearing` | string | Compass direction (e.g. `SW`) of the nearest recent strike; exact degrees in attributes |
| `sensor.stormwatch_strike_count_15m` | count | Strikes in the trailing 15 minutes — a storm-intensity proxy |
| `sensor.stormwatch_all_clear_at` | timestamp | When the all-clear timer expires; renders as a countdown in HA. Reports `None` while `CLEAR`. |
| `binary_sensor.stormwatch_lightning_nearby` | binary (`safety`) | On when swim status is `CLOSED` |
| `binary_sensor.stormwatch_lightning_available` | binary (`connectivity`, diagnostic) | On when the Blitzortung lightning feed is connected |
| `sensor.stormwatch_rain_forecast_today` | rain amount (`in`/`mm`) | Forecast rainfall for the rest of the UTC calendar day, from the NWS gridpoint QPF layer |
| `sensor.stormwatch_rain_forecast_48h` | rain amount (`in`/`mm`) | Forecast rainfall over the next 48 hours (rolling from now) |
| `sensor.stormwatch_rain_last_24h` | rain amount (`in`/`mm`) | Observed rainfall in the trailing 24 hours; hourly breakdown in attributes |
| `sensor.stormwatch_rain_last_7d` | rain amount (`in`/`mm`) | Observed rainfall in the trailing 7 days |
| `binary_sensor.stormwatch_rain_available` | binary (`connectivity`, diagnostic) | On when the NWS rainfall data source is reachable |
| `sensor.stormwatch_strikes` | count | Count of recent lightning strikes (last `STRIKE_MAP_WINDOW_MINUTES`); a GeoJSON `FeatureCollection` of strike points (distance/bearing/age/range per point) is in the `geojson` attribute — see [Lightning strike map](#lightning-strike-map) below |

The lightning/pool entities (rows 7-13, plus `sensor.stormwatch_strikes`) only appear when `BLITZORTUNG_ENABLED` is true (the
default), and the rainfall entities (last five rows) only appear when `RAIN_ENABLED` is true (also
the default, US locations only) — see [CONFIGURATION.md](CONFIGURATION.md#advanced).

`binary_sensor.stormwatch_connected` uses MQTT's Last Will and Testament, so it goes `off`
honestly if the container crashes or loses its network — see the availability doctrine in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#unavailable-entities) if you see entities go
`unavailable`. The same doctrine applies to lightning specifically: if the Blitzortung feed drops,
`binary_sensor.stormwatch_lightning_available` goes off and swim status holds at its last value —
it never silently reports `CLEAR` just because the feed is gone.

## Importing the severe alerts blueprint

The repo ships an importable blueprint that wires `sensor` state into real notifications:
[examples/blueprints/stormwatch_severe_alerts.yaml](../examples/blueprints/stormwatch_severe_alerts.yaml).

1. Home Assistant → Settings → Automations & Scenes → **Blueprints** tab → **Import Blueprint**.
2. Paste the raw GitHub URL:

   ```
   https://raw.githubusercontent.com/hngyhngyhobo/StormWatch-HomeAssistant/main/examples/blueprints/stormwatch_severe_alerts.yaml
   ```

3. Click **Preview** → **Import Blueprint**.
4. Go to the **Automations** tab → **Create Automation** → **Use Blueprint** → **StormWatch —
   Severe Weather Alerts**.
5. Fill in the two inputs:
   - **Notify Device** — pick your phone from the `mobile_app` device list.
   - **Enable Critical Alerts** — on by default; leave it on for tornado warnings to use the iOS
     critical-alert push (see below), or turn it off to route `critical`-priority alerts through a
     normal push instead.
6. Save.

<!-- screenshot: blueprint import screen with the two inputs filled in -->

## Importing the pool alerts blueprint

The repo also ships
[examples/blueprints/stormwatch_pool_alerts.yaml](../examples/blueprints/stormwatch_pool_alerts.yaml)
for the pool/swim notification pair: it fires when StormWatch closes the pool
(`stormwatch/event/lightning_close`) and again when the all-clear timer expires
(`stormwatch/event/all_clear`).

1. Home Assistant → Settings → Automations & Scenes → **Blueprints** tab → **Import Blueprint**.
2. Paste the raw GitHub URL:

   ```
   https://raw.githubusercontent.com/hngyhngyhobo/StormWatch-HomeAssistant/main/examples/blueprints/stormwatch_pool_alerts.yaml
   ```

3. Click **Preview** → **Import Blueprint**.
4. Go to the **Automations** tab → **Create Automation** → **Use Blueprint** → **StormWatch — Pool
   / Swim Alerts**.
5. Fill in the two inputs:
   - **Notify Device** — pick your phone from the `mobile_app` device list.
   - **Enable Critical Alerts for Pool Closed** — on by default; leave it on so a pool-closed
     notification uses the iOS critical-alert push, or turn it off to route it through a normal
     push instead. This toggle only affects the pool-closed notification — the all-clear
     notification is **always** a normal, silent push, with no toggle to change that (§8
     notification discipline: an all-clear should never wake anyone up).
6. Save.

## Importing the tornado warning lamp strobe blueprint

The repo also ships
[examples/blueprints/stormwatch_tornado_strobe.yaml](../examples/blueprints/stormwatch_tornado_strobe.yaml),
which flashes a lamp on and off when StormWatch publishes a **Tornado Warning** — a bedside lamp
strobing at 3 a.m. wakes you when a silent banner won't. It's a companion to the critical push, not
a replacement: run it alongside the Severe Weather Alerts blueprint, which still delivers the iOS
critical-alert sound.

This is entirely a Home Assistant automation — it drives any HA `light` entity (Zigbee, Wi-Fi,
ESPHome) through a normal on/off loop, so **no device or ESPHome firmware changes are needed.**
It fires **only** on an exact NWS `Tornado Warning`; other `critical` alerts (lightning inside
`CLOSE_RADIUS`, Flash Flood Emergency, Extreme Wind Warning) still notify but never touch the lamp.

1. Home Assistant → Settings → Automations & Scenes → **Blueprints** tab → **Import Blueprint**.
2. Paste the raw GitHub URL:

   ```
   https://raw.githubusercontent.com/hngyhngyhobo/StormWatch-HomeAssistant/main/examples/blueprints/stormwatch_tornado_strobe.yaml
   ```

3. Click **Preview** → **Import Blueprint**.
4. Go to the **Automations** tab → **Create Automation** → **Use Blueprint** → **StormWatch —
   Tornado Warning Lamp Strobe**.
5. Fill in the inputs:
   - **Strobe Light** — the lamp to flash (e.g. your nightstand lamp).
   - **Flash Count** / **Flash Interval (ms)** — how many on/off cycles and how long each half stays
     on/off. Total strobe time ≈ Flash Count × 2 × Flash Interval; the defaults (8 × 300 ms) give
     roughly a 5-second strobe.
   - **Strobe Brightness** — brightness of each ON flash (default 100%).
   - **Leave Lamp On After Strobe** — on by default, so the lamp ends up lit and the room stays
     bright while you move to shelter; turn it off to leave the lamp off afterward.
6. Save.

> **`transition: 0`.** The strobe forces a hard on/off with no fade. Many bulbs fade between states
> by default, which would smear the strobe into a slow pulse. If your bulb ignores `transition`, the
> automation still works — the edges are just softer.

Prefer editing YAML directly? The same automation is in
[examples/automations.yaml](../examples/automations.yaml) as `stormwatch_tornado_strobe` — replace
the `light.smart_lamp_1` placeholder with your lamp's real entity ID (find it under Developer Tools
→ States).

## Lightning strike map

`sensor.stormwatch_strikes`'s `geojson` attribute (a GeoJSON `FeatureCollection` of recent strike
points, each with `distance`/`bearing`/`bearing_deg`/`age_seconds`/`range` properties) can be
rendered on an actual map card via the community
[`nathan-gs/ha-map-card`](https://github.com/nathan-gs/ha-map-card) (`custom:map-card`) — not a
built-in Home Assistant card, so it needs installing first.

### Install the card

Via HACS (recommended):

1. HACS → **Frontend** → search **Map card** (`nathan-gs/ha-map-card`) → **Download**.
2. Reload the Home Assistant frontend (or restart) so the new card registers.

Without HACS, manually:

1. Download the card's `dist/ha-map-card.js` from its
   [GitHub releases](https://github.com/nathan-gs/ha-map-card/releases) into `/config/www/`.
2. Home Assistant → Settings → Dashboards → **Resources** (top-right ⋮ menu) → **Add Resource** →
   URL `/local/ha-map-card.js`, Resource type **JavaScript module**.

### Add the card

Paste [examples/lightning-map-card.yaml](../examples/lightning-map-card.yaml) into a dashboard view
(YAML mode, or the raw card editor), setting `x`/`y` to your own home latitude/longitude. Strikes
render as points from `sensor.stormwatch_strikes`'s `geojson` attribute; the two rings are
`zone.home` `circle` overlays at `CLOSE_RADIUS` and `WATCH_RADIUS` (10 mi / 25 mi by default —
adjust the `radius` values, in meters, if you've changed those or use `UNITS=metric`). The map
window of strike history shown is controlled by `STRIKE_MAP_WINDOW_MINUTES` (default 30 minutes —
see [CONFIGURATION.md](CONFIGURATION.md#advanced)).

<!-- screenshot: lightning strike map card with close/watch rings -->

## iOS critical alerts

The `critical` priority — tornado warnings, and lightning inside `CLOSE_RADIUS` (pool closed) — is
designed to reach you even through Do Not Disturb and a muted ringer. Two things have to be true
for that to work:

1. **Enable Critical Alerts in the Home Assistant companion app.** On the iPhone: open the Home
   Assistant app → Settings (the app's own settings, not Home Assistant's) → **Notifications** →
   turn on **Critical Alerts**. iOS will prompt for a one-time permission grant the first time this
   is used — accept it.
2. **The `critical_enabled` blueprint input must be on** (it is by default — see above).

### What the payload does

The blueprint sends this data block on a `critical`-priority alert:

```yaml
data:
  push:
    sound:
      name: "default"
      critical: 1
      volume: 1.0
```

- `critical: 1` is the flag that tells iOS to play the sound even if the phone is in Do Not Disturb
  or silent mode — this is what makes a 3 a.m. tornado warning actually wake you up instead of
  arriving as a silent banner.
- `volume: 1.0` plays it at maximum volume regardless of the phone's current volume setting.
- This requires both the companion-app toggle and per-notification-service Critical Alerts
  entitlement above; without them, `critical: 1` is silently ignored and the notification behaves
  like a normal push.

## Notification discipline

This is the part that determines whether the system stays useful instead of becoming another alert
you learn to ignore. Priority maps directly to how loud the notification is allowed to be:

| Priority | Examples today | Behavior |
|---|---|---|
| `critical` | Tornado Warning; pool closed (lightning inside `CLOSE_RADIUS`) | iOS critical-alert push — bypasses Do Not Disturb and a muted ringer. Reserved for the rare case that must never be missed. |
| `high` | Severe Thunderstorm Warning | Normal push, distinct/audible sound. Does **not** bypass Do Not Disturb. |
| `normal` | Watches (e.g. Tornado Watch, Severe Thunderstorm Watch); pool all-clear | Silent — no sound, no Do Not Disturb bypass. |

The pool all-clear event is **always** `normal` priority, with no toggle to change that: **an
all-clear should never wake anyone up.** The whole discipline only works if `critical` stays rare
enough that it never gets tuned out — see [ALERT-RULES.md](ALERT-RULES.md#priorities) for how NWS
alert priorities are assigned and how to change them.
