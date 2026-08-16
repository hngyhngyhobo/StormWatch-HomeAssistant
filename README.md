# StormWatch

StormWatch is a single self-hosted container that turns National Weather Service severe-weather
alerts into clean Home Assistant entities and critical push notifications, so a tornado warning at
3 a.m. actually wakes you up instead of arriving as a silent, Do-Not-Disturb-respecting
notification. It's also built around the question every pool owner asks all summer: "do we need to
get out of the water, and can we get back in?" — lightning proximity with a real all-clear timer,
via the free community Blitzortung lightning network. Free by default, no accounts, no cloud
service deciding anything for you.

<!-- screenshot: HA dashboard card — added at go-live -->

## ⚠ Read this first

> **StormWatch is decision support, not a life-safety system.**
>
> Lightning detection networks miss strikes. Networks go down. Containers crash. Your internet drops. Do not rely on this system to keep anyone safe.
>
> **NWS warnings are authoritative.** When StormWatch and the National Weather Service disagree, the NWS is right. Keep a NOAA Weather Radio or your phone's Wireless Emergency Alerts enabled.
>
> **When thunder roars, go indoors** — regardless of what this dashboard says.

## What it does

- **NWS watches and warnings become Home Assistant entities and critical iOS alerts.** Floods,
  winter storms, freeze/frost, hurricanes, tornadoes — essentially everything NWS issues is
  reported by default; priority (and what actually becomes a notification) is curated Home
  Assistant-side, so the alert that matters actually reaches you.
- **Lightning proximity with a real all-clear timer** for the "can we get back in the pool"
  question — swim status, nearest-strike distance/bearing, strike count, and a countdown to
  all-clear, driven by the free community Blitzortung lightning network. The timer resets on every
  qualifying strike and the all-clear is its own event, never a loud one.
- **Rainfall forecast and observed accumulation for watering decisions** — today's and next-48h
  forecast rainfall, plus rolling 24h/7d observed totals with an hourly breakdown, straight from
  free NWS gridpoint/station data (US locations only, no API key).
- **Free by default, no accounts.** No API key or signup is required for anything StormWatch does
  out of the box.
- **One container, MQTT auto-discovery.** Entities appear in Home Assistant the moment the
  container starts — no `configuration.yaml` editing, no custom integration.

## Quickstart (Unraid, ~5 minutes)

1. Make sure Mosquitto (or any MQTT broker) is already running and connected to Home Assistant —
   StormWatch publishes to it but doesn't provide one itself.
2. In Unraid, go to **Docker → Add Container** and add StormWatch from the Community Applications
   template (or paste the template URL — see [docs/INSTALL-UNRAID.md](docs/INSTALL-UNRAID.md)).
3. Fill in the two required fields: `MQTT_HOST`, `NWS_CONTACT`. Location defaults to Atlanta, GA
   — set `LOCATION="Your City, ST"` or exact `LATITUDE`/`LONGITUDE` coordinates for your own area.
4. Start the container. A **StormWatch** device with its entities appears in Home Assistant
   automatically — no restart or manual configuration needed.
5. Import the shipped
   [severe alerts blueprint](examples/blueprints/stormwatch_severe_alerts.yaml) to route alerts to
   notifications, including the critical iOS alert payload, and the
   [pool alerts blueprint](examples/blueprints/stormwatch_pool_alerts.yaml) to get notified when
   the pool closes for lightning and when it's safe to get back in. Want a Tornado Warning to
   physically wake you? Import the
   [tornado strobe blueprint](examples/blueprints/stormwatch_tornado_strobe.yaml) to flash a
   bedside lamp — no device firmware changes needed.

Full walkthrough, screenshots, and field-by-field explanations:
[docs/INSTALL-UNRAID.md](docs/INSTALL-UNRAID.md).

## Entities

Published via MQTT discovery under a single device, `StormWatch`.

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
| `sensor.stormwatch_strikes` | count | Count of recent lightning strikes (last `STRIKE_MAP_WINDOW_MINUTES`); a GeoJSON `FeatureCollection` of strike points (distance/bearing/age/range per point) is in the `geojson` attribute, for rendering on a map card — see [docs/HOME-ASSISTANT.md](docs/HOME-ASSISTANT.md#lightning-strike-map) |

The lightning/pool entities (rows 7-13, plus `sensor.stormwatch_strikes`) only appear when `BLITZORTUNG_ENABLED` is true (the
default), and the rainfall entities (last five rows) only appear when `RAIN_ENABLED` is true (also
the default, US locations only) — see [docs/CONFIGURATION.md](docs/CONFIGURATION.md#advanced).

## Configuration

All settings are environment variables — Unraid template fields map 1:1. These two are required;
everything else has a working default.

| Variable | Example | Notes |
|---|---|---|
| `MQTT_HOST` | `192.168.1.10` | Your Mosquitto broker |
| `NWS_CONTACT` | `you@example.com` | Sent in the User-Agent header, per NWS policy |

Location defaults to `LOCATION=Atlanta, GA` if you don't set anything — the container starts and
runs fine, it just logs a warning and watches the wrong city. For your own area, set either:

| Variable | Example | Notes |
|---|---|---|
| `LOCATION` | `Asheville, NC` | City name, geocoded once (via the free Open-Meteo API) and cached to `/config/location.json` |
| `LATITUDE` / `LONGITUDE` | `34.0234` / `-84.6155` | Decimal degrees — **recommended over `LOCATION`** for the lightning-proximity feature, since a city geocode lands on the city center, not your exact address |

Full reference for every variable (location resolution details, polling intervals, alert rule env
vars, optional Xweather tier, advanced settings): [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Updating

Docs recommend pinning the major version tag, e.g. `ghcr.io/hngyhngyhobo/stormwatch:1`, rather than
`:latest` or a full version — you get bug fixes and non-breaking improvements automatically without
surprise major upgrades. On Unraid, the Docker tab shows "update ready" on its own once a new image
is published under your pinned tag; no manual digest checking required.

See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

## Attribution & data sources

StormWatch is built on free, no-account data sources:

- **[Blitzortung.org](https://www.blitzortung.org/)** — community lightning detection network,
  used for the lightning-proximity feature. Blitzortung is explicit that its data must not
  be used to protect people or equipment; StormWatch treats it accordingly (see "Read this first"
  above).
- **[`mrk-its/homeassistant-blitzortung`](https://github.com/mrk-its/homeassistant-blitzortung)** —
  credit for the MQTT topic scheme StormWatch's lightning feature uses.
- **NWS / NOAA** ([api.weather.gov](https://www.weather.gov/documentation/services-web-api)) — the
  authoritative source for all watches, warnings, and advisories StormWatch publishes today.
- **[Vaisala Xweather](https://www.xweather.com/)** — optional, off by default. If you supply your
  own Xweather credentials, StormWatch can use it as a lightning-distance confirmation tier. It's a
  third-party service subject to Xweather's own terms; you bring your own credentials and are
  responsible for your own usage and any costs.

## License

StormWatch is licensed under the **GNU General Public License v3.0** — see
[LICENSE](LICENSE) for the full text.

This software is provided **AS IS, WITHOUT WARRANTY OF ANY KIND**, express or implied. Given the
safety context described above, read that as literally as it's written: StormWatch comes with no
guarantee it will detect, report, or notify you of anything, ever.
