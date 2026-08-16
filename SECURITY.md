# Security Policy

## Scope

StormWatch is a self-hosted container you run on your own infrastructure (Unraid, plain Docker,
etc.) — there is no cloud service, hosted API, or account system operated by the project. This
policy covers the StormWatch codebase and the container image published to
`ghcr.io/hngyhngyhobo/stormwatch`.

## Supported versions

Only the **latest major version** is supported with security fixes. If you're running an older
major version, please upgrade before reporting — see [README.md](README.md#updating) for the
update path.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report privately by emailing **christownsend17@gmail.com** with:

- A description of the vulnerability and its potential impact.
- Steps to reproduce, or a proof of concept if you have one.
- The version/commit you tested against.

You should expect an initial response within **7 days**. Once a fix is confirmed, we'll coordinate
on disclosure timing and credit (if you'd like it) before any public writeup or CHANGELOG entry
that names the issue.

## What's in scope

- The StormWatch application code (`src/stormwatch/`).
- The published Docker image and its build process.
- Configuration handling that could lead to credential exposure (e.g. Xweather credentials,
  MQTT credentials) or unintended network exposure.

Issues in third-party services StormWatch talks to (NWS `api.weather.gov`, Blitzortung, Vaisala
Xweather) are out of scope here — report those to the respective provider.
