# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The unit and browser shards split their suites by the measured durations in
tests/.test_durations. A shard's own `timeout N` is the only budget: a shard costs about 80 s
of collection on top of its measured share, and a slow runner stretches the run by up to 1.6
times (177 s against 110 s to the first 7% at one head), so the split keeps
share * 1.6 + 80 <= N, and the matrix runs every group the split makes. The share only means
something while the file measures the tests each job runs, compared by their full ids with
parameters, as the job collects them: every test a job runs has an entry (a missing one counts
as the file's average, however slow it is), and no entry names a test no job runs. The ids
come from the installed test tooling (pytest shortens long parameters, pytest-playwright adds
the browser), so the collection is only CI's when the tooling here is what CI installs from the
dev extra, and the split only stays as measured while the packages that make the ids or assign
them to shards are pinned exactly there and installed from nowhere else."""

from __future__ import annotations

import os
import re
import shlex
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from shard_durations import (  # noqa: E402
    ROOT, dev_extra, durations, flag, split_jobs, stale, ids_the_job_runs, tooling_unlike_ci, unmeasured,
)

# The first test collects every job's tests, two jobs of up to 600 s each.
pytestmark = pytest.mark.timeout(1200)

SLOW_RUNNER = 1.6
COLLECTION_S = 80
# A release of any of these can change the collected ids or the shard each id lands on.
SHAPES_THE_SPLIT = ("pytest", "pytest-playwright", "pytest-split")


def test_the_unit_and_browser_jobs_split_by_the_file():
    assert set(split_jobs()) == {"shard", "browser"}


def test_the_tooling_is_what_ci_installs():
    assert tooling_unlike_ci() == [], "install the dev extra: pip install -e '.[dev]'"


@pytest.mark.parametrize("name", SHAPES_THE_SPLIT)
def test_what_shapes_the_split_is_pinned_exactly(name):
    pins = [[(s.operator, "*" in s.version) for s in req.specifier] for req in dev_extra() if req.name == name]
    assert pins == [[("==", False)]], f"pin {name} to one version (==) in the dev extra"


def test_ci_installs_what_shapes_the_split_only_from_the_dev_extra():
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    extra = sorted({word for job in jobs.values() for step in job.get("steps", [])
                    if "pip install" in step.get("run", "")
                    for word in shlex.split(step["run"].replace("${{", "").replace("}}", ""))
                    if re.split(r"[<>=!~\[;]", word)[0] in SHAPES_THE_SPLIT})
    assert extra == [], "install these from the dev extra, where they are pinned"


@pytest.mark.parametrize("job", sorted(split_jobs()))
def test_the_matrix_runs_every_group_the_split_makes(job):
    groups, args = split_jobs()[job]
    assert groups == list(range(1, int(flag(args, "--splits")[0]) + 1))


def test_every_entry_names_a_test_a_job_runs():
    assert stale() == [], "run python scripts/shard_durations.py and commit tests/.test_durations"


@pytest.mark.parametrize("job", sorted(split_jobs()))
def test_every_test_the_job_runs_is_measured(job):
    assert unmeasured(job) == [], "run python scripts/shard_durations.py and commit tests/.test_durations"


@pytest.mark.parametrize("job", sorted(split_jobs()))
def test_each_shard_fits_its_own_timeout_on_a_slow_runner(job):
    _, args = split_jobs()[job]
    timeout = int(args[args.index("timeout") + 1])
    splits = int(flag(args, "--splits")[0])
    measured = durations()
    share = sum(measured.get(t, 0) for t in ids_the_job_runs(job)) / splits
    assert share * SLOW_RUNNER + COLLECTION_S <= timeout, (
        f"{share:.0f} s a shard: {share * SLOW_RUNNER + COLLECTION_S:.0f} s on a slow runner "
        f"against timeout {timeout}; raise --splits")
