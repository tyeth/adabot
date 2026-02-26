#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Adabot Contributors
# SPDX-License-Identifier: MIT
"""
approve_actions.py — Bulk-approve GitHub Actions workflow runs blocked by
                     first-time contributor policy across adabot bump PRs.

Background
----------
When a PR is opened by a contributor who hasn't committed to the repo before,
GitHub blocks their workflow run and marks it `action_required` until a
maintainer explicitly approves it.  The adabot fork account (tyeth-ai-assisted)
doesn't have the `actions:write` scope needed for the upstream adafruit/* repos,
so a separate Adafruit-org PAT is required.

Usage
-----
    ADAFRUIT_GITHUB_TOKEN=$(gh auth token) .env/bin/python approve_actions.py
    ADAFRUIT_GITHUB_TOKEN=ghp_xxx .env/bin/python approve_actions.py
    ADAFRUIT_GITHUB_TOKEN=ghp_xxx .env/bin/python approve_actions.py --dry-run
    ADAFRUIT_GITHUB_TOKEN=ghp_xxx .env/bin/python approve_actions.py --repos Adafruit-SHT4x-Arduino-Library

The token needs:
    - repo scope  (or at minimum: public_repo + actions:write)
    - Must belong to a user with write access to adafruit/* repos

State file
----------
Reads web_state.json to find repos with open bump PRs.
Re-queries the GitHub API to confirm which runs still need approval
(the state file is used only as the source of repo/PR numbers — it is
never written by this script).
"""
import argparse
import json
import os
import subprocess
import sys

STATE_FILE = os.path.join(os.path.dirname(__file__), "web_state.json")


# ---------------------------------------------------------------------------
# gh CLI wrapper
# ---------------------------------------------------------------------------

def _gh(token, *args):
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    result = subprocess.run(
        ["gh"] + list(args),
        capture_output=True, text=True, env=env,
    )
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def get_pr_head_sha(token, upstream, pr_number):
    """Return the full head commit SHA for a PR, or None on failure."""
    ok, out, err = _gh(token, "pr", "view", str(pr_number),
                       "--repo", upstream, "--json", "headRefOid,state")
    if not ok:
        return None, None
    try:
        data = json.loads(out)
        return data.get("headRefOid"), data.get("state", "open").lower()
    except (json.JSONDecodeError, AttributeError):
        return None, None


def find_action_required_runs(token, upstream, head_sha):
    """Return list of workflow run dicts with status=action_required for the given commit SHA."""
    ok, out, _ = _gh(token, "api",
                     f"/repos/{upstream}/actions/runs"
                     f"?head_sha={head_sha}&status=action_required&per_page=30")
    if not ok or not out:
        return []
    try:
        return json.loads(out).get("workflow_runs", [])
    except (json.JSONDecodeError, AttributeError):
        return []


def get_all_runs_for_sha(token, upstream, head_sha):
    """Return all workflow runs for the given commit SHA (any status)."""
    ok, out, _ = _gh(token, "api",
                     f"/repos/{upstream}/actions/runs"
                     f"?head_sha={head_sha}&per_page=30")
    if not ok or not out:
        return []
    try:
        return json.loads(out).get("workflow_runs", [])
    except (json.JSONDecodeError, AttributeError):
        return []


def approve_run(token, upstream, run_id, dry_run=False):
    """Approve a single workflow run. Returns (ok, error_message)."""
    if dry_run:
        print(f"      [dry-run] Would approve run {run_id}")
        return True, ""
    ok, out, err = _gh(token, "api", "--method", "POST",
                       f"/repos/{upstream}/actions/runs/{run_id}/approve")
    return ok, err


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def collect_open_bump_prs(state, filter_repos=None):
    """Yield (repo_name, pr_number) for all repos with open bump PRs."""
    for name, repo in state.get("repos", {}).items():
        if filter_repos and name not in filter_repos:
            continue
        bump = repo.get("bump_pr")
        if not bump:
            continue
        pr_state = (bump.get("state") or "open").lower()
        if pr_state == "merged":
            continue
        pr_number = bump.get("number")
        if pr_number:
            yield name, str(pr_number)


