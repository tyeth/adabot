# This file allows easy debugging of the arduino_libraries.py script
# Update your username and access token below.
# Set ci=0 to not check changes (for CI only)
# Set bump-ci-repos=1 to bump version for CI only changes
# Set idiot=1 to confirm generating version bump PRs

from adabot import arduino_libraries
import os
import sys
import time
os.environ.setdefault("ADABOT_GITHUB_USER", "tyeth")
os.environ.setdefault(
    "ADABOT_GITHUB_ACCESS_TOKEN", "YOUR_PERSONAL_ACCESS_TOKEN"
)

sys.argv = [
    "arduino_libraries.py",
    "--ci",
    "1",
    "--bump-ci-repos",
    "0",
    "--idiot",
    "0",
]
timestamp = time.strftime("%Y-%m-%d_%H%M%S")

arduino_libraries.main(verbosity=1, output_file=f"{timestamp}.md",ci=1)
print("done")


## TODO:
# Add CI checking after PR is created. Maybe output PRs to json, recheck file on load.
# Start with single PR + action review, once fail/pass then add more PRs. Eventually parallelize.
# Failures should be printed with URLS to runs, so that we can see what failed.
# User should be asked at run to archive old runs (rename file) or recheck.
# Also asked if individual PRs should be checked or all automatically.
# If archiving, then ask if delete/close old draft version bump PRs (linked in json file), assume yes.
# Also delete branch for PRs that were merged or closed.
# If rechecking, then ask if delete/close failing-actions draft version bump PRs (newly linked in json file), assume no.
# If actions pass then merge and delete branch for version bump PR.
# If actions fail then delete/close version bump PRs and branch (if user agreed, else just log and save in json).
# If user wants to recheck individual PRs, then check PR (action results) one by one and ask each time if failure then delete PR (or if pass then merge PR question), else recheck all.


## CI check flag should change to be about Readme / board assets / CI only changes, log accordingly.
# Current Issues for Repos and PRs could be shown, ideally with days Old and last updated/commented.