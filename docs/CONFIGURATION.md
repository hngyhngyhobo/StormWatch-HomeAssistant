# Configuration reference

Every StormWatch setting is an environment variable — Unraid template fields map to them 1:1, and
[.env.example](../.env.example) lists every one with the same defaults documented here. This page
is the full reference; if you just want the two you must set to get running, see
[INSTALL-UNRAID.md](INSTALL-UNRAID.md) or [INSTALL-DOCKER.md](INSTALL-DOCKER.md).

## Which layer do I use?

StormWatch has two layers of configuration, and most people only ever need the first:

- **Environment variables (this page)** — set location, MQTT connection, units, the lightning
  proximity thresholds for the pool/swim state machine, and a simple comma-separated list of which
  NWS event names count as `critical` / `high` / `normal`. This covers the large majority of
  setups: type event names into a template field, restart, done. No file editing.
- **`/config/alerts.yaml`** — a mounted YAML file for full control over alert matching: regex event
  matching, matching on severity/urgency/certainty/response, per-rule quiet hours, enabling or
  disabling individual rules. It's auto-generated with sensible defaults on first run and
  hot-reloads on change — no restart needed. See [ALERT-RULES.md](ALERT-RULES.md).

If the env-var alert lists (`ALERTS_CRITICAL` / `ALERTS_HIGH` / `ALERTS_NORMAL`, below) are enough
for you, you never need to touch `alerts.yaml` at all — it exists underneath either way and the env
vars are just a friendlier way of writing simple rules into it.

## Required

Must be set for the container to start. Every other variable on this page has a working default.

| Variable | Example | Notes |
|---|---|---|
| `MQTT_HOST` | `192.168.1.10` | Your Mosquitto broker |
| `NWS_CONTACT` | `you@example.com` | Sent in the User-Agent header, per NWS policy. Required whenever `NWS_ENABLED` or `RAIN_ENABLED` is true (both default true) — both features call api.weather.gov. |

## Recommended

