# Release Pipeline Status Enhancement — Implementation Status

## ALL CODE CHANGES ARE COMPLETE — needs Playwright verification only

## Changes Made

### 1. Fix: proposed_version locked to bump_pr.new_version (DONE)
**File: `adabot_web/app.py`** ~line 299-302
After normal `proposed_version` calc, added override:
```python
bump = repo.get("bump_pr")
if bump and bump.get("new_version"):
    proposed_version = bump["new_version"]
```

### 2. Pipeline CSS (DONE)
**File: `adabot_web/templates/base.html`** — after `.empty` rule
Added: `.pipeline-banner`, `.pipeline-step`, `.step-done` (green), `.step-active` (yellow+pulse), `.step-fail` (red), `.step-todo` (grey), `.pipeline-arrow`, `.pipeline-version-arrow`

### 3. Pipeline banner macro + per-tab banners (DONE)
**File: `adabot_web/templates/repo_detail.html`**
- `{% macro pipeline_banner() %}` at top, after `<script>` block
- Shows: `old → new  [Bump PR] → [PR CI] → [Merged] → [Branch CI] → [Release]`
- Color-coded steps from existing state data
- When no bump PR: "No bump PR" message
- Called `{{ pipeline_banner() }}` at top of all 4 tab panes
- Added green "Version already bumped to X.Y.Z" note on Bump tab when PR merged
- Added version display to CI tab's bump PR info line

### 4. Sidebar pending version badge (DONE)
**File: `adabot_web/templates/index.html`**
- Jinja: `→ X.Y.Z` badge with `id="ver-badge-{{ repo.name }}"` after CI badges
- JS: `updateSidebarBadge()` now manages `ver-badge-*` element dynamically

## Playwright MCP Config
- **Project-level config**: `/home/tyeth/dev-projects/python/adabot/.mcp.json`
- Updated `--executable-path` from `/usr/bin/chromium` to `/snap/bin/chromium`
- Also updated 3 files under `~/.claude/plugins/` (cache + marketplace copies)

## Verification Checklist (Playwright)
1. Navigate to `http://localhost:8080`
2. Click Adafruit_TCS3430 (merged bump PR for 1.1.0):
   - All 4 tabs: pipeline banner with `1.0.0 → 1.1.0`
   - Steps green up to "Branch CI"
   - Release Notes tag input = `1.1.0` (NOT `2.0.0`)
   - Version Bump diff = `version= 1.1.0` (NOT `2.0.0`)
   - Bump tab: green "Version already bumped" note
   - Sidebar: `→ 1.1.0` badge
3. Repo without bump PR → "No bump PR" pipeline
4. Sidebar version badges for all repos with bump PRs

## Server
- Run: `.venv/bin/python run_web.py --port 8080`
- May need restart to pick up app.py changes
- Check: `curl -s http://localhost:8080/`

## Branch
`arduino-ci-ver-bump-the-claude-years`
