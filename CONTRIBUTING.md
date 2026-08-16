# Contributing to StormWatch

Thanks for taking a look. StormWatch is a small, focused project — a boring stack on purpose, so
it stays easy to contribute to.

## Dev setup

```
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
ruff check .
pytest
```

(On macOS/Linux, create the venv with `python3.12 -m venv .venv` and activate it with
`source .venv/bin/activate` — everything after that is the same.)

`pip install -e ".[dev]"` installs StormWatch itself in editable mode plus the dev dependencies
(`ruff`, `pytest`). `ruff check .` lints; `pytest` runs the test suite. Both must pass before you
open a PR.

## Branch & PR flow

- Work on a feature branch, not `main`. No direct pushes to `main` — it's protected.
- Open a PR against `main`.
- CI must be green (lint, unit tests, integration tests, Docker build) before merge.
- Keep PRs focused — one logical change per PR makes review and the resulting CHANGELOG entry
  cleaner.

## Commit messages: Conventional Commits

StormWatch uses [Conventional Commits](https://www.conventionalcommits.org/), and
[release-please](https://github.com/googleapis/release-please) derives the version bump and
`CHANGELOG.md` entry directly from your commit messages. Commit discipline *is* release
discipline.

| Prefix | Effect |
|---|---|
| `feat:` | Minor version bump, appears in CHANGELOG under Features |
| `fix:` | Patch version bump, appears in CHANGELOG under Bug Fixes |
| `feat!:` / `fix!:` / any commit with a `BREAKING CHANGE:` footer | Major version bump |
| `docs:` | No release, no version bump |
| `chore:` | No release, no version bump |

**release-please owns `CHANGELOG.md` and the version number — never hand-edit either.** If a
CHANGELOG entry is wrong, fix it by correcting the commit message / PR title that generated it, not
by editing the file.

## The README rule

**Every PR that changes a feature or user-facing functionality updates `README.md` — and any
affected page under `docs/` — in the same PR.** Reviewers will block PRs that add or change
behavior without a matching doc update. Minor internal fixes (a typo, a bug fix with no
user-visible behavior change) don't need a README touch; those flow into `CHANGELOG.md`
automatically via release-please.

If you're not sure whether your change counts, ask in the PR description — better to over-include
than to ship a feature nobody can discover.

## Testing levels

StormWatch tests at three levels:

- **Unit** (`pytest`, default) — no network access. Anything that talks to NWS, MQTT, or Xweather
  is mocked or replayed from fixtures in `tests/fixtures/`.
- **Integration** (`pytest -m integration`) — runs against a real Mosquitto broker (CI provides one
  as a service container). Exercises actual MQTT discovery and publish/subscribe behavior.
- **End-to-end acceptance** — before any release, a manual pass against real Home Assistant and a
  real or synthetic NWS alert, confirming the full chain from data source to a notification on a
  phone. Not automated; a release checklist item.

## Entity IDs are a public API

Home Assistant entity IDs (`sensor.stormwatch_*`, `binary_sensor.stormwatch_*`) are load-bearing —
users build automations and dashboards on top of them. Never rename or remove one without a
deprecation cycle: publish both the old and new entity for at least one major version, and document
the change in `CHANGELOG.md`. Breaking someone's automation on a minor update is not acceptable.
