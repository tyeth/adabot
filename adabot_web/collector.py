# SPDX-FileCopyrightText: 2026 Adabot Contributors
# SPDX-License-Identifier: MIT
"""
Data collector: reuses arduino_libraries checks and saves results incrementally
to web_state.json so the web UI can display live progress and resume.
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
import requests_cache
import semver

from adabot import arduino_libraries as al
from adabot import github_requests as gh_reqs

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "web_state.json")
STATE_FILE = os.path.abspath(STATE_FILE)

_lock = threading.Lock()
_collection_thread = None

CATEGORY_NEEDS_RELEASE    = "needs_release"
CATEGORY_FAILED_LIB_PROP  = "failed_lib_prop"
CATEGORY_NEEDS_REGISTRATION = "needs_registration"
CATEGORY_MISSING_ACTIONS  = "missing_actions"
CATEGORY_MISSING_LIB_PROPS = "missing_lib_props"
CATEGORY_NO_EXAMPLES      = "no_examples"
CATEGORY_NO_RELEASE_TAG   = "no_release_tag"
CATEGORY_ERROR            = "error"

STALE_DAYS = 3

# In-memory cache of the parsed Arduino library index. The raw index is a
# ~58 MB download, so never fetch it more than once an hour except for a
# full collect run, which always gets a fresh copy.
_ARDUINO_INDEX_TTL = 3600
_arduino_index_cache = {"ts": 0.0, "data": None}


def _fetch_arduino_index(force=False):
    """Download + parse the Arduino library index (Adafruit entries only).

    Returns {lib_name: newest_version}. Raises on failure; callers fall back
    to the copy stored in web_state.json.
    """
    now = time.time()
    if (not force and _arduino_index_cache["data"] is not None
            and now - _arduino_index_cache["ts"] < _ARDUINO_INDEX_TTL):
        return _arduino_index_cache["data"]

    with requests_cache.disabled():
        reply = requests.get(
            "http://downloads.arduino.cc/libraries/library_index.json",
            timeout=600,
        )
    if not reply.ok:
        raise RuntimeError("Could not fetch Arduino library index")

    arduino_index = {}
    for lib in reply.json().get("libraries", []):
        if "adafruit" in lib.get("url", ""):
            lib_name = lib.get("name", "")
            ver = lib.get("version", "")
            if not lib_name or not ver:
                continue
            if lib_name not in arduino_index:
                arduino_index[lib_name] = ver
            else:
                try:
                    if semver.compare(ver, arduino_index[lib_name]) > 0:
                        arduino_index[lib_name] = ver
                except ValueError:
                    pass  # non-semver version strings, keep first

    _arduino_index_cache["data"] = arduino_index
    _arduino_index_cache["ts"] = now
    return arduino_index


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state():
    """Load state from JSON file, returning default if missing/corrupt.

    Also reconciles release_status for repos where evidence (released_tag or
    matching bump_pr) indicates a release happened but status was lost.
    """
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            # Self-heal any repos with inconsistent release status
            for repo in state.get("repos", {}).values():
                _reconcile_release_status(repo)
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return _default_state()


def _default_state():
    return {
        "last_run_start": None,
        "last_run_end": None,
        "status": "idle",
        "progress": 0,
        "total": 0,
        "repos": {},
        "arduino_library_index": {},
    }


def save_state(state):
    with _lock:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, STATE_FILE)


def data_age_days(state):
    ts = state.get("last_run_end")
    if not ts:
        return None
    try:
        last = datetime.fromisoformat(ts)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


def is_stale(state):
    age = data_age_days(state)
    return age is None or age > STALE_DAYS


def user_notes_current(repo_data):
    """Check whether user-edited release notes are still current.

    The stamp is ``"<proposed_version>@<commit_count>"``; notes are stale if
    the proposed version changed OR the number of commits changed.

    The stamp_tag at creation time uses: bump_pr.new_version > release_tag.
    We must reconstruct the same value here for comparison.

    Returns True if notes are current and should be kept, False if stale.
    Returns False if there is no user-edit stamp.
    """
    stamp = repo_data.get("release_notes_user_edited")
    if not stamp:
        return False
    # Reconstruct stamp_tag the same way it was created in app.py save_notes()
    bump = repo_data.get("bump_pr")
    if bump and bump.get("new_version"):
        stamp_tag = bump["new_version"]
    else:
        stamp_tag = repo_data.get("release_tag") or ""
    n_commits = len(repo_data.get("recent_commits", []))
    return stamp == f"{stamp_tag}@{n_commits}"


def is_running():
    global _collection_thread
    return _collection_thread is not None and _collection_thread.is_alive()


def start_collection(force=False):
    global _collection_thread
    if is_running() and not force:
        return False
    _collection_thread = threading.Thread(target=_run_collection, daemon=True)
    _collection_thread.start()
    return True


def refresh_single_repo(name):
    """Re-run Phase 1 + Phase 2 for one repo in a background thread.

    Returns False immediately if a full collection is already running
    (to avoid clobbering the state mid-run).
    """
    if is_running():
        return False
    t = threading.Thread(target=_run_single_repo, args=(name,), daemon=True)
    t.start()
    return True


def _save_repo_merge(name, repo_data):
    """Re-load fresh state, preserve user fields from disk, save only this repo.

    This avoids a race where a background refresh holds a stale snapshot and
    clobbers user-set fields (like release_status) that were updated by a
    concurrent request (e.g. POST /api/repo/<name>/status).
    """
    fresh = load_state()
    fresh_repos = fresh.setdefault("repos", {})
    prev = fresh_repos.get(name, {})
    for k in _USER_PRESERVED:
        if prev.get(k) is not None:
            repo_data[k] = prev[k]
    _reconcile_release_status(repo_data)
    _clear_completed_cycle(repo_data)
    fresh_repos[name] = repo_data
    save_state(fresh)


def _run_single_repo(name):
    """Phase 1 + Phase 2 for a single named repo, writing back to web_state.json."""
    logger.info("Single-repo refresh starting: %s", name)
    state = load_state()

    # Fetch the repo metadata directly from GitHub API
    resp = gh_reqs.get(f"/repos/adafruit/{name}")
    if not resp.ok:
        logger.error("Single-repo refresh: could not fetch repo metadata for %s (%s)", name, resp.status_code)
        return

    repo = resp.json()

    # Fetch Arduino library index (needed for registration check)
    try:
        arduino_index = _fetch_arduino_index()
    except Exception as e:
        logger.warning("Single-repo refresh: could not fetch Arduino index: %s", e)
        arduino_index = state.get("arduino_library_index", {})

    repo_data = _process_repo(repo, arduino_index)

    # Always re-enrich (clear details_loaded so _enrich_details runs)
    repo_data["details_loaded"] = False
    _save_repo_merge(name, repo_data)

    _enrich_details(repo_data)
    _save_repo_merge(name, repo_data)
    logger.info("Single-repo refresh done: %s", name)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

_USER_PRESERVED = (
    "release_notes", "release_notes_user_edited",
    "release_status", "bump_pr", "released_tag",
    "branch_ci_status",
    "release_ci_status", "release_ci_checks",
)
_ENRICHMENT_CACHE = (
    "recent_commits", "prs", "version_files",
    "bump_type", "bump_justification", "example_releases",
    "compare_files", "gh_auto_notes",
    "details_loaded",
)

def _reconcile_release_status(repo_data):
    """Ensure release_status is consistent with other release evidence.

    Handles two cases:
    1. released_tag exists but release_status was cleared (state corruption)
    2. Manual release on GitHub: release_tag matches bump_pr.new_version
       but app never set release_status (release happened outside adabot)
    """
    rs = repo_data.get("release_status")
    if rs == "released":
        return  # already correct

    # Case 1: released_tag exists → we released it via the app at some point
    if repo_data.get("released_tag"):
        repo_data["release_status"] = "released"
        return

    # Case 2: release_tag matches bump_pr.new_version → manual GitHub release
    bump = repo_data.get("bump_pr")
    if bump and bump.get("new_version") and bump.get("state") == "merged":
        release_tag = repo_data.get("release_tag") or ""
        new_ver = bump["new_version"]
        if release_tag == new_ver or release_tag == f"v{new_ver}" or f"v{release_tag}" == new_ver:
            repo_data["release_status"] = "released"
            repo_data["released_tag"] = release_tag


def _clear_completed_cycle(repo_data):
    """Clear stale release cycle fields when repo has been released AND has new commits."""
    if repo_data.get("release_status") != "released":
        return
    commits_behind = repo_data.get("commits_behind") or 0
    if not commits_behind:
        return  # still just "released", no new work yet

    # Don't clear if there's an open bump PR — it belongs to the next cycle
    bump = repo_data.get("bump_pr")
    if bump and bump.get("state") not in (None, "merged"):
        return

    # Released + new commits + no open bump PR → full cycle reset
    repo_data["bump_pr"] = None
    repo_data["release_status"] = None
    repo_data["released_tag"] = None
    repo_data["release_ci_status"] = None
    repo_data["release_ci_checks"] = None
    repo_data["branch_ci_status"] = None
    repo_data["release_notes"] = None
    repo_data["release_notes_user_edited"] = None


def _freshen_user_fields(state):
    """Re-read user-preserved fields from disk before saving.

    This avoids a race where the long-running collection thread holds a stale
    snapshot and clobbers user-set fields (like release_status) that were
    updated by a concurrent request during the run.
    """
    fresh = load_state()
    fresh_repos = fresh.get("repos", {})
    for name, repo in state.get("repos", {}).items():
        fresh_repo = fresh_repos.get(name, {})
        for k in _USER_PRESERVED:
            if fresh_repo.get(k) is not None:
                repo[k] = fresh_repo[k]
        _reconcile_release_status(repo)
        _clear_completed_cycle(repo)


def _run_collection():
    state = load_state()
    # Snapshot previous run: enrichment cache restored when pushed_at unchanged.
    preserved = {
        name: {k: v for k, v in repo.items()
               if k in _USER_PRESERVED + _ENRICHMENT_CACHE + ("pushed_at",)}
        for name, repo in state.get("repos", {}).items()
    }

    state["status"] = "running_basic"
    state["last_run_start"] = datetime.now(timezone.utc).isoformat()
    state["progress"] = 0
    state["error"] = None
    save_state(state)

    try:
        # Arduino library index — full runs always get a fresh copy
        arduino_index = _fetch_arduino_index(force=True)
        state["arduino_library_index"] = arduino_index

        # ── Phase 1: basic info (categories, versions, registration) ──
        repo_list = al.list_repos()
        state["total"] = len(repo_list)
        state["repos"] = {}
        save_state(state)
        logger.info("Collector: found %d repos — starting basic scan", len(repo_list))

        for i, repo in enumerate(repo_list):
            state["progress"] = i + 1
            repo_data = _process_repo(repo, arduino_index)  # sets fresh pushed_at
            saved = preserved.get(repo["name"], {})

            # Restore enrichment cache if the repo hasn't changed since last run
            if (repo_data.get("pushed_at")
                    and repo_data["pushed_at"] == saved.get("pushed_at")
                    and saved.get("details_loaded")):
                for k in _ENRICHMENT_CACHE:
                    if saved.get(k) is not None:
                        repo_data[k] = saved[k]
                logger.debug("Enrichment cache hit (pushed_at unchanged): %s", repo["name"])

            # Restore user-set fields from preserved snapshot (freshened at save time)
            for k in _USER_PRESERVED:
                if saved.get(k) is not None:
                    repo_data[k] = saved[k]

            state["repos"][repo["name"]] = repo_data
            if i % 20 == 0:
                _freshen_user_fields(state)
                save_state(state)

        _freshen_user_fields(state)
        save_state(state)
        logger.info("Basic scan done. Enriching details…")

        # ── Phase 2: details (PRs, commits, version files, bump suggestion) ──
        state["status"] = "running_details"
        to_enrich = [
            name for name, rd in state["repos"].items()
            if not rd.get("details_loaded") and not rd.get("error")
        ]
        state["total"] = len(to_enrich)
        state["progress"] = 0
        _freshen_user_fields(state)
        save_state(state)

        for i, name in enumerate(to_enrich):
            state["progress"] = i + 1
            _enrich_details(state["repos"][name])
            if i % 10 == 0:
                _freshen_user_fields(state)
                save_state(state)

        state["status"] = "done"
        state["last_run_end"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        logger.exception("Collector failed: %s", e)

    _freshen_user_fields(state)
    save_state(state)


def _process_repo(repo, arduino_index):
    """Process a single repo. Isolated — never raises, returns error entry on failure."""
    name = repo["name"]
    base = {
        "name": name,
        "full_name": repo.get("full_name", f"adafruit/{name}"),
        "html_url": repo.get("html_url", f"https://github.com/adafruit/{name}"),
        "pushed_at": repo.get("pushed_at", ""),
        "default_branch": repo.get("default_branch", "main"),
        "categories": [],
        "release_tag": None,
        "lib_version": None,
        "commits_behind": None,
        "compare_url": None,
        "ci_only": False,
        "arduino_registered": False,
        "arduino_version": None,
        "has_actions": None,
        "has_examples": None,
        "has_lib_props": None,
        "bump_pr": None,
        "release_status": None,
        "release_notes": None,
        "prs": [],
        "recent_commits": [],
        "version_files": [],        # other files containing version strings
        "branch_ci_status": None,   # CI on default branch after bump PR merged
        "details_loaded": False,    # True after Phase 2 enrichment
        "bump_type": None,          # suggested: patch / minor / major
        "bump_justification": None, # reason for the suggestion
        "error": None,
    }
    try:
        return _process_repo_inner(repo, arduino_index, base)
    except Exception as e:
        logger.exception("Error processing %s", name)
        base["error"] = str(e)
        base["categories"] = [CATEGORY_ERROR]
        return base


def _process_repo_inner(repo, arduino_index, entry):
    name = repo["name"]

    # -- Examples check --
    entry["has_examples"] = bool(al.validate_example(repo))
    if not entry["has_examples"]:
        entry["has_lib_props"] = bool(al.is_arduino_library(repo))
        entry["categories"].append(CATEGORY_NO_EXAMPLES)
        return entry

    # -- library.properties (using our fixed version) --
    lib_check = _validate_lib_props(repo)
    if not lib_check:
        entry["has_lib_props"] = False
        entry["categories"].append(CATEGORY_MISSING_LIB_PROPS)
        return entry

    entry["has_lib_props"] = True
    release_tag_raw, lib_version, lib_name = lib_check
    entry["lib_version"] = lib_version
    if lib_name:
        entry["lib_name"] = lib_name  # name= from library.properties

    if release_tag_raw in ("None", "Unknown"):
        entry["release_tag"] = None
        entry["categories"].append(CATEGORY_NO_RELEASE_TAG)
        entry["categories"].append(CATEGORY_NEEDS_RELEASE)
        entry["compare_url"] = f"{repo['html_url']}/compare/{repo['default_branch']}...HEAD"
        return entry

    release_tag = release_tag_raw.split(" ")[0]
    entry["release_tag"] = release_tag

    # -- Version mismatch --
    if release_tag != lib_version:
        try:
            ci_only = al.check_changes_for_ci_only(repo, lib_version, release_tag)
        except Exception:
            ci_only = False
        entry["ci_only"] = ci_only
        entry["categories"].append(CATEGORY_FAILED_LIB_PROP)

    # -- Arduino registration --
    # arduino_index is keyed by the library 'name' field from the index (spaces, mixed case).
    # GitHub repo names use hyphens, so we must look up by lib_name from library.properties.
    def _norm(s):
        return re.sub(r"[\s\-_]+", "", s).lower()
    arduino_ver = (
        arduino_index.get(lib_name)                          # exact lib_name match
        or arduino_index.get(name)                           # exact repo name (unlikely)
        or next((v for k, v in arduino_index.items()         # normalised fallback
                 if _norm(k) == _norm(lib_name or name)), None)
    )
    entry["arduino_version"] = arduino_ver
    entry["arduino_registered"] = arduino_ver is not None
    if not entry["arduino_registered"]:
        entry["categories"].append(CATEGORY_NEEDS_REGISTRATION)

    # -- Needs release --
    repo["tag_name"] = release_tag
    needs_release = al.validate_release_state(repo)
    if needs_release:
        try:
            ci_only = al.check_changes_for_ci_only(repo, release_tag, repo["default_branch"])
        except Exception:
            ci_only = False
        entry["ci_only"] = ci_only
        entry["commits_behind"] = needs_release[1]
        entry["compare_url"] = f"{repo['html_url']}/compare/{release_tag}...HEAD"
        entry["categories"].append(CATEGORY_NEEDS_RELEASE)

    # -- CI actions --
    entry["has_actions"] = _validate_actions(repo)
    if not entry["has_actions"]:
        entry["categories"].append(CATEGORY_MISSING_ACTIONS)

    return entry


# Trigger events that count as "real CI" (push/PR-driven or release-driven)
_CI_TRIGGER_EVENTS = {"push", "pull_request", "release", "repository_dispatch", "workflow_dispatch"}


def _validate_actions(repo):
    """
    Returns True if the repo has at least one .github/workflows/*.yml file
    with an appropriate trigger event (push, pull_request, release,
    repository_dispatch, or workflow_dispatch).

    Replaces al.validate_actions() which only checks for 'githubci.yml' by name.
    """
    name = repo["name"]
    resp = gh_reqs.get(f"/repos/adafruit/{name}/contents/.github/workflows")
    if not resp.ok:
        return False

    files = resp.json()
    if not isinstance(files, list):
        return False

    yml_files = [f for f in files if isinstance(f, dict)
                 and f.get("name", "").lower().endswith((".yml", ".yaml"))]
    if not yml_files:
        return False

    for wf_file in yml_files:
        raw_url = wf_file.get("download_url")
        if not raw_url:
            continue
        try:
            wf_resp = requests.get(raw_url, timeout=10)
            if not wf_resp.ok:
                continue
            content = wf_resp.text
            # Quick YAML parse: look for 'on:' key and check for trigger events.
            # We do a simple text scan rather than a full YAML parse to avoid
            # pulling in pyyaml as a hard dependency here.
            in_on_block = False
            for line in content.splitlines():
                stripped = line.strip()
                # Top-level 'on:' key (flow or block form)
                if re.match(r"^on\s*:", stripped) or re.match(r"^\"on\"\s*:", stripped):
                    in_on_block = True
                    # Flow form: on: [push, pull_request, ...]
                    flow = re.search(r"\[([^\]]+)\]", stripped)
                    if flow:
                        events = [e.strip().lower() for e in flow.group(1).split(",")]
                        if _CI_TRIGGER_EVENTS.intersection(events):
                            return True
                    continue
                # Once we hit another top-level key, the 'on' block is over
                if in_on_block and re.match(r"^\S", line) and not re.match(r"^\s*#", line):
                    if not stripped.startswith("-"):
                        in_on_block = False
                if in_on_block:
                    # Block form: each trigger is either '  push:' or '  - push'
                    event_match = re.match(r"^\s+([a-z_]+)\s*[:\[]?", stripped)
                    if event_match:
                        event = event_match.group(1).lower()
                        if event in _CI_TRIGGER_EVENTS:
                            return True
        except Exception:
            continue

    return False


def _validate_lib_props(repo):
    """
    Fixed replacement for arduino_libraries.validate_library_properties.
    Handles the semver crash when release_tag is 'None' (string).
    """
    name = repo["name"]
    branch = repo.get("default_branch", "main")

    lib_prop_resp = requests.get(
        f"https://raw.githubusercontent.com/adafruit/{name}/{branch}/library.properties"
    )
    if not lib_prop_resp.ok:
        logger.debug("%s skipped - no library.properties", name)
        return None

    lib_version = None
    lib_name = None
    for line in lib_prop_resp.text.splitlines():
        if re.match(r"^version\s*=", line):
            lib_version = line.split("=", 1)[1].strip()
        elif re.match(r"^name\s*=", line):
            lib_name = line.split("=", 1)[1].strip()
    if not lib_version:
        return None

    # Get latest release
    release_tag = "None"
    latest_resp = gh_reqs.get(f"/repos/adafruit/{name}/releases/latest")
    if latest_resp.ok:
        resp_json = latest_resp.json()
        if "tag_name" in resp_json:
            release_tag = resp_json["tag_name"]
        elif resp_json.get("message") not in (None, "Not Found"):
            release_tag = "Unknown"

    # Check if any release is newer than "latest" (non-latest with higher semver)
    all_resp = gh_reqs.get(f"/repos/adafruit/{name}/releases")
    if all_resp.ok:
        releases = all_resp.json()
        if isinstance(releases, list) and releases:
            first_tag = releases[0].get("tag_name", "")
            if first_tag and release_tag not in ("None", "Unknown"):
                try:
                    if semver.compare(first_tag, release_tag) > 0:
                        logger.info("*** Found newer non-latest release for %s: %s", name, first_tag)
                        release_tag = first_tag
                except ValueError:
                    pass  # non-semver tags — skip comparison

    return [release_tag, lib_version, lib_name]


def _fetch_prs(repo, entry):
    try:
        resp = gh_reqs.get(
            f"/repos/{repo['full_name']}/pulls",
            params={"state": "open", "per_page": 20}
        )
        if resp.ok:
            entry["prs"] = [
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "html_url": pr["html_url"],
                    "user": pr["user"]["login"],
                    "created_at": pr["created_at"],
                    "labels": [lb["name"] for lb in pr.get("labels", [])],
                    "draft": pr.get("draft", False),
                }
                for pr in resp.json()
                if isinstance(pr, dict)
            ]
    except Exception:
        pass


_SKIP_DIFF_PATTERNS = {
    ".github/", ".circleci/", ".travis", "makefile",
    ".gitignore", ".clang-format", ".pre-commit",
    "library.properties",  # version-only bump
    ".pylintrc", ".flake8", "pyproject.toml",
    "requirements.txt",    # dependency-only
}


def _is_diff_relevant(filename):
    """True if the file is likely relevant to release notes."""
    lower = filename.lower()
    return not any(skip in lower for skip in _SKIP_DIFF_PATTERNS)


def _is_whitespace_only_diff(patch):
    """True if a unified diff patch only adds/removes whitespace."""
    for line in patch.splitlines():
        if not line.startswith("+") and not line.startswith("-"):
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        content = line[1:]
        if content.strip():
            return False
    return True


def _fetch_commits(repo, entry, from_ref, to_ref):
    try:
        resp = gh_reqs.get(
            f"/repos/{repo['full_name']}/compare/{from_ref}...{to_ref}"
        )
        if resp.ok:
            data = resp.json()
            entry["recent_commits"] = [
                {
                    "sha": c["sha"][:7],
                    "message": c["commit"]["message"].split("\n")[0],
                    "author": c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"],
                    "html_url": c.get("html_url", ""),
                }
                for c in data.get("commits", [])[:30]
                if isinstance(c, dict)
            ]
            # Capture file diffs for LLM context
            entry["compare_files"] = [
                {
                    "filename": f["filename"],
                    "status": f.get("status", "modified"),
                    "patch": (f.get("patch") or "")[:800],
                }
                for f in data.get("files", [])[:40]
                if f.get("patch")
                and _is_diff_relevant(f["filename"])
                and not _is_whitespace_only_diff(f.get("patch", ""))
            ]
    except Exception:
        pass


def _find_version_files(repo, lib_version):
    """
    Scan repo tree for files that may contain version strings (other than library.properties).
    Returns list of {path, url, note} dicts.
    """
    results = []
    name = repo["name"]
    branch = repo.get("default_branch", "main")

    try:
        tree_resp = gh_reqs.get(
            f"/repos/adafruit/{name}/git/trees/{branch}",
            params={"recursive": "1"}
        )
        if not tree_resp.ok:
            return results

        # Files worth checking for version strings. Version defines live in a
        # narrow set of places — don't fetch every vendored/example source.
        source_re = re.compile(r"\.(h|hpp|H|cpp|c|ino)$")
        named_files = (
            "changelog.md", "changelog.rst",
            "library.json",
            "platformio.ini", "setup.py", "pyproject.toml",
            "cmakelists.txt",
        )

        def _is_candidate(path):
            lower = path.lower()
            if lower in named_files:  # named files at repo root only
                return True
            if not source_re.search(path):
                return False
            if lower.startswith(("examples/", "test/", "tests/", "extras/")):
                return False
            if "version" in lower.rsplit("/", 1)[-1]:
                return True  # version.h, foo_version.hpp — any depth
            # main library sources live at the root or directly under src/
            depth = path.count("/")
            return depth == 0 or (path.startswith("src/") and depth == 1)

        interesting = [
            item["path"] for item in tree_resp.json().get("tree", [])
            if item.get("type") == "blob"
            and not item["path"].startswith(".")
            and _is_candidate(item["path"])
        ]

        def _fetch_raw(path):
            try:
                resp = requests.get(
                    f"https://raw.githubusercontent.com/adafruit/{name}/{branch}/{path}"
                )
                return resp.text if resp.ok else None
            except requests.RequestException:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            contents = list(pool.map(_fetch_raw, interesting))

        for path, content in zip(interesting, contents):
            if content is None:
                continue

            # Look for version-like strings matching lib_version; track line number
            found_ver = None
            found_line = None
            ver_re = re.compile(
                r'(?:VERSION|version|ver)["\']?\s*[="\s:]\s*["\']?([\d]+\.[\d]+\.[\d]+(?:-[\w.]+)?)["\']?',
                re.IGNORECASE,
            )
            # Standalone quoted semver (for #define continuation lines)
            bare_ver_re = re.compile(
                r'^\s*["\'](\d+\.\d+\.\d+(?:-[\w.]+)?)["\']'
            )
            lines = content.splitlines()
            for lineno, line in enumerate(lines, 1):
                m = ver_re.search(line)
                if m:
                    found_ver = m.group(1)
                    found_line = lineno
                    break
                # #define SOMETHING_VERSION  \ (continuation on next line)
                if re.search(r'(?:VERSION|version|ver)\s*\\?\s*$', line, re.IGNORECASE):
                    if lineno < len(lines):
                        m2 = bare_ver_re.search(lines[lineno])  # next line (0-indexed = lineno)
                        if m2:
                            found_ver = m2.group(1)
                            found_line = lineno + 1
                            break

            if found_ver:
                note = "matches" if found_ver == lib_version else f"has {found_ver}"
                line_anchor = f"#L{found_line}" if found_line else ""
                results.append({
                    "path": path,
                    "line": found_line,
                    "url": f"https://github.com/adafruit/{name}/blob/{branch}/{path}{line_anchor}",
                    "version_found": found_ver,
                    "matches": found_ver == lib_version,
                    "note": note,
                })

    except Exception as e:
        logger.debug("version file scan failed for %s: %s", name, e)

    return results


# ---------------------------------------------------------------------------
# Recent releases (for LLM style examples)
# ---------------------------------------------------------------------------

def _release_qualifier(tag):
    """Extract the prerelease qualifier from a tag, e.g. 'beta' from '1.0.0-beta.3'.

    Returns the qualifier string (e.g. 'beta', 'offline') or '' for stable releases.
    """
    raw = tag.strip().lstrip("v")
    try:
        sv = semver.VersionInfo.parse(raw)
        if sv.prerelease:
            # e.g. 'beta.122' → 'beta', 'offline.5' → 'offline'
            return sv.prerelease.split(".")[0]
    except ValueError:
        # Fallback: look for -qualifier.N pattern
        m = re.search(r'-([a-zA-Z]+)', raw)
        if m:
            return m.group(1).lower()
    return ""


def _fetch_recent_releases(repo, repo_data, count=3):
    """Fetch the last `count` GitHub releases matching the current qualifier.

    Repos may publish releases with different qualifiers (e.g. -beta, -offline)
    that use different release note styles. We filter to only return examples
    that match the current version's qualifier so the LLM gets consistent style.
    """
    name = repo["name"]
    current_tag = repo_data.get("release_tag") or repo_data.get("lib_version") or ""
    target_qual = _release_qualifier(current_tag)

    try:
        # Fetch more than needed so we can filter by qualifier
        resp = gh_reqs.get(
            f"/repos/adafruit/{name}/releases",
            params={"per_page": min(count * 5, 30)},
        )
        if not resp.ok:
            return
        releases = []
        for r in resp.json():
            tag = r.get("tag_name", "")
            body = (r.get("body") or "").strip()
            if not body:
                continue
            if _release_qualifier(tag) != target_qual:
                continue
            releases.append({"tag": tag, "body": body})
            if len(releases) >= count:
                break
        repo_data["example_releases"] = releases
    except Exception as e:
        logger.debug("Failed to fetch releases for %s: %s", name, e)


# ---------------------------------------------------------------------------
# GitHub auto-generated release notes
# ---------------------------------------------------------------------------

def _fetch_gh_auto_notes(repo, repo_data, proposed_tag):
    """Fetch GitHub's auto-generated release notes via the generate-notes API.

    Stores the result in repo_data["gh_auto_notes"].
    Gracefully handles failures (logs + returns None).
    """
    name = repo["name"]
    default_branch = repo.get("default_branch", "main")
    release_tag = repo_data.get("release_tag")

    payload = {
        "tag_name": proposed_tag,
        "target_commitish": default_branch,
    }
    if release_tag and release_tag not in ("None", "Unknown"):
        payload["previous_tag_name"] = release_tag

    try:
        resp = gh_reqs.post(
            f"/repos/adafruit/{name}/releases/generate-notes",
            json=payload,
        )
        if resp.ok:
            body = resp.json().get("body", "")
            repo_data["gh_auto_notes"] = body
            return body
        else:
            logger.debug("generate-notes API returned %s for %s: %s",
                         resp.status_code, name, resp.text[:200])
    except Exception as e:
        logger.debug("generate-notes failed for %s: %s", name, e)

    return None


def _compute_proposed_version(lib_version, bump_type):
    """Compute a proposed version string from lib_version and bump_type.

    Returns a version string or None if lib_version can't be parsed.
    """
    if not lib_version:
        return None
    raw = lib_version.strip().lstrip("v")
    try:
        sv = semver.VersionInfo.parse(raw)
    except ValueError:
        return None

    if bump_type == "prerelease" and (sv.prerelease or sv.build):
        # Increment prerelease: beta.3 → beta.4
        pre = sv.prerelease or ""
        m = re.search(r'(\d+)(?=\D*$)', pre)
        if m:
            new_pre = pre[:m.start()] + str(int(m.group(1)) + 1) + pre[m.end():]
        else:
            new_pre = pre + ".1"
        return str(sv.replace(prerelease=new_pre, build=None))
    elif bump_type == "major":
        return str(sv.bump_major())
    elif bump_type == "minor":
        return str(sv.bump_minor())
    else:
        return str(sv.bump_patch())


# ---------------------------------------------------------------------------
# Commit classification helpers
# ---------------------------------------------------------------------------

_NOISE_PREFIXES = (
    "merge pull request", "merge branch", "merge remote",
    "bump version",
    "update github", "update ci ", "add .github", "update .github",
    "fix github", "ci:", "docs:", "chore:", "style:",
)
_NOISE_KEYWORDS = (
    ".github/workflows", "githubci", "pre-commit", "pylint",
    "copyright", "spdx", "license header",
)


def _is_noise(msg):
    """Return True if a commit message is CI/merge/admin noise."""
    ml = msg.lower().strip()
    if any(ml.startswith(p) for p in _NOISE_PREFIXES):
        return True
    if any(k in ml for k in _NOISE_KEYWORDS):
        return True
    return False


def _classify_commit(msg):
    """Classify a commit as breaking / feature / fix / other."""
    ml = msg.lower()
    if any(w in ml for w in ("breaking change", "remove support", "drop support",
                              "incompatible", "api break", "breaking:")):
        return "breaking"
    if any(w in ml for w in ("add ", "new ", "feat", "implement ", "support for",
                              "introduce ", "enable ")):
        return "feature"
    if any(w in ml for w in ("fix", "bug ", "correct", "repair", "resolve", "issue #")):
        return "fix"
    return "other"


def _suggest_bump_type(commits, current_version=None):
    """Heuristic fallback: analyse commit messages to suggest bump type.

    When current_version has a prerelease identifier (e.g. 1.0.0-beta.3),
    defaults to "prerelease" (increment the prerelease number) unless the
    commits contain breaking changes or major new features that warrant
    promoting to a stable release.
    """
    messages = [
        c.get("message", "").split("\n")[0].lower()
        for c in commits
        if not _is_noise(c.get("message", ""))
    ]
    breaking_kws = [
        "breaking change", "remove support", "drop support",
        "incompatible", "api break", "breaking:",
    ]
    feature_kws = [
        "add ", "new ", "feat", "implement ", "support for", "introduce ", "enable ",
    ]

    # Detect prerelease in current version
    has_prerelease = False
    if current_version:
        try:
            sv = semver.VersionInfo.parse(current_version.strip().lstrip("v"))
            has_prerelease = bool(sv.prerelease or sv.build)
        except ValueError:
            pass

    for msg in messages:
        if any(kw in msg for kw in breaking_kws):
            return "major", f"Breaking change: \"{msg[:60]}\""

    if has_prerelease:
        # Stay in prerelease unless there are strong signals to do a stable bump
        for msg in messages:
            if any(kw in msg for kw in feature_kws):
                return "prerelease", f"New feature during prerelease: \"{msg[:60]}\""
        return "prerelease", "Continuing prerelease cycle"

    for msg in messages:
        if any(kw in msg for kw in feature_kws):
            return "minor", f"New feature: \"{msg[:60]}\""
    return "patch", "Fixes, cleanup, or CI-only changes"


def _llm_analyse_changes(commits, lib_name, since_tag, current_version,
                         example_releases=None, gh_auto_notes=None,
                         compare_files=None):
    """
    Call claude CLI to analyse commits and return (bump_type, justification, release_notes).
    Returns (None, None, None) on failure — caller should fall back to heuristic.

    Parameters:
        gh_auto_notes: GitHub's auto-generated release notes (string) for reference
        compare_files: list of {filename, status, patch} dicts for file-level context
    """
    clean = [
        c for c in commits
        if not _is_noise(c.get("message", ""))
    ]
    if not clean:
        return None, None, None

    commit_lines = []
    for c in clean[:35]:
        msg = c.get("message", "").split("\n")[0].strip()
        sha = c.get("sha", "")
        url = c.get("html_url", "")
        author = c.get("author", "")
        # Format: - {sha} {msg} by @{author} {url}
        parts = [f"- {sha} {msg}" if sha else f"- {msg}"]
        if author:
            parts.append(f"by @{author}")
        if url:
            parts.append(url)
        commit_lines.append(" ".join(parts))
    commit_list = "\n".join(commit_lines)

    # Detect whether current_version is a prerelease (e.g. 1.0.0-beta.3)
    is_prerelease = False
    try:
        _sv = semver.VersionInfo.parse(current_version.strip().lstrip("v"))
        is_prerelease = bool(_sv.prerelease or _sv.build)
    except (ValueError, AttributeError):
        pass

    if is_prerelease:
        bump_type_instructions = (
            f"  bump_type: one of \"prerelease\", \"patch\", \"minor\", or \"major\".\n"
            f"    The current version ({current_version}) is a prerelease. "
            f"Use \"prerelease\" to increment the prerelease identifier "
            f"(e.g. beta.3 → beta.4) — this is the default unless the commits "
            f"clearly indicate a stable or breaking release is warranted.\n"
            f"    Use \"patch\"/\"minor\"/\"major\" only when the commits justify "
            f"promoting to a stable release at that semver level."
        )
    else:
        bump_type_instructions = (
            f"  bump_type: \"patch\", \"minor\", or \"major\""
        )

    # -- TOP: Role + instructions + JSON output format --
    has_examples = bool(example_releases)
    if has_examples:
        notes_instructions = (
            "  release_notes: markdown release notes that CLOSELY MATCH the "
            "structure, headings, formatting, and tone of the style examples below. "
            "Preserve any recurring boilerplate sections (e.g. install instructions, "
            "upgrade notes) exactly as they appear in the examples, then add the "
            "changes from this release using the same heading style and bullet format. "
            "Include contributor mentions and PR links from the GitHub auto-generated "
            "notes when available. For each change, link the PR number or short commit "
            "SHA, e.g. ([`abc1234`](url)) or (#123). Do NOT just list every commit — "
            "group related commits into single bullet points with clear descriptions. "
            "Do NOT include a **Full Changelog** link — that is added automatically."
        )
    else:
        notes_instructions = (
            "  release_notes: markdown release notes, concise and human-readable. "
            "Group changes under ### What's Changed with bullet points. "
            "Each bullet should describe the change in user-friendly language "
            "(do NOT just copy commit messages verbatim — summarize and clarify). "
            "For each change, link the PR number or short commit SHA, e.g. "
            "([`abc1234`](url)) or (#123). Mention contributors with @username. "
            "No h1/h2 headers. Do NOT list every commit individually — "
            "group related commits into single bullet points. "
            "Do NOT echo the raw commit list. "
            "Do NOT include a **Full Changelog** link — that is added automatically."
        )

    prompt_parts = [
        f"You are preparing a release for the Arduino library '{lib_name}' "
        f"(current version: {current_version}, since tag: {since_tag}).\n\n"
        f"Return ONLY a JSON object (no markdown fences, no extra text) with exactly these keys:\n"
        f"{bump_type_instructions}\n"
        f"  bump_justification: 1-2 terse sentences — what changed and why that semver level\n"
        f"{notes_instructions}",
    ]

    # -- MIDDLE: GitHub auto-generated notes + style examples --
    if gh_auto_notes:
        prompt_parts.append(
            f"\nGitHub's auto-generated release notes for reference "
            f"(use contributor mentions and PR links from here):\n{gh_auto_notes[:3000]}"
        )

    if example_releases:
        examples = "\n\n".join(
            f"Release {r['tag']}:\n{r['body'][:600]}"
            for r in example_releases[:3]
        )
        prompt_parts.append(
            f"\nIMPORTANT — Style examples from this library's previous releases. "
            f"Your release_notes MUST follow this same structure and formatting:\n{examples}"
        )

    # -- BOTTOM: Commits + file diffs (most likely to be truncated) --
    prompt_parts.append(
        f"\nReference data — commits in this release (use for context, "
        f"do NOT copy this list into release_notes verbatim):\n{commit_list}"
    )

    if compare_files:
        diff_lines = []
        total_chars = 0
        for f in compare_files:
            patch = f.get("patch", "")
            if not patch:
                continue
            entry = f"--- {f['filename']} ({f.get('status', 'modified')})\n{patch}"
            if total_chars + len(entry) > 8000:
                diff_lines.append("(remaining diffs truncated)")
                break
            diff_lines.append(entry)
            total_chars += len(entry)
        if diff_lines:
            prompt_parts.append(
                "\nFile diffs (for context — may be truncated):\n"
                + "\n\n".join(diff_lines)
            )

    prompt = "\n".join(prompt_parts)

    try:
        # Strip CLAUDECODE so we can call claude from inside a Claude Code session
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--model", "claude-haiku-4-5-20251001",
                "--no-session-persistence",
                "--dangerously-skip-permissions",
            ],
            capture_output=True, text=True, timeout=60,
            env=env,
        )
        if result.returncode != 0:
            logger.warning("claude CLI non-zero for %s (rc=%d): %s",
                           lib_name, result.returncode, result.stderr[:300])
            return None, None, None

        output = (result.stdout or result.stderr or "").strip()
        # Strip markdown fences if the model wraps in ```json ... ```
        if output.startswith("```"):
            parts = output.split("```")
            output = parts[1].lstrip("json").strip() if len(parts) > 1 else output
        data = json.loads(output)
        return (
            data.get("bump_type"),
            data.get("bump_justification"),
            data.get("release_notes"),
        )
    except subprocess.TimeoutExpired:
        logger.debug("claude CLI timed out for %s", lib_name)
    except (json.JSONDecodeError, KeyError) as e:
        logger.debug("claude CLI bad JSON for %s: %s", lib_name, e)
    except Exception as e:
        logger.debug("claude CLI unexpected error for %s: %s", lib_name, e)
    return None, None, None


# ---------------------------------------------------------------------------
# Phase 2: detail enrichment
# ---------------------------------------------------------------------------

def _enrich_details(repo_data):
    """Fetch commits, PRs, version files and suggest bump type for one repo."""
    if repo_data.get("details_loaded") or repo_data.get("error"):
        return

    name = repo_data["name"]
    default_branch = repo_data.get("default_branch", "main")
    release_tag = repo_data.get("release_tag")

    repo = {
        "name": name,
        "full_name": repo_data.get("full_name", f"adafruit/{name}"),
        "html_url": repo_data.get("html_url", f"https://github.com/adafruit/{name}"),
        "default_branch": default_branch,
    }

    try:
        _fetch_prs(repo, repo_data)

        # 1. Fetch commits + diffs
        if release_tag:
            _fetch_commits(repo, repo_data, from_ref=release_tag, to_ref=default_branch)
        elif repo_data.get("compare_url"):
            _fetch_commits(repo, repo_data, from_ref=default_branch, to_ref="HEAD")

        if repo_data.get("lib_version"):
            repo_data["version_files"] = _find_version_files(repo, repo_data["lib_version"])

        # 2. Fetch recent releases (for style examples)
        _fetch_recent_releases(repo, repo_data)

        # Invalidate stale user-edited notes (tag or commit count changed)
        if repo_data.get("release_notes_user_edited") and not user_notes_current(repo_data):
            logger.info("%s: user-edited notes stale (stamp %s) — clearing",
                        name, repo_data.get("release_notes_user_edited"))
            repo_data["release_notes"] = None
            repo_data["release_notes_user_edited"] = None

        _VALID_BUMP_TYPES = {"patch", "minor", "major", "prerelease"}
        lib_version = repo_data.get("lib_version") or "0.0.0"
        commits = repo_data.get("recent_commits", [])
        if CATEGORY_NEEDS_RELEASE in repo_data.get("categories", []) and commits:
            # 3. Compute heuristic bump first (cheap, needed for proposed version)
            h_bump_type, h_justification = _suggest_bump_type(commits, lib_version)

            # 4. Compute proposed version: prefer bump_pr.new_version, then
            # check if lib_version is already ahead of release_tag (already bumped),
            # otherwise bump from the higher of lib_version/release_tag.
            bump_pr = repo_data.get("bump_pr")
            release_tag = repo_data.get("release_tag")
            if bump_pr and bump_pr.get("new_version"):
                proposed_version = bump_pr["new_version"]
            else:
                try:
                    lib_sv = semver.VersionInfo.parse(lib_version.strip().lstrip("v"))
                except (ValueError, AttributeError):
                    lib_sv = None
                try:
                    rel_sv = semver.VersionInfo.parse(
                        release_tag.strip().lstrip("v")) if release_tag else None
                except (ValueError, AttributeError):
                    rel_sv = None
                if lib_sv and rel_sv and lib_sv > rel_sv:
                    # lib_version already bumped past release_tag — use directly
                    proposed_version = str(lib_sv)
                else:
                    proposed_version = _compute_proposed_version(lib_version, h_bump_type)
            proposed_tag = proposed_version or lib_version

            # 5. Fetch GitHub's auto-generated release notes
            _fetch_gh_auto_notes(repo, repo_data, proposed_tag)

            # 6. LLM analysis with all data
            bump_type, justification, llm_notes = _llm_analyse_changes(
                commits,
                name,
                repo_data.get("release_tag") or "initial",
                lib_version,
                example_releases=repo_data.get("example_releases", []),
                gh_auto_notes=repo_data.get("gh_auto_notes"),
                compare_files=repo_data.get("compare_files", []),
            )
            # 7. Use LLM result or fall back to heuristic
            if bump_type and bump_type in _VALID_BUMP_TYPES:
                repo_data["bump_type"] = bump_type
                repo_data["bump_justification"] = justification
            else:
                repo_data["bump_type"] = h_bump_type
                repo_data["bump_justification"] = h_justification
            # Store LLM notes only if the user hasn't manually edited them
            if llm_notes and not repo_data.get("release_notes_user_edited"):
                if not repo_data.get("release_notes"):
                    repo_data["release_notes"] = llm_notes
        else:
            bump_type, justification = _suggest_bump_type(
                repo_data.get("recent_commits", []), lib_version
            )
            repo_data["bump_type"] = bump_type
            repo_data["bump_justification"] = justification

    except Exception as e:
        logger.debug("Detail enrichment failed for %s: %s", name, e)

    repo_data["details_loaded"] = True


# ---------------------------------------------------------------------------
# Release notes generation
# ---------------------------------------------------------------------------

def generate_release_notes(repo_data):
    """
    Heuristic fallback release notes from merged commits only.
    Open PRs are NOT used — they haven't been merged and aren't part of the release.
    """
    commits = repo_data.get("recent_commits", [])
    tag = repo_data.get("release_tag") or "last release"

    groups = {"breaking": [], "feature": [], "fix": [], "other": []}
    seen = set()
    for c in commits[:40]:
        msg = c.get("message", "").strip().split("\n")[0]
        if not msg or _is_noise(msg) or msg in seen:
            continue
        seen.add(msg)
        short = msg[:80] + ("\u2026" if len(msg) > 80 else "")
        groups[_classify_commit(msg)].append(f"* {short}")

    caps = [
        ("Breaking Changes", "breaking", 20),
        ("New Features",     "feature",  10),
        ("Bug Fixes",        "fix",      10),
        ("Changed",          "other",     8),
    ]
    lines = [f"## Changes since {tag}\n"]
    for label, key, cap in caps:
        items = groups[key][:cap]
        if items:
            lines.append(f"### {label}")
            lines.extend(items)
            lines.append("")

    if len(lines) == 1:
        lines.append(f"* Changes since {tag}")

    return "\n".join(lines).rstrip()
