# StormWatch

StormWatch is a single self-hosted container that turns National Weather Service severe-weather
alerts and Blitzortung community lightning data into clean Home Assistant entities — plus a real
pool all-clear timer and rainfall tracking — over MQTT auto-discovery. Free by default, no
accounts, no cloud service deciding anything for you. A tornado warning at 3 a.m. should actually
wake you up, not arrive as a silent, Do-Not-Disturb-respecting notification.

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

- **NWS watches and warnings → Home Assistant entities and critical iOS alerts.** Nearly everything
  NWS issues is reported by default; what actually notifies you is curated Home Assistant-side.
- **Lightning proximity with a real all-clear timer** — swim status, nearest-strike distance/bearing,
  strike count, and a countdown to all-clear, from the free community Blitzortung network.
- **Rainfall forecast and observed totals** for watering decisions — today/48h forecast plus
  rolling 24h/7d observed, from free NWS data (US locations only, no API key).
- **Free by default, no accounts.** No API key or signup required for anything out of the box.
- **One container, MQTT auto-discovery.** Entities appear in Home Assistant the moment the
  container starts — no `configuration.yaml` editing, no custom integration.

## Install

Quickest path — Unraid:

1. Have an MQTT broker (Mosquitto) running and connected to Home Assistant — StormWatch publishes
   to it, it doesn't provide one.
2. Unraid → **Docker → Add Container** → the **StormWatch** Community Applications template (or
   paste the template URL — see [docs/INSTALL-UNRAID.md](docs/INSTALL-UNRAID.md)).
3. Set the two required fields — `MQTT_HOST` and `NWS_CONTACT` — and your location
   (`LOCATION="Your City, ST"` or exact `LATITUDE`/`LONGITUDE`).
4. **Apply.** The **StormWatch** device and its entities appear in Home Assistant automatically
   (MQTT discovery — no `configuration.yaml` edits).
5. Import a blueprint from [examples/blueprints/](examples/blueprints/) to turn alerts into
   notifications: [severe alerts](examples/blueprints/stormwatch_severe_alerts.yaml),
   [pool alerts](examples/blueprints/stormwatch_pool_alerts.yaml),
   [tornado strobe](examples/blueprints/stormwatch_tornado_strobe.yaml).

Full step-by-step guides: **[Unraid](docs/INSTALL-UNRAID.md)** · **[Docker / Compose](docs/INSTALL-DOCKER.md)**.

## Entities

Published via MQTT discovery under a single device, `StormWatch`:

- **Alerts** — active alert count, highest-priority alert, critical-alert flag.
- **Lightning & pool** — swim status (`CLEAR`/`WATCH`/`CLOSED`), nearest-strike distance/bearing,
  15-minute strike count, all-clear countdown, and a strikes map (GeoJSON).
- **Rain & watering** — forecast (today/48h) and observed (24h/7d) rainfall totals, plus a
  weekly-aware `watering_needed` flag you can drive irrigation automations from.
- **Diagnostics** — connection status, plus per-source availability (NWS, lightning, rain).

Full entity table with every attribute: **[docs/HOME-ASSISTANT.md](docs/HOME-ASSISTANT.md)**.

## Configuration

All settings are environment variables — Unraid template fields map 1:1. Two are required:

| Variable | Example | Notes |
|---|---|---|
| `MQTT_HOST` | `192.168.1.10` | Your Mosquitto broker |
| `NWS_CONTACT` | `you@example.com` | Sent in the User-Agent header, per NWS policy |

Set your location with either `LOCATION="Your City, ST"` (geocoded once and cached) or exact
`LATITUDE`/`LONGITUDE` (e.g. `34.0234` / `-84.6155`) — coordinates are recommended for the
lightning-proximity feature, since a city geocode lands on the city center, not your address.

Full reference (every variable, alert rules, Xweather): **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.
Customizing which alerts notify you: **[docs/ALERT-RULES.md](docs/ALERT-RULES.md)**.

## Documentation

- **[docs/INSTALL-UNRAID.md](docs/INSTALL-UNRAID.md)** — Unraid install walkthrough
- **[docs/INSTALL-DOCKER.md](docs/INSTALL-DOCKER.md)** — Docker / Compose install
- **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** — every setting, explained
- **[docs/HOME-ASSISTANT.md](docs/HOME-ASSISTANT.md)** — entities, blueprints, lightning strike map
- **[docs/ALERT-RULES.md](docs/ALERT-RULES.md)** — customizing which alerts notify you
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — fixing common problems

## Updating

While StormWatch is pre-1.0, pin the minor tag — `ghcr.io/hngyhngyhobo/stormwatch:0.2` — not
`:latest` or a full patch version: 0.x minor releases can still contain breaking changes. Unraid's
Docker tab shows "update ready" on its own once a new image lands under your pinned tag; the
Community Applications **Auto Update Applications** plugin can apply those automatically if you
want it hands-off. See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

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
