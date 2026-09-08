# SPDX-FileCopyrightText: 2026 Adabot Contributors
# SPDX-License-Identifier: MIT
"""Flask web app for Adabot Arduino library release management."""
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse

from flask import Flask, jsonify, render_template, request

from adabot_web import collector
from adabot_web.collector import (
    is_stale, data_age_days,
    CATEGORY_NEEDS_RELEASE,
    CATEGORY_FAILED_LIB_PROP,
    CATEGORY_NEEDS_REGISTRATION,
    CATEGORY_MISSING_ACTIONS,
    CATEGORY_MISSING_LIB_PROPS,
    CATEGORY_NO_EXAMPLES,
    CATEGORY_NO_RELEASE_TAG,
    generate_release_notes,
)

import semver as _sv

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET", "adabot-dev-secret")


# ---------------------------------------------------------------------------
# Semver helpers
# ---------------------------------------------------------------------------

def _coerce_version(v):
    """Parse a version string leniently, returning a VersionInfo or None.

    Handles:
    - Strict semver:          "1.2.3", "1.0.0-beta.122", "1.0.0-rc1+build"
    - Leading 'v':            "v1.2.3"
    - Two-part versions:      "1.0"  → treated as "1.0.0"
    - Non-standard prerelease:"1.0.0b1", "1.0.0-beta1" — strips suffix, uses base
    """
    if not v or str(v).lower() in ("none", ""):
        return None
    v = str(v).strip().lstrip("v")
    # Try strict parse first (handles valid prerelease like "1.0.0-beta.122")
    try:
        return _sv.VersionInfo.parse(v)
    except ValueError:
        pass
    # Two-part version: "1.0" → "1.0.0"
    parts = v.split("-", 1)
    base, rest = parts[0], ("-" + parts[1]) if len(parts) > 1 else ""
    if base.count(".") == 1:
        try:
            return _sv.VersionInfo.parse(f"{base}.0{rest}")
        except ValueError:
            pass
    # Non-standard suffix: grab leading x.y.z digits only
    m = re.match(r'^(\d+\.\d+\.\d+)', v)
    if m:
        try:
            return _sv.VersionInfo.parse(m.group(1))
        except ValueError:
            pass
    return None


def _bump_prerelease_str(pre):
    """Increment the last numeric run in a prerelease string.

    "beta.122" → "beta.123"
    "rc1"      → "rc2"
    "alpha"    → "alpha.1"
    """
    m = re.search(r'(\d+)(?=\D*$)', pre)
    if m:
        return pre[:m.start()] + str(int(m.group(1)) + 1) + pre[m.end():]
    return pre + ".1"


def _bump_version(base_sv, bump_type):
    """Bump a VersionInfo, handling pre-release versions correctly.

    For a pre-release version like 1.0.0-beta.122:
    - prerelease → 1.0.0-beta.123  (default: keep the prerelease, increment it)
    - patch      → 1.0.1           (stable bump from the base numeric version)
    - minor      → 1.1.0
    - major      → 2.0.0

    For a normal version:
    - patch → x.y.(z+1)
    - minor → x.(y+1).0
    - major → (x+1).0.0
    """
    if bump_type == "prerelease" and (base_sv.prerelease or base_sv.build):
        new_pre = _bump_prerelease_str(base_sv.prerelease or "")
        return base_sv.replace(prerelease=new_pre, build=None)

    # For patch/minor/major on a prerelease, strip to stable base first
    if base_sv.prerelease or base_sv.build:
        base_sv = base_sv.replace(prerelease=None, build=None)

    if bump_type == "major":
        return base_sv.bump_major()
    elif bump_type == "minor":
        return base_sv.bump_minor()
    else:
        return base_sv.bump_patch()


def _ensure_changelog_link(notes, repo, proposed_version=None):
    """Strip any existing Full Changelog link and re-append a current one.

    Returns the (possibly updated) notes string.
    """
    if not notes:
        return notes
    name = repo.get("name", "")
    release_tag = (repo.get("release_tag") or "").strip()
    # Compute proposed_version from bump_type if not provided
    if not proposed_version:
        lib_version = repo.get("lib_version")
        bump_type = repo.get("bump_type") or "patch"
        if lib_version:
            base_sv = _coerce_version(release_tag) if release_tag else None
            lib_sv = _coerce_version(lib_version)
            sv = max(base_sv, lib_sv) if base_sv and lib_sv else (lib_sv or base_sv)
            if sv:
                proposed_version = str(_bump_version(sv, bump_type))
    # Strip any existing changelog line
    notes = re.sub(r'\n*\*\*Full Changelog\*\*:.*$', '', notes, flags=re.MULTILINE).rstrip()
    if proposed_version and release_tag and release_tag.lower() not in ("none", ""):
        html_url = repo.get("html_url") or f"https://github.com/adafruit/{name}"
        notes += f"\n\n**Full Changelog**: {html_url}/compare/{release_tag}...{proposed_version}"
    return notes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sorted_repos(state, category=None):
    repos = list(state.get("repos", {}).values())
    if category:
        repos = [r for r in repos if category in r.get("categories", [])]

    def sort_key(r):
        needs = CATEGORY_NEEDS_RELEASE in r.get("categories", [])
        behind = r.get("commits_behind") or 0
        if isinstance(behind, str):
            # strip "*CI/Md/IMG*" suffix
            try:
                behind = int(re.sub(r"\s.*", "", str(behind)))
            except ValueError:
                behind = 0
        return (not needs, -behind, r["name"])

    return sorted(repos, key=sort_key)


def _category_counts(state):
    counts = {c: 0 for c in (
        CATEGORY_NEEDS_RELEASE, CATEGORY_FAILED_LIB_PROP,
        CATEGORY_NEEDS_REGISTRATION, CATEGORY_MISSING_ACTIONS,
        CATEGORY_MISSING_LIB_PROPS, CATEGORY_NO_EXAMPLES, CATEGORY_NO_RELEASE_TAG,
    )}
    for repo in state.get("repos", {}).values():
        for cat in repo.get("categories", []):
            if cat in counts:
                counts[cat] += 1
    return counts