def run(token, dry_run=False, filter_repos=None, verbose=False):
    if not os.path.exists(STATE_FILE):
        print(f"ERROR: {STATE_FILE} not found — run the web collector first", file=sys.stderr)
        sys.exit(1)

    with open(STATE_FILE) as f:
        state = json.load(f)

    prs = list(collect_open_bump_prs(state, filter_repos))
    if not prs:
        print("No repos with open bump PRs found in web_state.json.")
        return

    print(f"Checking {len(prs)} repo(s) with open bump PRs…\n")

    approved_total = 0
    skipped_merged = 0
    no_runs = 0
    already_ok = 0
    errors = 0

    for name, pr_number in sorted(prs):
        upstream = f"adafruit/{name}"
        print(f"  {name}  (PR #{pr_number})")

        head_sha, pr_state = get_pr_head_sha(token, upstream, pr_number)
        if pr_state and pr_state == "merged":
            print(f"    PR is already merged — skipping")
            skipped_merged += 1
            continue
        if not head_sha:
            print(f"    Could not fetch PR head SHA — skipping")
            errors += 1
            continue

        runs = find_action_required_runs(token, upstream, head_sha)

        if not runs:
            all_runs = get_all_runs_for_sha(token, upstream, head_sha) if verbose else []
            if all_runs:
                statuses = ", ".join(sorted({r.get("status", "?") for r in all_runs}))
                print(f"    {len(all_runs)} run(s) found, none need approval  [{statuses}]")
                already_ok += 1
            else:
                # Could be no CI configured, or runs haven't been created yet
                print(f"    No workflow runs found for {head_sha[:7]}"
                      f" — CI may not be configured or run hasn't been queued yet")
                no_runs += 1
            continue

        print(f"    {len(runs)} run(s) awaiting approval:")
        for run in runs:
            run_id = run.get("id")
            wf_name = run.get("name") or run.get("workflow_id", "?")
            event = run.get("event", "")
            print(f"      [{run_id}] {wf_name}  (event: {event})")
            if run_id:
                ok, err = approve_run(token, upstream, run_id, dry_run=dry_run)
                if ok:
                    print(f"        ✓ Approved")
                    approved_total += 1
                else:
                    print(f"        ✗ Failed: {err}")
                    errors += 1

    print(f"\n{'─' * 52}")
    print(f"  Approved        : {approved_total}")
    print(f"  Already running : {already_ok}")
    print(f"  No CI runs yet  : {no_runs}")
    print(f"  Merged (skipped): {skipped_merged}")
    print(f"  Errors          : {errors}")
    if dry_run:
        print("\n  (dry-run — no approvals were actually sent)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Approve pending GitHub Actions runs for adabot bump PRs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be approved without sending any API requests",
    )
    parser.add_argument(
        "--token",
        help="GitHub PAT with actions:write on adafruit/* repos "
             "(default: ADAFRUIT_GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "--repos", nargs="*", metavar="REPO",
        help="Limit to specific repo names (default: all repos with open bump PRs)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show run statuses for repos that don't need approval",
    )
    args = parser.parse_args()

    token = (
        args.token
        or os.environ.get("ADAFRUIT_GITHUB_TOKEN")
        or os.environ.get("ADABOT_GITHUB_ACCESS_TOKEN")
    )
    if not token:
        print(
            "ERROR: Provide a GitHub token via --token or ADAFRUIT_GITHUB_TOKEN env var.\n"
            "The token needs repo + actions:write scope on adafruit/* repos.",
            file=sys.stderr,
        )
        sys.exit(1)

    run(token,
        dry_run=args.dry_run,
        filter_repos=set(args.repos) if args.repos else None,
        verbose=args.verbose)


if __name__ == "__main__":
    main()
