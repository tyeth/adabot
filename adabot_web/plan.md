# Adabot Web App — Implementation Plan

> **Rule:** You must never release anything, ever. That's the user's job during testing / actual usage.

---

## Running the Server

The server requires a GitHub token (`repo`, `workflow`, `read:org` scopes).
Two approaches:

1. **`run_web.py` env defaults** — `run_web.py` calls
   `os.environ.setdefault("ADABOT_GITHUB_USER", "tyeth")` and has a commented-out
   `ADABOT_GITHUB_ACCESS_TOKEN` fallback. Uncomment and set it for a persistent
   default, or pass the variable on the command line.

2. **One-liner (recommended)** — from point 7 of the README Purpose section.
   This safely kills only the `run_web.py` process on port 8080, grabs a fresh
   token from the `gh` CLI, and starts the server:

   ```bash
   kill $(ps aux | grep '[r]un_web.py --port 8080' | awk '{print $2}') 2>/dev/null; \
     ADABOT_GITHUB_ACCESS_TOKEN=$(gh auth token) .venv/bin/python run_web.py --port 8080
   ```

   Check which GitHub account is active with `gh auth status`.

---

## TODO 1: Release CI Tracking

**Problem:** After a release is created, GitHub runs CI workflows (triggered by the
`release` event). The UI has no visibility into these runs — the pipeline banner stops
at "Released" and the CI tab / Reload CI button only check bump-PR or branch CI.

### Current state
- Pipeline banner (`repo_detail.html:6-66`) has 5 steps:
  Bump PR → PR CI → Merged → Branch CI → Release
- `check_ci()` (`app.py:812-902`) only handles two cases:
  1. Open PR → `gh pr checks`
  2. Merged PR → `_check_branch_ci()` on default branch (filters to post-merge runs)
- Neither path looks for workflow runs triggered by the `release` event.
- `_CI_TRIGGER_EVENTS` in `collector.py:462` already includes `"release"` in its set,
  but nothing in `check_ci()` uses it.

### Plan
1. **Add `release_ci_status` + `release_ci_checks` fields** to the repo state.
2. **New helper `_check_release_ci(upstream, tag)`** in `app.py`:
   - Call `gh api repos/{upstream}/actions/runs` filtered by `event=release`
     and `head_sha` or `created` after release time.
   - Or simpler: `gh run list --repo {upstream} --event release --limit 5 --json ...`
     then filter to runs whose `headBranch` matches the tag.
   - Conclude status with existing `_conclude_checks()`.
3. **Extend `check_ci()` route**: After a release (`release_status == "released"`),
   call `_check_release_ci()` and return `source: "release"` with the runs.
4. **Add step 6 to the pipeline banner**: "Release CI" after "Released".
   - `s6 = 'done'` if `release_ci_status == 'pass'`, `'fail'` if failed, `'active'`
     if pending, `'todo'` if not yet released.
5. **Update sidebar badge** (`index.html:266-300`): show release CI status
   (spinner/check/cross) when `release_status == 'released'`.
6. **CI tab rendering**: When source is `"release"`, render the release CI check
   rows the same way branch CI checks are shown.

### Files to change
- `app.py`: new `_check_release_ci()`, extend `check_ci()` route (~lines 812-902)
- `repo_detail.html`: pipeline banner macro (~lines 6-66), CI tab pane
- `index.html`: `updateSidebarBadge()` (~lines 266-300)
- `collector.py`: no changes needed (state is just dicts)

---

## TODO 2: Post-Release State Cleanup

**Problem:** After releasing, the repo stays visually marked as "ready" in the sidebar
and overview counts are stale. The `release_status` is set to `"released"` but the
sidebar item keeps its `.ready` CSS class, and no overview refresh happens.

### Current state
- `create_release()` (`app.py:1144-1149`) sets `release_status = "released"` and saves
  state, but the frontend `doRelease()` (`index.html:474-492`) doesn't update the
  local `repoData` or DOM classes after a successful release.
- `batch_release()` (`app.py:1154-1202`) has the same backend behavior; the frontend
  `batchRelease()` (`index.html:526-544`) similarly lacks post-release DOM updates.
- Sidebar item classes are set on initial render (`index.html:78`) using Jinja
  (`{% if repo.release_status == 'ready' %} ready{% endif %}`), but not updated
  client-side after release.
- There is no overview panel / summary stats that refresh — the counts in the sidebar
  tabs (`index.html:53-69`) are Jinja-rendered at page load only.

### Plan
1. **Frontend: `doRelease()` success handler** (`index.html:~480`):
   - Update `repoData[name].release_status = 'released'`
   - Remove `.ready` class, add `.released` class on the sidebar `.repo-item`
   - Call `updateSidebarBadge(name)` to refresh badge
   - Optionally reload the repo detail pane to get fresh pipeline banner
