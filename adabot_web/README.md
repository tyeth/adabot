# Adabot Arduino Release Manager — Web UI

A browser-based dashboard for managing Arduino library releases across Adafruit's GitHub repositories. Currently scoped to Arduino libraries only (repos tracked by `adabot/arduino_libraries.py`).

---

## Purpose

Adabot periodically audits hundreds of Adafruit Arduino libraries for release hygiene: version mismatches, missing CI, unreleased commits, etc. This web UI turns those audit results into an interactive release workflow, letting a maintainer:

1. Review repos that need a new release
2. Edit and save release notes (LLM-generated, styled after recent releases)
3. Create a version-bump PR (fork → branch → edit `library.properties` → push → PR)
4. Monitor CI on the bump PR and merge it when green
5. Watch branch CI after the merge
6. Create the GitHub release (or get a pre-filled manual URL if the token lacks permission)
7. This will kill and restart the server with gh creds (gh auth status to see who):
`kill $(ps aux | grep '[r]un_web.py --port 8080' | awk '{print $2}') 2>/dev/null; ADABOT_GITHUB_ACCESS_TOKEN=$(gh auth token) .venv/bin/python run_web.py --port 8080`
---

## Quick Start

```bash
# Show existing cached data (no network calls)
.env/bin/python run_web.py

# Fetch fresh data from GitHub (~20 min for all repos)
.env/bin/python run_web.py --collect

# Custom port
.env/bin/python run_web.py --port 8080
```

The server listens on `0.0.0.0:5000` by default. Open `http://localhost:5000` in a browser.

Safe restart (only kills the Flask listener, not any VS Code node connections):
```bash
kill $(lsof -i :5000 -sTCP:LISTEN -t) && sleep 1 && nohup .env/bin/python run_web.py --port 5000 &
```

---

## Configuration

Credentials are read from environment variables, with hardcoded fallbacks in `run_web.py`:

| Variable | Purpose |
|---|---|
| `ADABOT_GITHUB_USER` | GitHub username used for forking (default: `tyeth-ai-assisted`) |
| `ADABOT_GITHUB_ACCESS_TOKEN` | Classic PAT with `repo`, `workflow`, `read:org` scopes |

> **Note:** Fine-grained PATs cannot fork repositories in other organisations. A classic PAT is required for the bump-PR workflow.

---

## Architecture

```
run_web.py                  ← entry point; sets up logging, starts Flask
adabot_web/
  app.py                    ← Flask routes + action endpoints
  collector.py              ← background data collector; saves web_state.json
  templates/
    base.html               ← shared layout, log drawer, toast, confirm modal,
                               manual-release modal, keyboard handler
    index.html              ← sidebar repo list + detail panel shell + all JS
    repo_detail.html        ← per-repo tab content (rendered server-side,
                               injected via fetch into the detail panel)
web_state.json              ← persistent cache; survives restarts
adabot_web.log              ← server log (viewable via the Logs drawer in-app)
```

### Data flow

1. `collector.py` calls `arduino_libraries.py` → scans GitHub → saves `web_state.json`
2. Collection is two-phase: fast basic scan (`running_basic`) then per-repo detail enrichment (`running_details`)
3. Detail enrichment runs the LLM (`claude -p`) per repo to generate release notes, fetching the last 3 existing releases as style examples
4. Flask reads `web_state.json` on every request (no in-memory state)
5. Actions (bump PR, check CI, release) call `gh` CLI with `GH_TOKEN` injected

---

## UI Layout

```
┌─ Header ────────────────────────────────────────────────────────┐
│ Adabot Arduino Release Manager   [status]  [Refresh] [Batch ↗] │
├─ Progress bar ──────────────────────────────────────────────────┤
├─ Sidebar (320px) ──────┬─ Detail panel ──────────────────────── ┤
│ [category tabs]        │ [repo name]  [Mark Ready] [Skip]       │
│                        │ [Bump PR] [Check CI] [Release]         │
│  repo list             ├─ [Changes/PRs] [Notes] [Bump] [CI] ──  │
│  (scrollable)          │                                        │
│                        │  tab content                           │
│                        │                                        │
├─ keyboard hints ───────┴────────────────────────────────────────┤
└─────────────────────────────────────────────────────────────────┘
```