def _compute_proposed_version(repo, existing_tags=None):
    """Compute the proposed new release tag for a repo.

    Returns (proposed_version, bump_type, bump_justification, has_prerelease).
    Used by both single-release detail view and batch release.
    """
    existing_tags = set(existing_tags or repo.get("existing_tags") or [])
    bump_type = repo.get("bump_type") or "patch"
    bump_justification = repo.get("bump_justification")
    lib_version = repo.get("lib_version")
    release_tag = repo.get("release_tag")
    has_prerelease = False
    proposed_version = None

    if lib_version:
        lib_sv = _coerce_version(lib_version)
        if lib_sv:
            rel_sv = _coerce_version(release_tag) if release_tag else None
            if rel_sv and lib_sv > rel_sv:
                proposed_version = lib_version
                has_prerelease = bool(lib_sv.prerelease or lib_sv.build)
                bump_type = "none"
                bump_justification = "library.properties already ahead of release tag"
            else:
                base_sv = max(lib_sv, rel_sv) if rel_sv else lib_sv
                has_prerelease = bool(base_sv.prerelease or base_sv.build)
                proposed_sv = _bump_version(base_sv, bump_type)
                while str(proposed_sv) in existing_tags:
                    proposed_sv = proposed_sv.bump_patch()
                proposed_version = str(proposed_sv)

    # Override with bump PR version if one exists
    bump = repo.get("bump_pr")
    if bump and bump.get("new_version"):
        proposed_version = bump["new_version"]

    return proposed_version, bump_type, bump_justification, has_prerelease


def _release_blocked(repo):
    """
    Return a string reason if releasing this repo should be blocked, else None.
    - Blocked if bump PR exists and CI failed.
    - Blocked if bump PR exists and CI is pending (not yet confirmed pass).
    - Blocked if bump PR is open (not yet merged).
    - NOT blocked if there is no bump PR (direct release path).
    """
    bump = repo.get("bump_pr")
    if not bump:
        return None
    ci       = (bump.get("ci_status") or "").lower()
    state    = (bump.get("state") or "open").lower()
    if ci == "fail":
        return "Bump PR CI failed — fix before releasing"
    if state == "open" and ci != "pass":
        return "Bump PR is open and CI hasn't passed yet"
    if state == "open" and ci == "pass":
        return "Bump PR CI passed — merge it first"
    # PR merged — check branch CI
    branch_ci = (repo.get("branch_ci_status") or "").lower()
    if branch_ci == "fail":
        return "Default branch CI failed after bump merge"
    if branch_ci == "pending":
        return "Waiting for default branch CI after bump merge"
    return None


def _merge_repo_fields(name, repo, *fields):
    """Re-load state fresh and update only the specified fields for a repo.

    This avoids a race where repo_detail() holds a stale state snapshot
    and overwrites fields (like release_status) that were updated by
    a concurrent request (e.g. POST /api/repo/<name>/status).
    """
    fresh = collector.load_state()
    fresh_repo = fresh.get("repos", {}).get(name)
    if fresh_repo is None:
        return
    for f in fields:
        if f in repo:
            fresh_repo[f] = repo[f]
    collector.save_state(fresh)


_DEFAULT_GH_USER  = "tyeth-ai-assisted"
_DEFAULT_GH_TOKEN = "MISSING_TOKEN"


def _gh_token():
    return os.environ.get("ADABOT_GITHUB_ACCESS_TOKEN") or _DEFAULT_GH_TOKEN


def _gh_user():
    return os.environ.get("ADABOT_GITHUB_USER") or _DEFAULT_GH_USER


def _gh(*args, **kwargs):
    """Run a gh CLI command using ADABOT_GITHUB_ACCESS_TOKEN. Returns (ok, stdout, stderr)."""
    env = os.environ.copy()
    env["GH_TOKEN"] = _gh_token()
    result = subprocess.run(
        ["gh"] + list(args),
        capture_output=True, text=True,
        env=env,
        **kwargs
    )
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    state = collector.load_state()
    category = request.args.get("cat", CATEGORY_NEEDS_RELEASE)
    repos = _sorted_repos(state, category if category != "all" else None)
    counts = _category_counts(state)
    age = data_age_days(state)
    return render_template(
        "index.html",
        repos=repos,
        state=state,
        counts=counts,
        active_cat=category,
        data_stale=is_stale(state),
        data_age_days=round(age, 1) if age is not None else None,
        CATEGORY_NEEDS_RELEASE=CATEGORY_NEEDS_RELEASE,
        CATEGORY_FAILED_LIB_PROP=CATEGORY_FAILED_LIB_PROP,
        CATEGORY_NEEDS_REGISTRATION=CATEGORY_NEEDS_REGISTRATION,
        CATEGORY_MISSING_ACTIONS=CATEGORY_MISSING_ACTIONS,
        CATEGORY_MISSING_LIB_PROPS=CATEGORY_MISSING_LIB_PROPS,
        CATEGORY_NO_EXAMPLES=CATEGORY_NO_EXAMPLES,
        CATEGORY_NO_RELEASE_TAG=CATEGORY_NO_RELEASE_TAG,
    )


