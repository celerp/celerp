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

import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

# The first test collects every job's tests, two jobs of up to 600 s each.
pytestmark = pytest.mark.timeout(1200)

ROOT = Path(__file__).resolve().parent.parent
SLOW_RUNNER = 1.6
COLLECTION_S = 80
# A release of any of these can change the collected ids or the shard each id lands on.
SHAPES_THE_SPLIT = ("pytest", "pytest-playwright", "pytest-split")


@cache
def _split_jobs() -> dict[str, tuple[list, list[str]]]:
    """Each CI job that splits by the durations file: its matrix groups and its pytest words."""
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    found = {}
    for name, job in jobs.items():
        for step in job.get("steps", []):
            if "--durations-path" in step.get("run", ""):
                # One group's number stands in for the matrix expression, which is no shell word.
                found[name] = (job["strategy"]["matrix"]["group"],
                               shlex.split(step["run"].replace("${{ matrix.group }}", "1")))
    return found


def _flag(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name] + [
        a.split("=", 1)[1] for a in args if a.startswith(name + "=")
    ]


def _dev_extra() -> list[Requirement]:
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    return list(map(Requirement, dev))


def _tooling_unlike_ci() -> list[str]:
    """The dev extra's requirements the installed packages do not meet."""
    unmet = []
    for req in _dev_extra():
        if req.marker and not req.marker.evaluate():
            continue
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            installed = None
        if installed is None or not req.specifier.contains(installed, prereleases=True):
            unmet.append(f"{req} (installed: {installed})")
    return unmet


# The job's own options that split or report the run rather than choose its tests.
_RUN_ONLY = {"-n": 1, "--splits": 1, "--group": 1, "--splitting-algorithm": 1, "--durations-path": 1, "-q": 0}


@cache
def _tests_the_job_runs(job: str) -> frozenset[str]:
    """The test ids, with their parameters, that the job collects: its own paths and options,
    collected the way the job collects them."""
    assert not _tooling_unlike_ci(), "install the dev extra first: ids collected here would not be CI's"
    _, args = _split_jobs()[job]
    chosen, rest = [], iter(args[args.index("pytest") + 1:])
    for arg in rest:
        if arg in _RUN_ONLY:
            for _ in range(_RUN_ONLY[arg]):
                next(rest)
        elif "/" in arg and not arg.startswith("-") and "*" in arg:
            chosen += sorted(p.relative_to(ROOT).as_posix() + "/" for p in ROOT.glob(arg.rstrip("/")))
        else:
            chosen.append(arg)
    # One id a line whatever -q the job's addopts carry: --verbosity sets the level outright.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    out = subprocess.run([sys.executable, "-m", "pytest", *chosen, "--collect-only", "--verbosity=-1", "-p", "no:cacheprovider"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stdout[-4000:] + out.stderr[-4000:]
    return frozenset(line for line in out.stdout.splitlines() if "::" in line and not line.startswith(" "))


def _durations() -> dict[str, float]:
    return json.loads((ROOT / "tests" / ".test_durations").read_text())


def test_the_unit_and_browser_jobs_split_by_the_file():
    assert set(_split_jobs()) == {"shard", "browser"}


def test_the_tooling_is_what_ci_installs():
    assert _tooling_unlike_ci() == [], "install the dev extra: pip install -e '.[dev]'"


@pytest.mark.parametrize("name", SHAPES_THE_SPLIT)
def test_what_shapes_the_split_is_pinned_exactly(name):
    pins = [[(s.operator, "*" in s.version) for s in req.specifier] for req in _dev_extra() if req.name == name]
    assert pins == [[("==", False)]], f"pin {name} to one version (==) in the dev extra"


def test_ci_installs_what_shapes_the_split_only_from_the_dev_extra():
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    extra = sorted({word for job in jobs.values() for step in job.get("steps", [])
                    if "pip install" in step.get("run", "")
                    for word in shlex.split(step["run"].replace("${{", "").replace("}}", ""))
                    if re.split(r"[<>=!~\[;]", word)[0] in SHAPES_THE_SPLIT})
    assert extra == [], "install these from the dev extra, where they are pinned"


@pytest.mark.parametrize("job", sorted(_split_jobs()))
def test_the_matrix_runs_every_group_the_split_makes(job):
    groups, args = _split_jobs()[job]
    assert groups == list(range(1, int(_flag(args, "--splits")[0]) + 1))


def test_every_entry_names_a_test_a_job_runs():
    gone = sorted(_durations().keys() - frozenset().union(*map(_tests_the_job_runs, _split_jobs())))
    assert gone == [], "drop these from tests/.test_durations"


@pytest.mark.parametrize("job", sorted(_split_jobs()))
def test_every_test_the_job_runs_is_measured(job):
    unmeasured = sorted(_tests_the_job_runs(job) - _durations().keys())
    assert unmeasured == [], "unmeasured; refresh tests/.test_durations"


@pytest.mark.parametrize("job", sorted(_split_jobs()))
def test_each_shard_fits_its_own_timeout_on_a_slow_runner(job):
    _, args = _split_jobs()[job]
    timeout = int(args[args.index("timeout") + 1])
    splits = int(_flag(args, "--splits")[0])
    durations = _durations()
    share = sum(durations.get(t, 0) for t in _tests_the_job_runs(job)) / splits
    assert share * SLOW_RUNNER + COLLECTION_S <= timeout, (
        f"{share:.0f} s a shard: {share * SLOW_RUNNER + COLLECTION_S:.0f} s on a slow runner "
        f"against timeout {timeout}; raise --splits")