### Category tabs

| Tab | Description |
|---|---|
| Needs Release | Has commits since last release tag (primary workflow) |
| Version Mismatch | `library.properties` version ≠ latest release tag |
| Unregistered | Not in the Arduino library index |
| No CI | Missing `.github/workflows/githubci.yml` |
| No lib.props | Missing `library.properties` |
| All | Every scanned repo |

### Sidebar badges

Each repo item shows live status badges (updated after every action):

- `bump pending/pass/fail` — bump PR CI status (PR open)
- `merged ✓` + `CI:pending/pass/fail` — branch CI after bump merge
- `✓ ready` / `released` / `skip`

### Detail panel tabs

| Tab | Content |
|---|---|
| Changes / PRs | Open PRs, commits since last release, repo metadata |
| Release Notes | Editable textarea (LLM-generated), release tag input, Create Release button |
| Version Bump | Bump type selector (patch/minor/major), version preview, other version files |
| CI Status | Checks table (per-job rows, clickable → GitHub job page), merge/release banners |

---

## Release Workflow

```
1. Needs Release?
   └─ Check Changes tab — review commits
2. Edit release notes (Notes tab) → Save
3. Bump PR (Version Bump tab or header button)
   └─ Fork adafruit/Repo → branch bump-version-X.Y.Z
   └─ Edit library.properties (+ any matching version files)
   └─ Push → gh pr create → stored as bump_pr
4. Check CI (CI Status tab or header button)
   └─ gh pr checks → per-job rows, clickable links
   └─ Auto-merge when CI passes (or open PR on GitHub manually)
5. After merge: Check CI again
   └─ Fetches runs on default branch with createdAt ≥ mergedAt
   └─ Expands each run into individual job rows
6. Branch CI passes → Mark Ready
7. Release (Notes tab → Create Release button, or Batch Release)
   └─ gh release create tag --notes "..."
   └─ On permission failure: manual release modal with pre-filled GitHub URL
      and clipboard-copy of release notes
```

---

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `j` / `↓` | Next repo |
| `k` / `↑` | Previous repo |
| `Enter` | Switch to Changes tab |
| `m` | Mark repo as ready |
| `s` | Skip repo |
| `b` | Create bump PR |
| `R` (Shift+R) | Create release |
| `i` | Check CI |
| `1` / `2` / `3` / `4` | Switch to Changes / Notes / Bump / CI tab |
| `L` | Toggle server log drawer |

Hash-based navigation is supported: `http://localhost:5000/#RepoName/tabname` (e.g. `#Adafruit_IO_Arduino/ci`) restores the selected repo and active tab on page load or category switch.

---

## Bulk Release

**Batch Release Ready** (header button) releases all repos marked as ready in sequence. On permission failures, a sequential modal walks through each one:

- Edit notes in-place
- **Copy Notes** → clipboard
- **Open GitHub ↗** → pre-filled `releases/new` URL (tag, title, body pre-populated)
- **Done & Next** → advance to the next failure
- **Skip** → skip without marking done

---

## Server Logs

A slide-up log drawer (bottom-right **📋 Logs** button, or press `L`) shows the last 300 lines of `adabot_web.log`, auto-refreshing every 6 seconds while open.

---

## Known Limitations

- **Arduino libraries only** — the collector calls `arduino_libraries.py` which scans `adafruit` org repos with `library.properties`. Other Adafruit repos (CircuitPython, etc.) are not shown.
- **Token scope** — the `tyeth-ai-assisted` classic PAT expires periodically. Update `ADABOT_GITHUB_ACCESS_TOKEN` or the fallback in `run_web.py` when it does.
- **`release_tag = "None"`** — `arduino_libraries.py` stores the string `"None"` for repos with no tag. The semver bump code guards against this but the display shows `*None*` as a signal to investigate.
- **LLM availability** — release notes generation uses `claude -p` (Claude Code CLI). If not installed or rate-limited, it falls back to a heuristic summary.
- **Collection time** — full enrichment of ~600 repos takes ~20 minutes. The UI remains usable with partial data during collection.
