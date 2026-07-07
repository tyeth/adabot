#!/bin/bash
`kill $(ps aux | grep '[r]un_web.py --port 8080' | awk '{print $2}') 2>/dev/null; ADABOT_GITHUB_ACCESS_TOKEN=$(gh auth token) .venv/bin/python run_web.py --port 8080`
