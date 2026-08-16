# Troubleshooting

Symptom → likely cause → fix, for the problems people actually hit setting up StormWatch.

If your problem isn't here, check the container log first (see
[Where logs live](#where-logs-live-on-unraid) below) — most of these show a specific line that
tells you exactly what's wrong.

| Symptom | Likely cause | Fix |
|---|---|---|
| No StormWatch device / entities in Home Assistant | Home Assistant's MQTT integration isn't set up or isn't connected to the same broker StormWatch publishes to | Settings → Devices & Services → confirm an MQTT integration exists and shows connected, pointed at the same host StormWatch uses for `MQTT_HOST` |
| No StormWatch device / entities in Home Assistant | `MQTT_USERNAME` / `MQTT_PASSWORD` wrong or missing for a broker that requires auth | Check the container log for an MQTT connection/auth error; correct the credentials and restart |
| No StormWatch device / entities in Home Assistant | `DISCOVERY_PREFIX` doesn't match what Home Assistant's MQTT integration expects (both default to `homeassistant`, but either can be changed) | Confirm `DISCOVERY_PREFIX` (see [CONFIGURATION.md](CONFIGURATION.md#advanced)) matches Home Assistant's configured discovery prefix |
| No StormWatch device / entities in Home Assistant | The MQTT broker itself is down or unreachable from the container | Confirm the broker is running and reachable from wherever StormWatch runs (`telnet`/`ping` the host); check the broker's own logs for a refused connection |
| Container keeps restarting | A required environment variable is missing or empty (`MQTT_HOST`, `NWS_CONTACT`) — StormWatch fails fast at startup rather than running half-configured | Check the log for a plain-English startup error naming the missing variable, e.g. a line mentioning `MQTT_HOST` or `NWS_CONTACT`; fill it in via the Unraid template or `.env` and restart. See [CONFIGURATION.md](CONFIGURATION.md#required). |
| Container keeps restarting | `LOCATION` is set to a custom city string, geocoding failed (no network, or no matching city), and there's no cache yet at `/config/location.json` | Check the log for a startup error naming the `LOCATION` string and telling you to set `LATITUDE`/`LONGITUDE` instead; either fix `LOCATION` or switch to exact coordinates. See [CONFIGURATION.md#location-resolution](CONFIGURATION.md#location-resolution). |
| Wrong city in alerts/lightning-proximity, or a `WARNING ... Using default location Atlanta, GA` line in the logs | `LOCATION`, `LATITUDE`, and `LONGITUDE` are all unset — StormWatch defaults to Atlanta, GA rather than failing to start | Set `LOCATION="Your City, ST"` or exact `LATITUDE`/`LONGITUDE` (recommended for lightning-proximity precision) and restart. See [CONFIGURATION.md#location-resolution](CONFIGURATION.md#location-resolution). |
| Container restarts immediately on Linux/Docker Compose | `/config` bind mount not writable by UID 99 (the user the container runs as) — Docker auto-creates `./config` owned by `root` on first run if it doesn't already exist | `chown 99:100 ./config` on the host, then restart the container — see [INSTALL-DOCKER.md](INSTALL-DOCKER.md#install) (not needed on Docker Desktop for Mac/Windows) |
| Alerts aren't firing for an event you expect | The NWS event name doesn't match anything in `ALERTS_CRITICAL` / `ALERTS_HIGH` / `ALERTS_NORMAL` or in `/config/alerts.yaml` | Compare the exact NWS event name (visible in the raw alert, or in a diagnostic sensor once published) against your rules — see [ALERT-RULES.md](ALERT-RULES.md#match-fields). Names must match exactly unless you use `event_regex`. |
| Alerts aren't firing at all | `min_severity` in `alerts.yaml` (default `Minor`) is set to a level that filters out alerts below that severity | With the `Minor` default, almost nothing is severity-filtered out of the box; raise `defaults.min_severity` in `/config/alerts.yaml` if you want stricter filtering — see [ALERT-RULES.md](ALERT-RULES.md) |
| Alerts aren't firing at all | `NWS_ENABLED` is set to `false` | Set `NWS_ENABLED=true` (the default) unless you deliberately disabled it, e.g. for a non-US location — see [CONFIGURATION.md](CONFIGURATION.md#advanced) |
| Pool never shows `WATCH`/`CLOSED` even though storms are visibly nearby on radar | Blitzortung is a **volunteer sensor network**, not a commercial radar feed — sensor density varies a lot by region (dense in parts of Europe and the US, sparse elsewhere), so it can legitimately miss strikes a radar app would show. This is a real limitation of the free data source, not a StormWatch bug (see DESIGN.md §4.1 and the "Read this first" warning at the top of the README: lightning detection networks miss strikes). | Confirm `binary_sensor.stormwatch_lightning_available` is `on` (feed connected) and `BLITZORTUNG_ENABLED=true`; if both check out, the feed simply isn't seeing strikes in your area yet. There is no config fix for sensor coverage — treat StormWatch as decision support, not the sole source of truth, and keep the NWS-issued severe thunderstorm/tornado alerts (which don't depend on Blitzortung) as the authoritative backstop. |
| Entities show `unavailable` in Home Assistant | **By design** — see [Unavailable entities](#unavailable-entities) below. This is not a bug. | Fix the underlying data source (MQTT connection, NWS reachability); the entity recovers on its own once the source is back |
| Rain sensors show `None` / `binary_sensor.stormwatch_rain_available` is `off` | Station discovery in progress — StormWatch has to find a nearby observation station that actually reports precipitation before `rain_last_24h`/`rain_last_7d` have anything to show; this can take up to one full `RAIN_OBS_POLL_SECONDS` cycle (15 minutes by default) after startup. Also: rainfall is US-only (NWS gridpoints don't cover other countries), and `RAIN_ENABLED` might be `false`. | Wait out one observation poll cycle after startup before troubleshooting further. Confirm `RAIN_ENABLED=true` and the location is in the US. Check `/healthz`'s `sources.rain.available` (see [Checking health and diagnostics](#checking-health-and-diagnostics) below) and the container log for a `stormwatch.sources.rain` warning naming the underlying HTTP failure. |

## Checking health and diagnostics

The `/healthz` endpoint on port `8099` returns JSON showing the container's current status:

```bash
curl http://<your-host>:8099/healthz
```

Returns (shown here for the default-on config — `NWS_ENABLED=true`, `BLITZORTUNG_ENABLED=true`, and
`RAIN_ENABLED=true`, all defaults):

```json
{
  "status": "ok",
  "version": "1.0.0",
  "config_ok": true,
  "sources": {
    "nws": {
      "available": true
    },
    "lightning": {
      "available": true
    },
    "rain": {
      "available": true
    }
  },
  "state": {
    "active_alerts": 0,
    "swim_status": "CLEAR",
    "rain_last_24h": 0.0
  }
}
```

Use this to monitor StormWatch from external systems (Uptime Kuma, Nagios, etc.). `status` is `ok`
only when every required *safety-relevant* input checks out: the MQTT publisher is connected, the
NWS source is available (or `NWS_ENABLED=false`, in which case its availability doesn't matter),
`alerts.yaml` has no validation error, and — when `BLITZORTUNG_ENABLED=true` — the Blitzortung
lightning feed is connected; otherwise it's `degraded` (HTTP status is still `200` either way —
`degraded` is a body field, not an error code). When you see `degraded`, check
`sources.nws.available`, `sources.lightning.available` (present only when lightning is enabled),
and `config_ok` to see which input is at fault — if all look fine, the container likely hasn't
connected to the MQTT broker yet. `config_ok` reflects whether `alerts.yaml` has a validation error
(when false, check `binary_sensor.stormwatch_config_problem` in Home Assistant for the error detail
in attributes). With lightning enabled, `state` also gains a `swim_status` field mirroring
`sensor.stormwatch_swim_status`.

**Rain is deliberately excluded from the `status` calculation.** `sources.rain.available` being
`false` never flips the overall `status` to `degraded` the way NWS/lightning unavailability does —
rainfall is a watering-decision feature, not a "get out of the water" one. Check
`sources.rain.available` (and `state.rain_last_24h`, the trailing-24h observed total in mm)
directly if you specifically care about rain freshness; don't rely on the overall `status` field
for it.

**`sources.nws` disappears entirely when `NWS_ENABLED=false`**, and **`sources.rain` disappears
entirely when `RAIN_ENABLED=false`** — for a lightning-only deployment (or any combination with a
feature turned off), the corresponding key is simply absent (not `{"available": false}`), the same
way `sources.lightning` is absent when `BLITZORTUNG_ENABLED=false`. `status` can still be `ok` in
that case; a disabled source's availability is never checked.

## Rain forecast timing

`sensor.stormwatch_rain_forecast_today` and `sensor.stormwatch_rain_forecast_48h` use two
different, deliberately simple windows in this version — worth knowing before the numbers look
"wrong":

- **`rain_forecast_today` uses the UTC calendar day, not your local day.** For a US Eastern user,
  the UTC day rolls over at 8 p.m. Eastern (7 p.m. during Eastern Daylight Time) — so
  `rain_forecast_today` resets to tomorrow's forecast in the early evening, local time, not at
  midnight local time. West Coast users see an even earlier rollover (4-5 p.m. Pacific). This is a
  known v1 simplification, not a bug — a local-timezone "today" window is on the improvement list
  for a future release.
- **`rain_forecast_48h` is rolling from now**, not tied to any calendar boundary — it's always
  "the next 48 hours starting from whenever you last checked," so it changes continuously rather
  than resetting once a day.

Neither of these affects `rain_last_24h` / `rain_last_7d` (observed rainfall), which are plain
rolling windows ending at the current moment regardless of calendar or timezone.

## Unavailable entities

If a StormWatch entity shows `unavailable` in Home Assistant, that's **by design, not a bug.**

StormWatch's availability doctrine (DESIGN.md §3.2): loss of a data source sets an
availability flag Home Assistant can see. It never silently reports a "safe" state — like `CLEAR`
— just because it lost its feed. **Unknown is not the same as safe.** If the container crashes,
loses its MQTT connection, or a data source stops responding, the honest thing to show is
`unavailable`, not to keep displaying the last-known-good value as if it were still current.

`binary_sensor.stormwatch_connected` uses MQTT's Last Will and Testament specifically so Home
Assistant learns about a lost connection immediately and honestly, rather than the connection
silently going stale. If you're seeing `unavailable` and expected a live value, check what's
actually down — the container, the network path to it, or the broker — rather than treating the
`unavailable` state itself as the problem to fix.

## Where logs live on Unraid

StormWatch logs to stdout only — there's no separate application log file written under `/config`.
Docker's own logging driver captures stdout, and Unraid exposes it in the webUI:

1. Docker tab → click the StormWatch container icon.
2. Click **Logs**.

This shows the same output as running `docker logs stormwatch` (or `docker logs -f stormwatch` to
follow it live) from a terminal on the Unraid host, e.g. over SSH. Increase verbosity with the
`LOG_LEVEL` variable (default `INFO`; set to `DEBUG` for more detail while diagnosing something) —
see [CONFIGURATION.md](CONFIGURATION.md#common).

On a plain Docker Compose install, the equivalent is
`docker compose logs -f stormwatch` — see [INSTALL-DOCKER.md](INSTALL-DOCKER.md#verify).