Not required — the container runs fine without either of these, defaulting to `LOCATION=Atlanta,
GA` (see [Location resolution](#location-resolution) below) — but setting one of them is how you
point StormWatch at your own area.

| Variable | Example | Notes |
|---|---|---|
| `LATITUDE` | `34.0234` | Decimal degrees. **Recommended over `LOCATION`** for lightning-proximity precision — see the note below. |
| `LONGITUDE` | `-84.6155` | Decimal degrees. |

**Exact coordinates vs. a city name:** `LATITUDE`/`LONGITUDE` pin an exact point; `LOCATION`
geocodes a city name to that city's *center*, which can be several miles from your actual address.
For NWS alerts (county/zone-based) that rarely matters. For the lightning-proximity pool feature
(`CLOSE_RADIUS`/`WATCH_RADIUS`, a handful of miles), a city-center offset can matter — set exact
`LATITUDE`/`LONGITUDE` if precise swim-status timing matters to you.

## Location resolution

StormWatch resolves where you are in this order, stopping at the first match:

1. **Explicit coordinates** — `LATITUDE` and `LONGITUDE` both set. Used as-is; `LOCATION` is
   ignored entirely once both are present.
2. **The default** — `LOCATION` unset or left as `Atlanta, GA` (the built-in default, case/
   whitespace-insensitive). Resolves instantly to Atlanta's coordinates with **no network call** —
   and logs a startup warning so a genuinely unconfigured deployment doesn't go unnoticed:

   ```
   WARNING stormwatch: Using default location Atlanta, GA — set LOCATION or LATITUDE/LONGITUDE for your area
   ```

3. **Cached geocode** — `LOCATION` set to something else, and `/config/location.json` already has
   coordinates cached for that *exact* location string. No network call.
4. **Fresh geocode** — `LOCATION` set to something else with no matching cache entry: StormWatch
   geocodes it via the free, no-key [Open-Meteo geocoding API](https://open-meteo.com/en/docs/geocoding-api)
   and writes the result to `/config/location.json` for next time. Changing `LOCATION` to a
   different city string invalidates the old cache entry and triggers a fresh geocode.
5. **Failure** — geocoding fails (network error, or no matching city) and there's no cache to fall
   back on: the container logs a plain-English error and exits, telling you to set `LATITUDE`/
   `LONGITUDE` instead.

A successful geocode (fresh or cached) logs an info line naming what it resolved to, e.g.:

```
INFO stormwatch: Location 'Asheville, NC' resolved to 35.5951, -82.5515 (geocoded)
```

## Common

The settings most people will actually touch, beyond the two required ones.

| Variable | Default | Notes |
|---|---|---|
| `LOCATION` | `Atlanta, GA` | City name, geocoded once and cached — see [Location resolution](#location-resolution) above. Ignored once `LATITUDE`/`LONGITUDE` are both set. |
| `MQTT_PORT` | `1883` | |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | — | If your broker requires auth |
| `UNITS` | `imperial` | `imperial` or `metric` |
| `CLOSE_RADIUS` | `10` | In `UNITS`. A strike inside this radius closes the pool (`swim_status` → `CLOSED`) |
| `WATCH_RADIUS` | `25` | In `UNITS`. A strike inside this radius (but outside `CLOSE_RADIUS`) sets `swim_status` → `WATCH` |
| `ALL_CLEAR_MINUTES` | `30` | Minutes of no qualifying lightning activity before signaling all-clear; resets on every qualifying strike |
| `ALERTS_CRITICAL` / `ALERTS_HIGH` / `ALERTS_NORMAL` | see [ALERT-RULES.md](ALERT-RULES.md) | Comma-separated NWS event names |
| `QUIET_HOURS` | `22:00-07:00` | Applies only to rules with `quiet_hours: true` |
| `LOG_LEVEL` | `INFO` | |

`CLOSE_RADIUS`, `WATCH_RADIUS`, and `ALL_CLEAR_MINUTES` drive the lightning decision state machine
(DESIGN.md §6) that publishes `sensor.stormwatch_swim_status` and the rest of the pool/lightning
entity set — see [HOME-ASSISTANT.md](HOME-ASSISTANT.md#entities).

## Optional — Xweather (planned — Phase 4, not yet implemented)

Leave every field in this section blank to stay fully free — blank means the feature is off, and
the container is fully functional without it. See [XWEATHER.md](XWEATHER.md) for what this tier
will do and the cost math behind the defaults below.

| Variable | Default | Notes |
|---|---|---|
| `XWEATHER_CLIENT_ID` | *(empty)* | **Blank = feature off.** Your own key |
| `XWEATHER_CLIENT_SECRET` | *(empty)* | |
| `XWEATHER_CONFIRM_RADIUS` | `25` | Only confirm strikes already this close |
| `XWEATHER_MONTHLY_CALL_LIMIT` | `1400` | Under the free allowance on purpose |
| `XWEATHER_MIN_CALL_INTERVAL` | `60` | Seconds |

## Advanced

| Variable | Default | Notes |
|---|---|---|
| `BLITZORTUNG_MQTT_HOST` | `blitzortung.ha.sed.pl` | Community lightning relay (see [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) for its coverage/reliability limitations). Rarely changed. |
| `BLITZORTUNG_ENABLED` | `true` | Set `false` to disable the lightning/pool feature entirely — no client connection, no lightning entities, `/healthz` omits the `lightning` source |
| `NWS_ENABLED` | `true` | Non-US users turn this off |
| `NWS_POLL_SECONDS` | `60` | Floor of 30 enforced |
| `NWS_API_BASE` | `https://api.weather.gov` | Testing/override; rarely changed. NWS API base URL. |
| `CONFIG_DIR` | `/config` | Path to persistent storage for alert rules, state, and the cached geocoded location (`location.json`) |
| `RAIN_ENABLED` | `true` | Set `false` to disable the rainfall feature entirely — no entities, no polling, `/healthz` omits the `rain` source. US locations only (NWS data); non-US users should leave it off. |
| `RAIN_FORECAST_POLL_SECONDS` | `3600` | Seconds between rainfall forecast updates (`sensor.stormwatch_rain_forecast_today` / `_rain_forecast_48h`) |
| `RAIN_OBS_POLL_SECONDS` | `900` | Seconds between observed-rainfall updates (`sensor.stormwatch_rain_last_24h` / `_rain_last_7d`) |
| `DISCOVERY_PREFIX` | `homeassistant` | |
| `DEVICE_NAME` | `StormWatch` | Change if running multiple instances |
| `STRIKE_MAP_WINDOW_MINUTES` | `30` | Minutes of strike history retained for `sensor.stormwatch_strikes`'s `geojson` attribute (the lightning strike map — see [HOME-ASSISTANT.md](HOME-ASSISTANT.md#lightning-strike-map)). Floored at 1. |

**Multi-location:** run a second container with a different `DEVICE_NAME` and coordinates rather
than trying to make one instance cover two places — cleaner, and Unraid makes running a second
container trivial. Useful for a lake house or a kid's school.