@app.route("/repo/<name>")
def repo_detail(name):
    state = collector.load_state()
    repo = state.get("repos", {}).get(name)
    if not repo:
        return "Repo not found", 404
    # User-edited notes are scoped to release_tag + commit count.
    # If either changed since the edit, the notes are stale and should be regenerated.
    if repo.get("release_notes_user_edited") and not collector.user_notes_current(repo):
        logger.info("%s: user-edited notes stale (stamp %s) — clearing",
                    name, repo.get("release_notes_user_edited"))
        repo["release_notes"] = None
        repo["release_notes_user_edited"] = None
        _merge_repo_fields(name, repo, "release_notes", "release_notes_user_edited")

    # Signal to the template whether notes still need async generation
    needs_notes_gen = False
    if not repo.get("release_notes") and not repo.get("release_notes_user_edited"):
        commits = repo.get("recent_commits", [])
        if commits and collector.CATEGORY_NEEDS_RELEASE in repo.get("categories", []):
            needs_notes_gen = True  # frontend will trigger async generation
        else:
            repo["release_notes"] = generate_release_notes(repo)
            _merge_repo_fields(name, repo, "release_notes")
    blocked = _release_blocked(repo)

    # Fetch and cache existing release tags so we can avoid version conflicts.
    # Cache expires after 1 hour; also cleared after a successful release.
    tags_age = time.time() - (repo.get("existing_tags_at") or 0)
    if "existing_tags" not in repo or tags_age > 3600:
        ok, out, _ = _gh("release", "list", "--repo", f"adafruit/{name}",
                         "--limit", "50", "--json", "tagName")
        if ok and out:
            try:
                repo["existing_tags"] = [t["tagName"] for t in json.loads(out)]
            except (json.JSONDecodeError, KeyError):
                repo["existing_tags"] = []
        else:
            repo["existing_tags"] = []
        repo["existing_tags_at"] = time.time()
        # Persist — re-load state to avoid clobbering concurrent status updates
        _merge_repo_fields(name, repo, "existing_tags", "existing_tags_at")

    existing_tags = set(repo.get("existing_tags") or [])

    proposed_version, bump_type, bump_justification, has_prerelease = \
        _compute_proposed_version(repo, existing_tags)

    # Keep the changelog link current (strip old one if present, then re-add)
    notes = _ensure_changelog_link(repo.get("release_notes") or "", repo, proposed_version)
    if notes != (repo.get("release_notes") or "").rstrip():
        repo["release_notes"] = notes
        _merge_repo_fields(name, repo, "release_notes")

    return render_template("repo_detail.html", repo=repo,
                           release_blocked=blocked,
                           proposed_version=proposed_version,
                           bump_type=bump_type,
                           bump_justification=bump_justification,
                           has_prerelease=has_prerelease,
                           needs_notes_gen=needs_notes_gen)


@app.route("/api/state")
def api_state():
    from adabot import github_requests as gh
    state = collector.load_state()
    age = data_age_days(state)
    result = {
        "status": state.get("status"),
        "progress": state.get("progress", 0),
        "total": state.get("total", 0),
        "last_run_start": state.get("last_run_start"),
        "last_run_end": state.get("last_run_end"),
        "repo_count": len(state.get("repos", {})),
        "stale": is_stale(state),
        "age_days": round(age, 1) if age is not None else None,
    }
    if state.get("error"):
        result["error"] = state["error"]
    if gh.rate_limit_remaining is not None:
        result["rate_limit_remaining"] = gh.rate_limit_remaining
        result["rate_limit_reset"] = gh.rate_limit_reset_at
    return jsonify(result)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    started = collector.start_collection(force=True)
    return jsonify({"started": started})


@app.route("/api/repos_with_bump_pr")
def repos_with_bump_pr():
    """Return names of repos that have a bump PR or are released (need CI tracking)."""
    state = collector.load_state()
    names = [
        name for name, r in state.get("repos", {}).items()
        if r.get("bump_pr") or (r.get("release_status") or "").lower() == "released"
    ]
    return jsonify({"repos": sorted(names)})


@app.route("/api/repos_ready_to_merge")
def repos_ready_to_merge():
    """Return names of repos with bump PRs that are open and CI has passed."""
    state = collector.load_state()
    names = [
        name for name, r in state.get("repos", {}).items()
        if (r.get("bump_pr") and
            (r["bump_pr"].get("ci_status") or "") == "pass" and
            (r["bump_pr"].get("state") or "open").lower() == "open")
    ]
    return jsonify({"repos": sorted(names)})


@app.route("/api/repos_ready_to_mark")
def repos_ready_to_mark():
    """Repos with merged bump PR + branch CI pass, not yet marked ready."""
    state = collector.load_state()
    names = [
        name for name, r in state.get("repos", {}).items()
        if (r.get("bump_pr") and
            (r["bump_pr"].get("state") or "").lower() == "merged" and
            (r["bump_pr"].get("ci_status") or "") == "pass" and
            r.get("branch_ci_status") == "pass" and
            r.get("release_status") not in ("ready", "skip", "released"))
    ]
    return jsonify({"repos": sorted(names)})


@app.route("/api/repo/<name>/refresh", methods=["POST"])
def refresh_repo(name):
    state = collector.load_state()
    if name not in state.get("repos", {}):
        return jsonify({"error": "not found"}), 404
    # Mark details_loaded=false synchronously so the poll sees it immediately
    state.get("repos", {})[name]["details_loaded"] = False
    collector.save_state(state)
    started = collector.refresh_single_repo(name)
    if not started:
        return jsonify({"error": "full collection is running — try again shortly"}), 409
    return jsonify({"started": True})


@app.route("/api/repo/<name>/status")
def repo_status(name):
    """Lightweight check used to poll single-repo refresh progress."""
    from adabot import github_requests as gh
    state = collector.load_state()
    repo = state.get("repos", {}).get(name)
    if not repo:
        return jsonify({"error": "not found"}), 404
    result = {"details_loaded": bool(repo.get("details_loaded"))}
    if gh.rate_limit_remaining is not None:
        result["rate_limit_remaining"] = gh.rate_limit_remaining
        result["rate_limit_reset"] = gh.rate_limit_reset_at
    return jsonify(result)


# ---------------------------------------------------------------------------
# Repo data actions
# ---------------------------------------------------------------------------

@app.route("/api/repo/<name>/release_notes", methods=["POST"])
def update_release_notes(name):
    state = collector.load_state()
    repo = state.get("repos", {}).get(name)
    if not repo:
        return jsonify({"error": "not found"}), 404
    notes = request.get_json().get("notes", "")
    # Stamp with the *proposed* (bumped) version + commit count so we can
    # detect staleness if the tag changes OR new commits land months later.
    # The proposed version is what the user sees in the tag input field.
    bump_type = repo.get("bump_type") or "patch"
    lib_version = repo.get("lib_version")
    release_tag = repo.get("release_tag")
    proposed = None
    if lib_version:
        base_sv = _coerce_version(release_tag) if release_tag else None
        lib_sv = _coerce_version(lib_version)
        sv = max(base_sv, lib_sv) if base_sv and lib_sv else (lib_sv or base_sv)
        if sv:
            proposed = str(_bump_version(sv, bump_type))
    bump = repo.get("bump_pr")
    if bump and bump.get("new_version"):
        proposed = bump["new_version"]
    stamp_tag = proposed or release_tag or ""
    n_commits = len(repo.get("recent_commits", []))
    merged = {
        "release_notes": notes,
        "release_notes_user_edited": f"{stamp_tag}@{n_commits}",
    }
    _merge_repo_fields(name, merged, "release_notes", "release_notes_user_edited")
    return jsonify({"ok": True})


