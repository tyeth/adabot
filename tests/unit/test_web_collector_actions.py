# SPDX-FileCopyrightText: 2026 Adafruit Industries
#
# SPDX-License-Identifier: MIT

"""Tests for the tolerant CI-workflow detection in adabot_web.collector."""

import textwrap

import pytest

from adabot_web.collector import _workflow_ci_events


def _wf(on_block, jobs="jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n"):
    return textwrap.dedent(on_block) + "\n" + jobs


@pytest.mark.parametrize(
    "on_block, expected",
    [
        # bare scalar
        ("on: push", {"push"}),
        ("'on': pull_request", {"pull_request"}),
        # flow list
        ("on: [push, pull_request]", {"push", "pull_request"}),
        ('"on": [ "push" , "release" ]', {"push", "release"}),
        # block list
        ("on:\n  - push\n  - pull_request", {"push", "pull_request"}),
        # block mapping with empty values (Adafruit_Monster_Eyes build.yml)
        (
            """\
            name: Arduino Library CI
            on:
              push:
              pull_request:
              workflow_dispatch:
              release:
                types: [published]
            """,
            {"push", "pull_request", "workflow_dispatch", "release"},
        ),
        # block mapping with nested filters + workflow_call (Wippersnapper)
        (
            """\
            name: WipperSnapper Build CI
            on:
              workflow_dispatch:
                inputs:
                  board:
                    required: false
              pull_request:
              push:
                branches:
                  - main
              workflow_call:
                secrets:
                  GH_REPO_TOKEN:
                    required: true
            """,
            {"workflow_dispatch", "pull_request", "push", "workflow_call"},
        ),
        # schedule-only nightly build still counts
        ("on:\n  schedule:\n    - cron: '0 3 * * *'", {"schedule"}),
        # non-CI triggers do not count
        ("on:\n  issues:\n    types: [opened]", set()),
        ("on: [issue_comment, discussion]", set()),
        # no 'on' key at all
        ("name: nothing", set()),
    ],
)
def test_workflow_ci_events(on_block, expected):
    assert _workflow_ci_events(_wf(on_block)) == expected


def test_workflow_without_jobs_is_ignored():
    assert _workflow_ci_events("on: [push, pull_request]\n") == set()
    assert _workflow_ci_events("on:\n  push:\njobs: {}\n") == set()


def test_unparseable_yaml_falls_back_to_text_scan():
    broken = textwrap.dedent(
        """\
        name: Broken
        on:
          push:
          pull_request:
            branches: [main
        jobs:
          build:
            runs-on: ubuntu-latest
        """
    )
    assert _workflow_ci_events(broken) == {"push", "pull_request"}


def test_unparseable_yaml_without_jobs_is_ignored():
    assert _workflow_ci_events("on: [push\n") == set()
