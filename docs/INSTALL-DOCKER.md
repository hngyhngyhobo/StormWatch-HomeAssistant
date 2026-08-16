# Installing StormWatch with Docker Compose

For any Docker host that isn't Unraid (a NAS, a Raspberry Pi, a plain Linux box, a Docker Desktop
install). If you're on Unraid, use [INSTALL-UNRAID.md](INSTALL-UNRAID.md) instead — it uses the
Community Applications template rather than editing files by hand.

For the full environment-variable reference, see [CONFIGURATION.md](CONFIGURATION.md). For wiring
up Home Assistant after the container is running, see [HOME-ASSISTANT.md](HOME-ASSISTANT.md).

## Prerequisites

- Docker and Docker Compose (`docker compose`, the plugin form — not the standalone `docker-compose`
  v1 binary, though it will also work).
- An MQTT broker (e.g. Mosquitto) reachable from wherever the container runs, with Home Assistant's
  MQTT integration already pointed at it.
- A clone or download of this repository, so you have [docker-compose.yml](../docker-compose.yml)
  and [.env.example](../.env.example).

## Install

1. Copy the example environment file:

   ```bash
   cp .env.example .env
   ```

   (PowerShell: `Copy-Item .env.example .env`)

2. Edit `.env` and fill in the two required variables — everything else already has a working
   default in `.env.example`:

   ```
   MQTT_HOST=192.168.1.10
   NWS_CONTACT=you@example.com
   ```

   See [CONFIGURATION.md](CONFIGURATION.md#required) for what each one means.

   Location defaults to `LOCATION=Atlanta, GA` if you leave it alone — the container runs fine, it
   just watches the wrong city and logs a warning saying so. For your own area, also set either:

   ```
   LOCATION=Asheville, NC
   ```

   or, **recommended** for the lightning-proximity pool feature's precision (a city geocode lands
   on the city center, not your exact address):

   ```
   LATITUDE=34.0234
   LONGITUDE=-84.6155
   ```

   See [CONFIGURATION.md#location-resolution](CONFIGURATION.md#location-resolution) for the full
   resolution order and cache behavior.

3. **Linux hosts only** (a NAS, a Raspberry Pi, a plain Linux box — not Docker Desktop on Mac or
   Windows): create the config directory and hand it to the container's user *before* the first
   `docker compose up`:

   ```bash
   mkdir -p config && sudo chown 99:100 config
   ```

   The container runs as UID 99 (matches Unraid's `nobody` convention). A bind-mounted `./config`
   that Docker auto-creates on first run is owned by `root`, which the container can't write to —
   without this step, StormWatch crash-loops on startup trying to write `alerts.yaml`/
   `rain_history.json` to a directory it doesn't own. Docker Desktop (Mac/Windows) doesn't need this
   — its VM-backed bind mounts don't enforce host-side UID/GID ownership the same way.

4. Start the container:

   ```bash
   docker compose up -d
   ```

   [docker-compose.yml](../docker-compose.yml) pulls `ghcr.io/hngyhngyhobo/stormwatch:latest`,
   loads `.env`, publishes port `8099`, and mounts `./config` to the container's `/config` for
   persistent alert rules and quota state.

## Verify

1. **Check the logs:**

   ```bash
   docker compose logs -f stormwatch
   ```

   Expect startup lines confirming the config loaded, the NWS poller started, and the MQTT
   connection succeeded, similar to:

   ```
   INFO stormwatch: Location 'Asheville, NC' resolved to 35.5951, -82.5515 (geocoded)
   INFO stormwatch: StormWatch 1.0.0 starting
   INFO stormwatch.sources.nws: NWS poller started (contact: you@example.com, poll interval 60s)
   INFO stormwatch.sources.blitzortung: Blitzortung client started (9 cells, host blitzortung.ha.sed.pl)
   INFO stormwatch.publisher: Connected to MQTT broker 192.168.1.10:1883
   ```

   If `LOCATION`/`LATITUDE`/`LONGITUDE` are all unset, the first line is instead a `WARNING` naming
   the Atlanta, GA default — not an error, just a sign StormWatch is watching the wrong city until
   you set one of those (see [CONFIGURATION.md#location-resolution](CONFIGURATION.md#location-resolution)).

   The Blitzortung line only appears when `BLITZORTUNG_ENABLED=true` (the default); the cell count
   depends on your coordinates and `WATCH_RADIUS`.

   A container that exits and restarts in a loop almost always means a missing or malformed
   required variable — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

2. **Check the health endpoint:**

   ```bash
   curl http://localhost:8099/healthz
   ```

   Returns JSON with the container's health status:

   ```json
   {
     "status": "ok",
     "version": "1.0.0",
     "config_ok": true,
     "sources": {
       "nws": {
         "available": true
       }
     },
     "state": {
       "active_alerts": 0
     }
   }
   ```

   `status` is `ok` only when the MQTT publisher is connected, the NWS source is available (or
   `NWS_ENABLED=false`), `alerts.yaml` is valid, and — when `BLITZORTUNG_ENABLED=true` — the
   Blitzortung lightning feed is connected; otherwise `degraded` (HTTP status is still `200` either
   way). When `degraded`, check `sources.nws.available`, `sources.lightning.available` (present
   only when lightning is enabled), and `config_ok` to see which input is at fault; `config_ok`
   reflects the validity of `alerts.yaml` (see
   [TROUBLESHOOTING.md](TROUBLESHOOTING.md#checking-health-and-diagnostics)); the `sources` block
   shows per-source connectivity; `state` shows current alert counts, plus `swim_status` when
   lightning is enabled. Use this endpoint to check health from monitoring systems or dashboards.

## Upgrade

```bash
docker compose pull
docker compose up -d
```

This pulls the latest image for whatever tag is set in `docker-compose.yml` (`:latest` by default
— edit the `image:` line to pin a major version, e.g. `ghcr.io/hngyhngyhobo/stormwatch:1`, if you'd
rather control exactly when you take non-breaking updates) and recreates the container. The
`./config` volume — alert rules, quota state — is untouched. See
[CHANGELOG.md](../CHANGELOG.md) for what changed in each release.

To roll back, edit the `image:` tag in `docker-compose.yml` to a previous version and run
`docker compose up -d` again.

## Watchtower

The image already ships the standard Watchtower label
(`com.centurylinklabs.watchtower.enable=true`), so if you run Watchtower against your Docker host,
StormWatch is automatically eligible for it to manage — no extra configuration needed on the
StormWatch side. As with the Unraid CA Auto Update plugin, whether you want fully automatic updates
on a safety-adjacent container is a judgment call; pinning a major version tag (rather than
`:latest`) keeps Watchtower's automatic updates to non-breaking releases either way.

## Uninstall

```bash
docker compose down
```

This stops and removes the container but leaves the `./config` directory and your `.env` file in
place. Delete `./config` manually if you want to remove saved alert rules and quota state too. As
with the Unraid path, Home Assistant may keep showing the StormWatch device as `unavailable` until
its retained MQTT discovery messages are cleared — delete the device from Home Assistant or purge
the retained `homeassistant/.../stormwatch_*` topics on your broker.