@app.route("/api/repo/<name>/regenerate_notes", methods=["POST"])
def regenerate_notes(name):
    """Clear user-edit lock and release_notes so they will be regenerated on next load."""
    state = collector.load_state()
    repo = state.get("repos", {}).get(name)
    if not repo:
        return jsonify({"error": "not found"}), 404
    repo.pop("release_notes_user_edited", None)
    repo["release_notes"] = None
    collector.save_state(state)
    return jsonify({"ok": True})


@app.route("/api/repo/<name>/generate_notes", methods=["POST"])
def generate_notes_async(name):
    """Generate release notes via LLM (called async by the frontend).

    Returns JSON with the generated notes text, or an error.
    """
    state = collector.load_state()
    repo = state.get("repos", {}).get(name)
    if not repo:
        return jsonify({"error": "not found"}), 404

    # Don't regenerate if user-edited or already present
    if repo.get("release_notes_user_edited") and collector.user_notes_current(repo):
        return jsonify({"notes": repo.get("release_notes", ""), "source": "user_edited"})
    if repo.get("release_notes"):
        return jsonify({"notes": repo["release_notes"], "source": "cached"})

    from adabot_web.collector import (
        _llm_analyse_changes, _fetch_gh_auto_notes, _compute_proposed_version,
    )
    commits = repo.get("recent_commits", [])
    if not commits:
        notes = generate_release_notes(repo)
        repo["release_notes"] = notes
        _merge_repo_fields(name, repo, "release_notes")
        return jsonify({"notes": notes, "source": "heuristic"})

    # Compute proposed_version: prefer bump_pr.new_version, then check if
    # lib_version is already ahead of release_tag (i.e. already bumped),
    # otherwise bump from the higher of lib_version/release_tag.
    proposed_version = None
    bump = repo.get("bump_pr")
    if bump and bump.get("new_version"):
        proposed_version = bump["new_version"]
    else:
        lib_version = repo.get("lib_version")
        release_tag = repo.get("release_tag")
        bump_type = repo.get("bump_type") or "patch"
        if lib_version:
            lib_sv = _coerce_version(lib_version)
            rel_sv = _coerce_version(release_tag) if release_tag else None
            if lib_sv and rel_sv and lib_sv > rel_sv:
                # lib_version already bumped past release_tag — use it directly
                proposed_version = str(lib_sv)
            elif lib_sv:
                base_sv = max(lib_sv, rel_sv) if (lib_sv and rel_sv) else lib_sv
                proposed_version = str(_bump_version(base_sv, bump_type))

    # Lazily fetch GH auto-notes if missing
    if not repo.get("gh_auto_notes"):
        proposed_tag = proposed_version or repo.get("lib_version") or "0.0.0"
        lazy_repo = {"name": name, "default_branch": repo.get("default_branch", "main")}
        _fetch_gh_auto_notes(lazy_repo, repo, proposed_tag)
        if repo.get("gh_auto_notes"):
            _merge_repo_fields(name, repo, "gh_auto_notes")

    _, _, llm_notes = _llm_analyse_changes(
        commits,
        name,
        repo.get("release_tag") or "initial",
        repo.get("lib_version") or "0.0.0",
        example_releases=repo.get("example_releases", []),
        gh_auto_notes=repo.get("gh_auto_notes"),
        compare_files=repo.get("compare_files", []),
    )
    notes = llm_notes or generate_release_notes(repo)
    notes = _ensure_changelog_link(notes, repo, proposed_version)
    repo["release_notes"] = notes
    _merge_repo_fields(name, repo, "release_notes")
    return jsonify({"notes": notes, "source": "llm" if llm_notes else "heuristic"})


@app.route("/api/repo/<name>/status", methods=["POST"])
def update_repo_status(name):
    state = collector.load_state()
    repo = state.get("repos", {}).get(name)
    if not repo:
        return jsonify({"error": "not found"}), 404
    repo["release_status"] = request.get_json().get("status")
    collector.save_state(state)
    return jsonify({"ok": True, "status": repo["release_status"]})


# ---------------------------------------------------------------------------
# Version bump PR
# ---------------------------------------------------------------------------

@app.route("/api/repo/<name>/bump", methods=["POST"])
def bump_version(name):
    try:
        state = collector.load_state()
        repo = state.get("repos", {}).get(name)
        if not repo:
            return jsonify({"error": "not found"}), 404

        lib_version = repo.get("lib_version")
        release_tag = repo.get("release_tag")
        if not lib_version:
            return jsonify({"error": "no lib_version found"}), 400

        # Use caller-supplied target version if provided, else compute from bump_type
        data = request.get_json(silent=True) or {}
        new_version = data.get("target_version", "").strip()
        if not new_version:
            lib_sv = _coerce_version(lib_version)
            if not lib_sv:
                return jsonify({"error": f"Cannot parse lib version: {lib_version!r}"}), 400
            rel_sv = _coerce_version(release_tag) if release_tag and release_tag != "None" else None
            base_sv = max(lib_sv, rel_sv) if rel_sv else lib_sv
            bump_type = repo.get("bump_type") or "patch"
            new_version = str(_bump_version(base_sv, bump_type))

        default_branch = repo.get("default_branch") or "main"
        result = _gh_bump_pr(
            default_branch, lib_version, new_version, name,
            repo.get("version_files", [])
        )
        if result.get("error"):
            logger.error("bump PR failed for %s: %s", name, result["error"])
            return jsonify(result), 500

        # Re-load state fresh before writing — _gh_bump_pr can take 60+ seconds
        # and concurrent bumps for other repos may have saved state in the meantime.
        # Re-loading ensures we don't clobber their bump_pr entries.
        state = collector.load_state()
        repo = state.get("repos", {}).get(name)
        if repo is None:
            logger.error("Repo %s missing from state after bump PR creation — PR was created at %s",
                         name, result.get("url"))
            return jsonify({"error": "repo disappeared from state; PR was created", "url": result.get("url")}), 500
        repo["bump_pr"] = result
        collector.save_state(state)
        return jsonify(result)
    except Exception as e:
        logger.exception("Unhandled error in bump_version for %s", name)
        return jsonify({"error": str(e)}), 500


