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
import re
import subprocess
import sys

# Set credentials if not already in environment
os.environ.setdefault("ADABOT_GITHUB_USER", "tyeth")
# os.environ.setdefault(
#     "ADABOT_GITHUB_ACCESS_TOKEN",
#     "MISSING_TOKEN",
# )

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

def _check_gh_version():
    """Abort startup if gh CLI is missing or too old.

    Versions 2.45.x and 2.46.x shipped with Ubuntu Noble lack `gh pr checks --json`
    and have other regressions that break CI checking.  Anything < 2.47.0 is rejected.

    Upgrade instructions (Ubuntu / WSL):
        sudo apt-get remove gh
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \\
            | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
        sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) \\
            signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \\
            https://cli.github.com/packages stable main" \\
            | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        sudo apt-get update && sudo apt-get install gh
    """
    try:
        result = subprocess.run(
            ["gh", "--version"], capture_output=True, text=True, timeout=10
        )
        version_line = result.stdout.splitlines()[0] if result.stdout else ""
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logging.critical("gh CLI not found or unresponsive: %s", exc)
        logging.critical(
            "Install gh from https://cli.github.com/ then restart."
        )
        sys.exit(1)

    # Parse "gh version X.Y.Z (...)"
    m = re.search(r"gh version (\d+)\.(\d+)\.(\d+)", version_line)
    if not m:
        logging.warning("Could not parse gh version from: %r — skipping check.", version_line)
        return

    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    version_tuple = (major, minor, patch)
    MIN_VERSION = (2, 47, 0)

    if version_tuple < MIN_VERSION:
        logging.critical(
            "gh CLI version %d.%d.%d is too old (minimum required: %d.%d.%d).",
            major, minor, patch, *MIN_VERSION,
        )
        logging.critical(
            "Versions 2.45.x and 2.46.x (Ubuntu Noble default) are known to be broken."
        )
        logging.critical("Run the following commands to upgrade gh:")
        logging.critical("  sudo apt-get remove gh")
        logging.critical(
            "  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg"
            " | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg"
        )
        logging.critical(
            "  sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg"
        )
        logging.critical(
            "  echo \"deb [arch=$(dpkg --print-architecture)"
            " signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg]"
            " https://cli.github.com/packages stable main\""
            " | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null"
        )
        logging.critical("  sudo apt-get update && sudo apt-get install gh")
        sys.exit(1)

    logging.info("gh CLI version %d.%d.%d — OK.", major, minor, patch)


def _log_gh_identity():
    """Log the GitHub user and token being used so it's visible on every startup."""
    user_env  = os.environ.get("ADABOT_GITHUB_USER")
    token_env = os.environ.get("ADABOT_GITHUB_ACCESS_TOKEN")

    user  = user_env  or "tyeth-ai-assisted (default)"
    label = user_env  and "ADABOT_GITHUB_USER"  or "built-in default"
    logging.info("GitHub user : %s  [%s]", user, label)

    if token_env:
        masked = token_env[:5] + "*" * (len(token_env) - 9) + token_env[-4:] if len(token_env) > 9 else "****"
        logging.info("GitHub token: %s  [ADABOT_GITHUB_ACCESS_TOKEN]", masked)
    else:
        logging.warning("GitHub token: MISSING — set ADABOT_GITHUB_ACCESS_TOKEN or API calls will fail")

    env = os.environ.copy()
    if token_env:
        env["GH_TOKEN"] = token_env
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    auth_out = (result.stdout + result.stderr).strip()
    for line in auth_out.splitlines():
        line = line.strip()
        if line:
            logging.info("gh auth: %s", line)

    git_result = subprocess.run(
        ["git", "config", "--get-regexp", "user"],
        capture_output=True, text=True, timeout=5,
    )
    for line in git_result.stdout.splitlines():
        line = line.strip()
        if line:
            logging.info("git config: %s", line)


_check_gh_version()
_log_gh_identity()
logging.info("Starting web server on http://localhost:%d", args.port)
app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
