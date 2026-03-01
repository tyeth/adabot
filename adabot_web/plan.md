#New TODOs
You must never release anything, ever. That's the users job during testing / actual usage.

releasing doesnt show CI tracking of the release, and the repo list badge / other release badges don't show the status of the release CI run, and the reload CI didn't show the release run jobs.

Also the released repo doesn't get unmarked if already marked, and the overview plus repo list entry show stale info.

Releases were meant to be part of a release / changelog markdown file with the timestamp of today, both individual releases and bulk ones, as I'm required to report the released repos in a separate email/basecamp task from the generated markdwon report. I thought we already had some code for this, but doing a couple of test releases I didn't notice a new/changed markdown file in the git status.

It would also be quite nice if the tree of objs/props in web_state.json was sorted, so the changes/diff were more easily aligned/viewed.

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