def _gh_bump_pr(default_branch, old_version, new_version, repo_name, version_files=None):
    """Create a version bump PR via fork, clone, edit, push, gh pr create."""
    import tempfile, shutil

    owner = _gh_user()
    fork_name = f"{owner}/adafruit-{repo_name}"
    branch = f"bump-version-{new_version}"
    upstream = f"adafruit/{repo_name}"

    # Ensure fork exists (create if missing; poll until GitHub confirms it's accessible)
    tok = _gh_token()
    tok_hint = f"{tok[:8]}…{tok[-4:]}" if len(tok) > 12 else "(short)"
    logger.info("Using GH user=%s token=%s", owner, tok_hint)
    ok, _, _ = _gh("repo", "view", fork_name, "--json", "name")
    if not ok:
        logger.info("Fork %s not found, creating…", fork_name)
        ok, _, err = _gh("repo", "fork", upstream, "--clone=false",
                         f"--fork-name=adafruit-{repo_name}")
        if not ok:
            logger.error("Fork failed (user=%s token=%s): %s", owner, tok_hint, err)
            return {"error": f"fork failed: {err}"}
        logger.info("Fork created: %s — waiting for GitHub to make it accessible…", fork_name)
        # Poll until the fork is actually resolvable (can take 10-30s on GitHub)
        for attempt in range(12):  # up to ~60s
            time.sleep(5)
            ok, _, _ = _gh("repo", "view", fork_name, "--json", "name")
            if ok:
                logger.info("Fork %s is accessible after %ds", fork_name, (attempt + 1) * 5)
                break
            logger.info("Fork not ready yet, waiting… (%d/12)", attempt + 1)
        else:
            return {"error": f"Fork created but not accessible after 60s: {fork_name}"}

    tmpdir = tempfile.mkdtemp(prefix="adabot-bump-")
    try:
        # Clone UPSTREAM directly via git (bypasses GitHub GraphQL lag on new forks).
        # We'll re-point origin at the fork before pushing.
        clone = subprocess.run(
            ["git", "clone", "--depth=1", f"--branch={default_branch}",
             f"https://github.com/{upstream}.git", tmpdir],
            capture_output=True, text=True,
        )
        if clone.returncode != 0:
            return {"error": f"clone upstream failed: {clone.stderr.strip()}"}

        # Point origin at our fork with token auth embedded in URL
        fork_push_url = f"https://{owner}:{_gh_token()}@github.com/{fork_name}.git"
        subprocess.run(["git", "-C", tmpdir, "remote", "set-url", "origin",
                        fork_push_url], capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "checkout", "-b", branch],
                       capture_output=True)

        # --- Update library.properties ---
        lib_props = os.path.join(tmpdir, "library.properties")
        if not os.path.exists(lib_props):
            return {"error": "library.properties not found in cloned repo"}

        with open(lib_props) as f:
            content = f.read()
        ver_match = re.search(r"^version\s*=\s*(.*)$", content, flags=re.MULTILINE)
        if ver_match is None:
            return {"error": "version= line not found in library.properties"}
        current_version = ver_match.group(1).strip()
        if current_version == new_version:
            return {"error": f"library.properties on {default_branch} is already at "
                             f"{new_version} — state is stale, re-collect this repo"}
        new_content = re.sub(
            r"^(version\s*=\s*).*$", rf"\g<1>{new_version}",
            content, flags=re.MULTILINE
        )
        with open(lib_props, "w") as f:
            f.write(new_content)

        files_changed = ["library.properties"]

        # --- Update other version files if they match old_version exactly ---
        ver_pattern = re.compile(
            r'((?:VERSION|version)["\']?\s*[=:"\s]\s*["\']?)' + re.escape(old_version) + r'(["\']?)',
        )
        for vf in (version_files or []):
            if not vf.get("matches"):
                continue  # only update files that currently match lib_version
            vf_path = os.path.join(tmpdir, vf["path"])
            if not os.path.exists(vf_path):
                continue
            with open(vf_path) as f:
                vc = f.read()
            new_vc = ver_pattern.sub(rf"\g<1>{new_version}\g<2>", vc)
            if new_vc != vc:
                with open(vf_path, "w") as f:
                    f.write(new_vc)
                files_changed.append(vf["path"])
                logger.info("Updated version in %s for %s", vf["path"], repo_name)

        # Commit and push
        subprocess.run(["git", "-C", tmpdir, "config", "user.email", "adabot@adafruit.com"],
                       capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Adabot"],
                       capture_output=True)
        for f in files_changed:
            subprocess.run(["git", "-C", tmpdir, "add", f], capture_output=True)

        extra_files_note = (
            f"\n\nAlso updated version in: {', '.join(files_changed[1:])}"
            if len(files_changed) > 1 else ""
        )
        commit = subprocess.run(
            ["git", "-C", tmpdir, "commit", "-m", f"Bump version to {new_version}"],
            capture_output=True, text=True
        )
        if commit.returncode != 0:
            return {"error": f"commit failed: {commit.stderr}"}

        push = subprocess.run(
            ["git", "-C", tmpdir, "push", "origin", branch, "--force"],
            capture_output=True, text=True
        )
        if push.returncode != 0:
            return {"error": f"push failed: {push.stderr}"}

        # Create PR
        body = (
            f"Bump library.properties version from `{old_version}` to `{new_version}`."
            f"{extra_files_note}\n\n"
            f"_This PR was created automatically by Adabot._"
        )
        ok, pr_url, err = _gh(
            "pr", "create",
            "--repo", upstream,
            "--head", f"{owner}:{branch}",
            "--base", default_branch,
            "--title", f"Update version to {new_version}",
            "--body", body,
        )
        already_existed = False
        if not ok:
            # Recover gracefully if the PR already exists (e.g. previous run created it
            # but state wasn't saved, or the user double-submitted).
            m = re.search(r'(https://github\.com/\S+/pull/(\d+))', err)
            if m and 'already exists' in err:
                already_existed = True
                pr_url = m.group(1)
                logger.error(
                    "bump_pr for %s: 'pr create' reported PR already exists — "
                    "recovering with existing PR %s. Full error: %s",
                    repo_name, pr_url, err,
                )
            else:
                return {"error": f"pr create failed: {err}"}

        pr_number = pr_url.rstrip("/").split("/")[-1]
        result = {
            "url": pr_url,
            "number": pr_number,
            "state": "open",
            "ci_status": "pending",
            "new_version": new_version,
            "files_changed": files_changed,
        }
        if already_existed:
            result["already_existed"] = True
        return result

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CI checking
# ---------------------------------------------------------------------------

