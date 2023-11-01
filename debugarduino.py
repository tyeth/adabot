# This file allows easy debugging of the arduino_libraries.py script
# Update your username and access token below.
# Set ci=0 to not check changes (for CI only)
# Set bump-ci-repos=1 to bump version for CI only changes
# Set idiot=1 to confirm bumping version PRs

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
