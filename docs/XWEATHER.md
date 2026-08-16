# Xweather confirmation tier

**Planned — Phase 4. Not yet implemented.**

This page documents what the optional Xweather tier will do once it ships, so the config surface
(already present in [.env.example](../.env.example) and the Unraid template) makes sense ahead of
time. Nothing on this page is functional yet — leave every `XWEATHER_*` variable blank and
StormWatch is fully functional without it, per the project's key policy: a data source ships
enabled by default only if it needs no key and has no quota; anything requiring credentials is
optional, off by default, and uses your own account.

## What it will do

Vaisala's Xweather network (GLD360/NLDN — the same class of data behind commercial pool alerting
systems) requires a `client_id` and `client_secret`, so it's off by default. When you supply your
own credentials, it will act **not as a monitor, but as a confirmer** — it's only called at
decision moments, not on a polling timer:

```
Blitzortung reports a strike inside XWEATHER_CONFIRM_RADIUS (planned, Phase 2 dependency)
        │
        ├─ Quota governor: budget available?
        │        ├─ no  → skip, use Blitzortung's distance, log it
        │        └─ yes → call Xweather /lightning
        │
        └─ Use the returned distance for the swim/no-swim decision
```

Because this tier only confirms strikes that the free Blitzortung feed already reported nearby, it
depends on the Phase 2 lightning state machine (also planned, not yet implemented) existing first —
that's why it's Phase 4 in the build order rather than shipping alongside Phase 1's NWS alerts.

## The cost math

Xweather's free tier allows **15,000 accesses/month**, shared across all of Xweather's endpoints.
Lightning endpoint calls carry a **10x multiplier** against that allowance — so the free tier is
effectively about **1,500 lightning calls/month**, not 15,000.

Polling on a timer would burn that budget in a day. Confirming only when Blitzortung has already
reported a strike close enough to matter costs a few hundred calls in a bad summer month —
comfortably inside the free allowance, which is what makes this tier viable to offer without
requiring anyone to pay for it.

## Quota governor

To keep that budget from ever being exceeded, even in an unusually active storm season:

- A persistent monthly counter is kept in `/config/quota.json`, surviving container restarts and
  resetting on the 1st of each month.
- A hard ceiling, `XWEATHER_MONTHLY_CALL_LIMIT` (default `1400`), is set **deliberately under**
  the 1,500-call free allowance rather than right up against it.
- A minimum interval between calls, `XWEATHER_MIN_CALL_INTERVAL` (default `60` seconds), stops a
  single fast-moving storm from draining the whole month's budget in one event.
- **On hitting the ceiling, StormWatch silently falls back to Blitzortung's own distance estimate,
  publishes that fallback state to Home Assistant, and keeps working.** It never stops functioning
  because a quota ran out.
- Once implemented, call usage will be exposed as its own sensor (`sensor.stormwatch_xweather_calls_used`
  per DESIGN.md §8 — planned, Phase 4, not yet implemented) so you can watch your own
  consumption rather than being surprised by it.

## Configuration

All of the following are documented in full in the Xweather section of
[CONFIGURATION.md](CONFIGURATION.md): `XWEATHER_CLIENT_ID`, `XWEATHER_CLIENT_SECRET`,
`XWEATHER_CONFIRM_RADIUS`, `XWEATHER_MONTHLY_CALL_LIMIT`, `XWEATHER_MIN_CALL_INTERVAL`. Leave them
all blank to stay on the free Blitzortung-only path indefinitely — that's a fully supported,
permanent configuration, not just a fallback.

## Bring your own credentials

Xweather is a third-party commercial service. If you enable this tier, you sign up for your own
Xweather account and supply your own `client_id`/`client_secret` — StormWatch never ships or shares
a project-wide key. You're responsible for your own usage and any costs you incur beyond the free
tier, and for reviewing Xweather's own terms of service.