@app.route("/api/repo/<name>/check_ci", methods=["POST"])
def check_ci(name):
    """Check CI on the bump PR. If PR is merged, check default branch instead."""
    state = collector.load_state()
    repo = state.get("repos", {}).get(name)
    if not repo:
        return jsonify({"error": "not found"}), 404

    bump = repo.get("bump_pr")
    if not bump or not bump.get("number"):
        # No bump PR — if the repo is released, still check release CI only.
        if (repo.get("release_status") or "").lower() == "released":
            upstream = f"adafruit/{name}"
            tag = repo.get("released_tag") or repo.get("release_tag") or ""
            if tag:
                release_ci = _check_release_ci(upstream, tag)
                repo["release_ci_status"] = release_ci.get("status", "unknown")
                repo["release_ci_checks"] = release_ci.get("runs", [])
                collector.save_state(state)
                return jsonify({
                    "ci_status": None,
                    "source": "release_only",
                    "release_ci_status": release_ci.get("status"),
                    "release_ci_checks": release_ci.get("runs", []),
                    "release_status": repo.get("release_status"),
                    "released_tag": repo.get("released_tag"),
                })
        return jsonify({"error": "no bump PR recorded"}), 400

    upstream = f"adafruit/{name}"

    # Check current PR state and grab head SHA in one call
    head_sha = None
    ok, pr_json, _ = _gh("pr", "view", str(bump["number"]),
                          "--repo", upstream,
                          "--json", "state,mergedAt,headRefName,headRefOid")
    if ok:
        try:
            pr_data = json.loads(pr_json)
            bump["state"] = pr_data.get("state", "open").lower()
            head_sha = pr_data.get("headRefOid")
        except (json.JSONDecodeError, AttributeError):
            pass

    if bump.get("state", "").lower() == "merged":
        default_branch = repo.get("default_branch") or "main"

        # Store mergedAt so we can filter branch runs to only post-merge ones
        merged_at = bump.get("merged_at")
        if not merged_at:
            try:
                merged_at = json.loads(pr_json).get("mergedAt") or ""
            except (json.JSONDecodeError, AttributeError, UnboundLocalError):
                merged_at = ""
            bump["merged_at"] = merged_at

        merge_result = _check_branch_ci(upstream, default_branch,
                                        label="merge CI", since=merged_at)
        overall = merge_result["status"]
        repo["branch_ci_status"] = overall
        bump["ci_status"] = overall
        bump["checks"] = merge_result.get("runs", [])

        # Also check release CI if the repo has been released
        release_ci = {}
        if (repo.get("release_status") or "").lower() == "released":
            tag = repo.get("released_tag") or repo.get("release_tag") or ""
            if tag:
                release_ci = _check_release_ci(upstream, tag)
                repo["release_ci_status"] = release_ci.get("status", "unknown")
                repo["release_ci_checks"] = release_ci.get("runs", [])

        collector.save_state(state)
        return jsonify({
            "ci_status": overall,
            "source": "branch",
            "checks": merge_result.get("runs", []),
            "release_ci_status": release_ci.get("status") if release_ci else None,
            "release_ci_checks": release_ci.get("runs", []) if release_ci else [],
            "release_status": repo.get("release_status"),
            "released_tag": repo.get("released_tag"),
        })

    # PR still open — check PR checks
    # gh pr checks --json fields: bucket,completedAt,description,event,link,name,startedAt,state,workflow
    # (no 'conclusion' field in this gh version; use 'state' directly)

    ok, out, checks_err = _gh("pr", "checks", str(bump["number"]),
                              "--repo", upstream, "--json", "name,state,bucket,link")
    # `gh pr checks` exits non-zero (e.g. "no checks reported on branch") when workflow
    # runs are blocked pending first-contributor approval — don't bail out here; fall
    # through so we can detect action_required runs and surface the right status.
    checks = []
    if ok:
        try:
            checks = json.loads(out)
        except json.JSONDecodeError:
            pass

    ci_status = _conclude_checks(checks)

    # Detect runs awaiting first-time contributor approval (action_required).
    # This MUST run even when gh pr checks failed, because that failure IS the symptom.
    needs_approval, approval_run_ids = _check_action_approval_needed(upstream, head_sha)
    bump["actions_need_approval"] = needs_approval
    bump["pending_approval_run_ids"] = approval_run_ids
    if needs_approval:
        ci_status = "approval_required"
    elif not ok and not checks:
        # gh pr checks failed and no approval-required runs found — surface the original error
        return jsonify({"error": checks_err}), 500

    bump["ci_status"] = ci_status
    bump["checks"] = checks
    collector.save_state(state)
    return jsonify({
        "ci_status": ci_status,
        "state": bump.get("state", "open"),
        "source": "pr",
        "checks": checks,
        "actions_need_approval": needs_approval,
        "release_status": repo.get("release_status"),
        "released_tag": repo.get("released_tag"),
    })


def _check_action_approval_needed(upstream, head_sha):
    """Return (needs_approval: bool, run_ids: list[int]) for runs awaiting first-contributor approval.

    GitHub blocks workflow runs from first-time contributors until a maintainer
    approves them.  These runs have status='action_required' via the Actions API.
    We query the REST API directly since `gh pr checks` doesn't surface this state.
    """
    if not head_sha:
        return False, []
    ok, out, _ = _gh("api",
                     f"/repos/{upstream}/actions/runs"
                     f"?head_sha={head_sha}&status=action_required&per_page=20")
    if not ok or not out:
        return False, []
    try:
        runs = json.loads(out).get("workflow_runs", [])
    except (json.JSONDecodeError, AttributeError):
        return False, []
    run_ids = [r["id"] for r in runs if isinstance(r, dict) and r.get("id")]
    return bool(run_ids), run_ids


