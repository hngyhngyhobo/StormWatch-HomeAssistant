# Installing StormWatch on Unraid

StormWatch ships as a single Docker container, installable from a Community Applications-style
template even before it's listed in Community Applications (CA) itself. This page walks through
that install, verifying it worked, keeping it updated, rolling back, and uninstalling.

For the full environment-variable reference, see [CONFIGURATION.md](CONFIGURATION.md). For wiring
up Home Assistant after the container is running, see [HOME-ASSISTANT.md](HOME-ASSISTANT.md).

## Prerequisites

- **An MQTT broker, running and reachable from Unraid.** Most Unraid/Home Assistant users already
  run the **Mosquitto** app (search "Mosquitto" or "MQTT" in Community Applications if you don't
  have one). StormWatch publishes to this broker; it does not provide one itself.
- **Home Assistant's MQTT integration connected to that same broker.** Settings → Devices &
  Services → confirm an **MQTT** integration is configured and shows as connected. StormWatch's
  entities arrive through MQTT discovery on this integration — if it isn't set up first, the
  container will run fine but nothing will show up in Home Assistant.

## Install

StormWatch isn't yet published to the Community Applications feed, so install it by pointing
Unraid at the template URL directly. Once it's on CA, this file will be updated with a "search
Apps for StormWatch" shortcut — this is the pre-CA path.

1. In the Unraid webUI, go to the **Docker** tab.
2. Click **Add Container** at the bottom of the page.
3. At the top of the Add Container form, find the **Template** field. Paste in the template URL:

   ```
   https://raw.githubusercontent.com/hngyhngyhobo/WeatherAlert-HomeAssistant/main/unraid/stormwatch.xml
   ```

   Unraid fetches the XML and pre-fills the rest of the form — repository, icon, category, ports,
   and every configuration field below.

   <!-- screenshot: Add Container form with the Template URL field filled in -->

