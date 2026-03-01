#New TODOs
You must never release anything, ever. That's the users job during testing / actual usage.

releasing doesnt show CI tracking of the release, and the repo list badge / other release badges don't show the status of the release CI run, and the reload CI didn't show the release run jobs.

Also the released repo doesn't get unmarked if already marked, and the overview plus repo list entry show stale info.

Releases were meant to be part of a release / changelog markdown file with the timestamp of today, both individual releases and bulk ones, as I'm required to report the released repos in a separate email/basecamp task from the generated markdwon report. I thought we already had some code for this, but doing a couple of test releases I didn't notice a new/changed markdown file in the git status.

It would also be quite nice if the tree of objs/props in web_state.json was sorted, so the changes/diff were more easily aligned/viewed. 