def _check_branch_ci(upstream, branch, label="CI", since=None):
    """Fetch runs on a branch triggered at or after `since` (ISO timestamp).

    Expands each matching run into per-job rows.
    Each job row: name, state, url, run_id, run_label.
    """
    ok, out, _ = _gh("run", "list",
                     "--repo", upstream,
                     "--branch", branch,
                     "--limit", "10",
                     "--json", "status,conclusion,name,createdAt,updatedAt,url,databaseId")
    if not ok or not out:
        return {"status": "unknown", "runs": []}
    try:
        all_runs = json.loads(out)
    except json.JSONDecodeError:
        return {"status": "unknown", "runs": []}

    if not all_runs:
        return {"status": "unknown", "runs": []}

    # Filter to only runs created at or after the merge timestamp
    if since:
        runs = [r for r in all_runs if (r.get("createdAt") or "") >= since]
        if not runs:
            runs = all_runs[:1]  # fallback: at least show the most recent
    else:
        runs = all_runs[:1]

    # Overall status: pending beats everything; then fail; then pass
    statuses    = [(r.get("status") or "").lower() for r in runs]
    conclusions = [(r.get("conclusion") or "").lower() for r in runs]
    if any(s in ("in_progress", "queued", "waiting") for s in statuses):
        status = "pending"
    elif any(c in ("failure", "cancelled") for c in conclusions):
        status = "fail"
    elif conclusions and all(c == "success" for c in conclusions):
        status = "pass"
    else:
        status = "unknown"

    # Expand each matching run into individual job rows
    jobs = []
    for run in runs:
        run_id = run.get("databaseId", "")
        run_jobs_expanded = []
        if run_id:
            jok, jout, _ = _gh("run", "view", str(run_id),
                               "--repo", upstream, "--json", "jobs")
            if jok and jout:
                try:
                    run_jobs_expanded = json.loads(jout).get("jobs", [])
                except json.JSONDecodeError:
                    pass
        if run_jobs_expanded:
            for job in run_jobs_expanded:
                jobs.append({
                    "name":      job.get("name", ""),
                    "state":     (job.get("conclusion") or job.get("status") or "").upper(),
                    "url":       job.get("url", ""),
                    "run_id":    str(run_id),
                    "run_label": label,
                })
        else:
            # Fallback: single summary row for this run
            rc = (run.get("conclusion") or run.get("status") or "").upper()
            jobs.append({
                "name":      run.get("name", "Run"),
                "state":     rc,
                "url":       run.get("url", ""),
                "run_id":    str(run_id),
                "run_label": label,
            })

    return {"status": status, "runs": jobs}


def _check_release_ci(upstream, tag):
    """Fetch CI runs associated with a release tag.

    Tries two strategies:
    1. Runs triggered by the `release` event (repos with `on: release` workflows)
    2. Runs on the tag branch via push event (repos with `on: push` that trigger
       when a tag is created — this is the common Adafruit Arduino pattern)
    """
    all_runs = []

    # Strategy 1: release-event runs
    ok, out, _ = _gh("run", "list",
                      "--repo", upstream,
                      "--event", "release",
                      "--limit", "10",
                      "--json", "status,conclusion,name,createdAt,updatedAt,url,databaseId,headBranch")
    if ok and out:
        try:
            all_runs = json.loads(out)
        except json.JSONDecodeError:
            pass

    # Filter to runs matching the tag
    runs = [r for r in all_runs if r.get("headBranch") == tag]

    # Strategy 2: push-event runs on the tag branch (fallback)
    if not runs:
        ok2, out2, _ = _gh("run", "list",
                            "--repo", upstream,
                            "--branch", tag,
                            "--limit", "5",
                            "--json", "status,conclusion,name,createdAt,updatedAt,url,databaseId,headBranch,event")
        if ok2 and out2:
            try:
                tag_runs = json.loads(out2)
                # Only include push/release events on this tag, not PRs
                runs = [r for r in tag_runs
                        if r.get("event") in ("push", "release", "dynamic")]
            except json.JSONDecodeError:
                pass

    if not runs:
        return {"status": "unknown", "runs": []}

    statuses    = [(r.get("status") or "").lower() for r in runs]
    conclusions = [(r.get("conclusion") or "").lower() for r in runs]
    if any(s in ("in_progress", "queued", "waiting") for s in statuses):
        status = "pending"
    elif any(c in ("failure", "cancelled") for c in conclusions):
        status = "fail"
    elif conclusions and all(c == "success" for c in conclusions):
        status = "pass"
    else:
        status = "unknown"

    # Expand into per-job rows
    jobs = []
    for run in runs:
        run_id = run.get("databaseId", "")
        run_jobs_expanded = []
        if run_id:
            jok, jout, _ = _gh("run", "view", str(run_id),
                                "--repo", upstream, "--json", "jobs")
            if jok and jout:
                try:
                    run_jobs_expanded = json.loads(jout).get("jobs", [])
                except json.JSONDecodeError:
                    pass
        if run_jobs_expanded:
            for job in run_jobs_expanded:
                jobs.append({
                    "name":      job.get("name", ""),
                    "state":     (job.get("conclusion") or job.get("status") or "").upper(),
                    "url":       job.get("url", ""),
                    "run_id":    str(run_id),
                    "run_label": "release CI",
                })
        else:
            rc = (run.get("conclusion") or run.get("status") or "").upper()
            jobs.append({
                "name":      run.get("name", "Run"),
                "state":     rc,
                "url":       run.get("url", ""),
                "run_id":    str(run_id),
                "run_label": "release CI",
            })

    return {"status": status, "runs": jobs}


def _conclude_checks(checks):
    """Interpret gh pr checks 'state' field. gh returns mixed case: SUCCESS/FAILURE/etc."""
    states = [c.get("state", "").upper() for c in checks]
    if any(s in ("FAIL", "FAILURE", "ERROR", "CANCELLED") for s in states):
        return "fail"
    if states and all(s in ("PASS", "SUCCESS", "SKIPPED", "NEUTRAL") for s in states):
        return "pass"
    return "pending"


def _ci_status_label(status):
    """Human-readable label for ci_status values used in templates."""
    return {
        "pass": "pass",
        "fail": "fail",
        "pending": "pending",
        "approval_required": "approval required",
        "unknown": "unknown",
    }.get(status or "", status or "pending")


# ---------------------------------------------------------------------------
# Merge bump PR
# ---------------------------------------------------------------------------