4. Confirm the **Repository** field reads `ghcr.io/hngyhngyhobo/stormwatch:latest` (pin a version
   tag instead of `:latest` if you prefer — see [Updates](#updates) below).
5. Fill in the two required fields (walkthrough below). Optionally also set **Location** (or
   **Latitude**/**Longitude**) for your own area — see the note below the table.
6. Leave everything else at its default unless you know you need to change it — the container is
   fully functional out of the box once the two required fields are set.
7. Click **Apply**.

   <!-- screenshot: completed Add Container form before clicking Apply -->

### The two required fields

These are the only fields you must fill in. Help text below is copied verbatim from the template
([unraid/stormwatch.xml](../unraid/stormwatch.xml)).

| Field (template label) | Env var | Help text shown in Unraid |
|---|---|---|
| **MQTT Host** | `MQTT_HOST` | "Your Mosquitto broker. Required." |
| **NWS Contact** | `NWS_CONTACT` | "Sent in the User-Agent header, per NWS policy. Required." |

Notes:

- **MQTT Host** — the LAN IP or hostname of the Mosquitto broker from the prerequisites above, e.g.
  `192.168.1.10`. Not a URL — just the host.
- **NWS Contact** — an email address or URL identifying you to the National Weather Service. NWS
  requires every API client to send a `User-Agent` with contact info and will reject requests
  without one; this is not used for anything else and is never shared with StormWatch's maintainer.

### Setting your location (optional, but recommended)

If you leave **Location** and **Latitude**/**Longitude** blank, StormWatch defaults to
`Atlanta, GA` — the container starts and works fine, it's just watching the wrong city, and logs a
startup warning saying so. To point it at your own area, fill in one of:

| Field (template label) | Env var | Notes |
|---|---|---|
| **Location** | `LOCATION` | A city name, e.g. `Asheville, NC`. Geocoded once (free, no API key) and cached to `/config/location.json`. |
| **Latitude** / **Longitude** | `LATITUDE` / `LONGITUDE` | Decimal degrees, e.g. `34.0234` / `-84.6155`. **Recommended** over Location for the lightning-proximity pool feature — a city geocode lands on the city center, which can be a few miles off your actual address. If you don't know yours, any map app that shows coordinates on long-press works. |

Setting both **Latitude** and **Longitude** always wins over **Location**. See
[CONFIGURATION.md#location-resolution](CONFIGURATION.md#location-resolution) for the full
resolution order and cache behavior.

Every other field (MQTT port/credentials, units, alert thresholds, advanced settings, the optional
Xweather section) has a working default — full reference in
[CONFIGURATION.md](CONFIGURATION.md).

## Verify

After clicking Apply, Unraid pulls the image and starts the container.

1. **Check the container log.** Click the StormWatch icon on the Docker tab → **Logs**. Expect
   lines confirming startup, similar to:

   ```
   INFO stormwatch: Location 'Asheville, NC' resolved to 35.5951, -82.5515 (geocoded)
   INFO stormwatch: StormWatch 1.0.0 starting
   INFO stormwatch.sources.nws: NWS poller started (contact: you@example.com, poll interval 60s)
   INFO stormwatch.sources.blitzortung: Blitzortung client started (9 cells, host blitzortung.ha.sed.pl)
   INFO stormwatch.publisher: Connected to MQTT broker 192.168.1.10:1883
   ```

   If you left **Location**/**Latitude**/**Longitude** all blank, the first line is instead a
   `WARNING` naming the Atlanta, GA default — see
   [Setting your location](#setting-your-location-optional-but-recommended) above. That's not an
   error; it just means StormWatch is watching the wrong city until you set one of those fields.

   The Blitzortung line only appears when `BLITZORTUNG_ENABLED=true` (the default); the cell count
   depends on your coordinates and `WATCH_RADIUS`.

   If instead you see the container exit and restart repeatedly, see
   [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — it's almost always a missing or malformed required
   field.

2. **Check Home Assistant.** Go to Settings → Devices & Services → MQTT → Devices, and look for a
   device named **StormWatch**. It should appear within a few seconds of the container connecting,
   with no restart or manual configuration needed. See
   [HOME-ASSISTANT.md](HOME-ASSISTANT.md) for the full entity list and what to set up next.

   <!-- screenshot: StormWatch device page in Home Assistant showing its entities -->

The template also exposes a WebUI button pointed at the container's `:8099/healthz` endpoint — see
[INSTALL-DOCKER.md](INSTALL-DOCKER.md#verify) for the JSON shape and what each field means.

## Updates

- **Update-ready flow.** Unraid's Docker tab checks image digests for containers with a fixed tag
  and shows an **"update ready"** badge automatically once a new image is published under that tag
  — no manual digest checking. Click the badge (or the container icon → **Update**) to pull and
  recreate the container with the new image. Your `/config` volume and settings are untouched.
- **Pin the major tag.** Images are published as `:latest`, `:1`, `:1.2`, `:1.2.3`. Set the
  **Repository** field to `ghcr.io/hngyhngyhobo/stormwatch:1` (or whatever the current major is)
  rather than `:latest` — you get bug fixes and non-breaking improvements without an unannounced
  major version jump. See [CHANGELOG.md](../CHANGELOG.md) for what changed in each release.
- **CA Auto Update plugin (opt-in).** If you run the **CA Auto Update Applications** plugin, you can
  add StormWatch to its schedule so updates apply automatically instead of waiting for you to click
  "update ready." This is optional — given StormWatch is safety-adjacent, some users prefer to
  review the changelog before every update rather than auto-apply. Either is a reasonable choice;
  pinning the major tag keeps auto-updates to non-breaking changes either way.

## Rollback

If an update causes a problem, pin back to the previous version tag:

1. Docker tab → click the StormWatch icon → **Edit**.
2. Change the **Repository** field from your current tag to the last known-good one, e.g.
   `ghcr.io/hngyhngyhobo/stormwatch:1.2.2`.
3. Click **Apply**. Unraid pulls that specific tag and recreates the container on it.
4. Once you're ready to move forward again, edit the Repository field back to your normal pinned
   tag (or `:latest`) and Apply again.

Your `/config` volume (alert rules, quota state) is unaffected by switching tags in either
direction, as long as you're not jumping across a major version documented as requiring a config
migration in [CHANGELOG.md](../CHANGELOG.md).

## Uninstall

1. Docker tab → click the StormWatch icon → **Remove**. Confirm removal. This stops and deletes the
   container; it does not touch your MQTT broker or Home Assistant.
2. Optional — delete the appdata folder (`/mnt/user/appdata/stormwatch`) via the Unraid file
   manager if you want to remove StormWatch's saved alert rules and quota state along with it.
3. Optional — Home Assistant retains MQTT discovery entities as long as their retained discovery
   messages exist on the broker, so the StormWatch device may stay listed (showing `unavailable`)
   after the container is gone. To clean that up immediately: in Home Assistant, go to the
   StormWatch device page and delete it, or purge the retained `homeassistant/.../stormwatch_*`
   topics from your broker with a tool like MQTT Explorer.
