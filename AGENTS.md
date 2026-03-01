# Adabot — Agent Guide

## Scope

This document covers the **Arduino Release Manager** web UI (`adabot_web/`).
It is the Arduino-library-only equivalent of `adabot/arduino_libraries.py` —
managing version bumps, release notes, and GitHub releases for Adafruit's
Arduino libraries.

For the **CircuitPython** side of adabot (bundles, library validators, download
stats, patches, releases, etc.) this document does not yet apply. Refer to the
top-level `README.rst` and the modules under `adabot/` directly — key files
include `circuitpython_libraries.py`, `circuitpython_library_release.py`,
`circuitpython_bundle.py`, and the validators in `adabot/lib/`.

## Web App

See [`adabot_web/plan.md`](adabot_web/plan.md) for the detailed implementation
plan covering architecture, LLM integration, release-notes generation, and
current TODOs.

See [`adabot_web/README.md`](adabot_web/README.md) for quick-start, UI layout,
keyboard shortcuts, and the full release workflow.

## Running the Server

The server needs a GitHub token with `repo`, `workflow`, and `read:org` scopes.
There are two ways to provide it:

1. **Environment variable** — set `ADABOT_GITHUB_ACCESS_TOKEN` before launch.
   `run_web.py` also sets `ADABOT_GITHUB_USER` via `os.environ.setdefault`.

2. **One-liner from the README** (recommended during development) — safely
   kills only the `run_web.py` process on your port, grabs a fresh token from
   `gh auth`, and restarts:

   ```bash
   kill $(ps aux | grep '[r]un_web.py --port 8080' | awk '{print $2}') 2>/dev/null; \
     ADABOT_GITHUB_ACCESS_TOKEN=$(gh auth token) .venv/bin/python run_web.py --port 8080
   ```

   Verify which GitHub account is active with `gh auth status`.