2. **Frontend: `batchRelease()` success handler** (`index.html:~530`):
   - For each successful result, do the same DOM + repoData updates
   - Show summary toast with released count
3. **Backend: `create_release()`** — also clear `release_status` artifacts:
   - Already done (`app.py:1144-1149`), no backend changes needed.
4. **Sidebar tab counts**: Add a JS function `refreshSidebarCounts()` that fetches
   updated counts from a new lightweight endpoint (or recompute from `repoData`).
   Call it after release and batch release.

### Files to change
- `index.html`: `doRelease()` success path, `batchRelease()` success path,
  new `refreshSidebarCounts()` function
- `app.py`: possibly a `/api/counts` endpoint if client-side recount isn't feasible

---

## TODO 3: Release Changelog Markdown File

**Problem:** The user needs a daily changelog file listing all releases for reporting
purposes. Code exists (`_append_release_log()` at `app.py:1080-1101`) that writes to
`releases/YYYY-MM-DD.md`, but the file wasn't noticed in `git status` during testing.

### Current state
- `_RELEASES_DIR` = `os.path.join(os.path.dirname(__file__), "..", "releases")`
  → resolves to `<project_root>/releases/`
- `_append_release_log()` is called in both `create_release()` (line 1150) and
  `batch_release()` (line 1185) on success.
- The function creates the directory, writes a markdown header if the file is new,
  and appends a timestamped entry per release.
- **Possible issue:** `_RELEASES_DIR` resolves relative to `app.py` (which is in
  `adabot_web/`), so `..` goes to the project root — this is correct.

### Investigation needed
- Verify the file is actually being created by doing a test release and checking
  `ls -la releases/`. If the directory exists but is in `.gitignore`, that explains
  why `git status` doesn't show it.
- Check `.gitignore` for `releases/` pattern.

### Plan
1. **Verify path**: Confirm `_RELEASES_DIR` resolves correctly at runtime by
   adding a startup log line or checking after a test release.
2. **Check `.gitignore`**: If `releases/` is gitignored, either remove the ignore
   (so changelogs are tracked) or document that files are in `releases/` but
   untracked. The user likely wants them tracked.
3. **If the code works but files are ignored**: Remove `releases/` from `.gitignore`
   and add existing files.
4. **If the code doesn't work**: Debug path resolution — `__file__` in `app.py`
   may resolve differently depending on how the server is started (e.g., from
   project root via `run_web.py` vs directly). Fix to use an absolute path.
5. **Enhance the log format** if needed — current format looks good:
   ```markdown
   # Arduino Library Releases — 2026-03-01

   - **14:35** `Adafruit_BusIO` 1.16.1 → 1.16.2 — Fix I2C scan — [1.16.2](url)
   ```

### Files to change
- `.gitignore`: check/update
- `app.py`: possibly fix `_RELEASES_DIR` path resolution if broken

---

## TODO 4: Sort `web_state.json` Keys

**Problem:** The JSON keys in `web_state.json` are in insertion order, making diffs
hard to read when reviewing changes.

### Current state
- `save_state()` (`collector.py:69-74`) uses `json.dump(state, f, indent=2)` with
  no `sort_keys` parameter.

### Plan
1. **Add `sort_keys=True`** to `json.dump()` in `save_state()`:
   ```python
   json.dump(state, f, indent=2, sort_keys=True)
   ```
2. **One-time rewrite**: After deploying, the next save will reorder all keys.
   This will create a large diff in `web_state.json` once, then subsequent diffs
   will be clean and aligned.
3. **Note**: `sort_keys=True` sorts recursively through all nested dicts, which is
   exactly what's wanted.

### Files to change
- `collector.py`: line 73, add `sort_keys=True`

---

## Implementation Order

Recommended sequence (least to most complex, with quick wins first):

1. **TODO 4** — Sort `web_state.json` keys (1 line change)
2. **TODO 3** — Verify/fix release changelog (investigation + possibly 1-2 line fix)
3. **TODO 2** — Post-release state cleanup (frontend JS changes, moderate)
4. **TODO 1** — Release CI tracking (new backend helper + frontend + pipeline, largest)

TODOs 2 and 1 are somewhat coupled — both involve post-release UI updates — so
implementing 2 first gives a foundation that 1 builds on.


Latest TODO: Its a new week, the old data is still present even after a refresh (bump PR info) and the arduino wippersnapper library has a version bump already merged as part of another PR, but the suggested prerelease version is wrong as as result. The Version Bump panel also doesnt show the file that includes a semver in the repo "No other version files detected. Search repo ↗"