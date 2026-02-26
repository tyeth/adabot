#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Adabot Contributors
# SPDX-License-Identifier: MIT
"""
Entry point for the Adabot Arduino Release Manager web UI.

Usage:
    .env/bin/python run_web.py [--collect] [--port 5000]

Flags:
    --collect   Trigger a fresh data collection on startup (background)
    --port N    Port to listen on (default 5000)
"""
import argparse
import logging
import os
import sys

# Set credentials if not already in environment
os.environ.setdefault("ADABOT_GITHUB_USER", "tyeth-ai-assisted")
os.environ.setdefault(
    "ADABOT_GITHUB_ACCESS_TOKEN",
    "MISSING_TOKEN",
)

LOG_FILE = os.path.join(os.path.dirname(__file__), "adabot_web.log")

_file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), _file_handler],
)

parser = argparse.ArgumentParser(description="Adabot Release Manager Web UI")
parser.add_argument("--collect", action="store_true", help="Start data collection on launch")
parser.add_argument("--port", type=int, default=5000, help="Port (default 5000)")
args = parser.parse_args()

from adabot_web.app import app
from adabot_web import collector

app.config["TEMPLATES_AUTO_RELOAD"] = True

# Flask's app.logger propagates to root by default, so the file handler above
# will capture it. Just ensure the level is set so nothing is filtered early.
app.logger.setLevel(logging.INFO)

if args.collect:
    logging.info("Starting background data collection…")
    collector.start_collection(force=True)
else:
    state = collector.load_state()
    if not state.get("repos"):
        logging.info("No existing data found. Run with --collect to fetch data,")
        logging.info("or visit http://localhost:%d and click Refresh Data.", args.port)
    elif collector.is_stale(state):
        age = collector.data_age_days(state)
        logging.warning(
            "Data is %.1f days old (stale after %d days). "
            "Use --collect or click Refresh Data to update.",
            age, collector.STALE_DAYS,
        )
    else:
        age = collector.data_age_days(state)
        logging.info(
            "Loaded %d repos from cache (%.1f days old).",
            len(state["repos"]), age,
        )

logging.info("Starting web server on http://localhost:%d", args.port)
app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