@app.route("/api/repo/<name>/merge_bump_pr", methods=["POST"])
def merge_bump_pr(name):
    """Merge the bump PR once CI has passed."""
    state = collector.load_state()
    repo = state.get("repos", {}).get(name)
    if not repo:
        return jsonify({"error": "not found"}), 404

    bump = repo.get("bump_pr")
    if not bump or not bump.get("number"):
        return jsonify({"error": "no bump PR"}), 400

    if bump.get("ci_status") != "pass":
        return jsonify({"error": "CI has not passed yet"}), 400

    upstream = f"adafruit/{name}"
    ok, out, err = _gh("pr", "merge", str(bump["number"]),
                       "--repo", upstream,
                       "--merge",
                       "--delete-branch",
                       "--subject", f"Bump version to {bump.get('new_version', '')}")
    if not ok:
        return jsonify({"error": f"merge failed: {err}"}), 500

    bump["state"] = "merged"
    repo["branch_ci_status"] = "pending"
    collector.save_state(state)
    return jsonify({"ok": True, "merged": True})


# ---------------------------------------------------------------------------
# Release log
# ---------------------------------------------------------------------------

_RELEASES_DIR = os.path.join(os.path.dirname(__file__), "..", "releases")


def _micro_summary(notes):
    """Extract the first meaningful line from release notes as a one-liner."""
    for line in (notes or "").splitlines():
        line = line.strip()
        # Skip headings, blank lines, the changelog footer
        if not line or line.startswith("#") or line.startswith("**Full Changelog"):
            continue
        # Strip leading markdown list/bold markers
        line = re.sub(r"^[\*\-\•]+\s*", "", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        return line[:120]
    return ""


def _append_release_log(name, old_tag, new_tag, notes, url):
    """Append one entry to today's dated release log in releases/YYYY-MM-DD.md."""
    from datetime import date, datetime as dt
    os.makedirs(_RELEASES_DIR, exist_ok=True)
    log_path = os.path.join(_RELEASES_DIR, f"{date.today().isoformat()}.md")
    summary = _micro_summary(notes)
    timestamp = dt.now().strftime("%H:%M")
    old = (old_tag or "").strip()
    arrow = f"{old} → {new_tag}" if old and old.lower() != "none" else new_tag
    entry = (
        f"- **{timestamp}** `{name}` {arrow}"
        + (f" — {summary}" if summary else "")
        + (f" — [{new_tag}]({url})" if url else "")
        + "\n"
    )
    # Write header if file is new
    is_new = not os.path.exists(log_path)
    with open(log_path, "a", encoding="utf-8") as fh:
        if is_new:
            fh.write(f"# Arduino Library Releases — {date.today().isoformat()}\n\n")
        fh.write(entry)
    logger.info("Release logged: %s", entry.rstrip())


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------

@app.route("/api/repo/<name>/release", methods=["POST"])
def create_release(name):
    state = collector.load_state()
    repo = state.get("repos", {}).get(name)
    if not repo:
        return jsonify({"error": "not found"}), 404

    blocked = _release_blocked(repo)
    if blocked:
        return jsonify({"error": blocked, "blocked": True}), 400

    data = request.get_json()
    new_tag = data.get("tag")
    notes = data.get("notes") or repo.get("release_notes") or ""

    if not new_tag:
        return jsonify({"error": "tag required"}), 400

    upstream = f"adafruit/{name}"
    ok, out, err = _gh("release", "create", new_tag,
                       "--repo", upstream,
                       "--title", new_tag,
                       "--notes", notes)
    if not ok:
        manual_url = (
            "https://github.com/" + upstream + "/releases/new?" +
            urllib.parse.urlencode({"tag": new_tag, "title": new_tag, "body": notes})
        )
        logger.error("release create failed for %s: %s", name, err)
        return jsonify({
            "error": err,
            "manual_release_url": manual_url,
            "tag": new_tag,
            "notes": notes,
        }), 500

    repo["release_status"] = "released"
    repo["released_tag"] = new_tag
    repo.pop("existing_tags", None)
    repo.pop("existing_tags_at", None)
    repo.pop("release_notes_user_edited", None)
    collector.save_state(state)
    _append_release_log(name, repo.get("release_tag"), new_tag, notes, out.strip())
    return jsonify({"ok": True, "url": out})


@app.route("/api/batch_release", methods=["POST"])
def batch_release():
    """Release all repos marked ready (throttled, skips blocked)."""
    state = collector.load_state()
    ready = [r for r in state.get("repos", {}).values()
             if r.get("release_status") == "ready"]

    results = []
    for repo in ready:
        name = repo["name"]
        blocked = _release_blocked(repo)
        if blocked:
            results.append({"name": name, "ok": False, "error": blocked, "blocked": True})
            continue

        tag, _, _, _ = _compute_proposed_version(repo)
        if not tag:
            results.append({"name": name, "ok": False, "error": "no proposed version"})
            continue

        notes = repo.get("release_notes", "")
        ok, out, err = _gh("release", "create", tag,
                           "--repo", f"adafruit/{name}",
                           "--title", tag,
                           "--notes", notes)
        if ok:
            repo["release_status"] = "released"
            repo["released_tag"] = tag
            repo.pop("existing_tags", None)
            repo.pop("existing_tags_at", None)
            repo.pop("release_notes_user_edited", None)
            _append_release_log(name, repo.get("release_tag"), tag, notes, out.strip())
            results.append({"name": name, "ok": True, "url": out})
        else:
            manual_url = (
                "https://github.com/adafruit/" + name + "/releases/new?" +
                urllib.parse.urlencode({"tag": tag, "title": tag, "body": notes})
            )
            logger.error("batch release failed for %s: %s", name, err)
            results.append({
                "name": name, "ok": False, "error": err,
                "manual_release_url": manual_url,
                "tag": tag,
                "notes": notes,
            })

        collector.save_state(state)

    return jsonify({"results": results})


# ---------------------------------------------------------------------------
# Log viewer
# ---------------------------------------------------------------------------

LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "adabot_web.log")
LOG_FILE = os.path.abspath(LOG_FILE)


@app.route("/api/logs")
def api_logs():
    """Return the last N lines of the server log as JSON."""
    n = int(request.args.get("n", 200))
    try:
        with open(LOG_FILE, encoding="utf-8") as fh:
            lines = fh.readlines()
        tail = [l.rstrip("\n") for l in lines[-n:]]
    except FileNotFoundError:
        tail = ["(log file not found — restart server to create it)"]
    return jsonify({"lines": tail})
