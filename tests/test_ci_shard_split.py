# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The test shards split the suite by the measured durations in tests/.test_durations.
The shard's own `timeout N` is the only budget: a shard costs about 80 s of collection
on top of its measured share, and a slow runner stretches the run by up to 1.6 times
(177 s against 110 s to the first 7% at one head), so the split keeps
share * 1.6 + 80 <= N, and the matrix runs every group the split makes. The share only
means something while the file measures the tests the job runs, compared by their full ids
with parameters, as the job collects them: every test the job runs has an entry (a missing
one counts as the file's average, however slow it is), and no entry names a test the job
does not run."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SLOW_RUNNER = 1.6
COLLECTION_S = 80


def _shard_run() -> tuple[list, list[str]]:
    job = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["shard"]
    run = next(s["run"] for s in job["steps"] if "--splits" in s.get("run", ""))
    # One group's number stands in for the matrix expression, which is no shell word.
    return job["strategy"]["matrix"]["group"], shlex.split(run.replace("${{ matrix.group }}", "1"))


def _flag(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name] + [
        a.split("=", 1)[1] for a in args if a.startswith(name + "=")
    ]


# The job's own options that split or report the run rather than choose its tests.
_RUN_ONLY = {"-n": 1, "--splits": 1, "--group": 1, "--splitting-algorithm": 1, "--durations-path": 1,
             "-q": 0, "--tb=short": 0}


@cache
def _tests_the_job_runs() -> frozenset[str]:
    """The test ids, with their parameters, that the shard job collects: its own paths, -m and
    --ignore, collected the way the job collects them."""
    _, args = _shard_run()
    chosen, rest = [], iter(args[args.index("pytest") + 1:])
    for arg in rest:
        if arg in _RUN_ONLY:
            for _ in range(_RUN_ONLY[arg]):
                next(rest)
        elif "/" in arg and not arg.startswith("-") and "*" in arg:
            chosen += sorted(p.relative_to(ROOT).as_posix() + "/" for p in ROOT.glob(arg.rstrip("/")))
        else:
            chosen.append(arg)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    out = subprocess.run([sys.executable, "-m", "pytest", *chosen, "--collect-only", "-p", "no:cacheprovider"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stdout[-4000:] + out.stderr[-4000:]
    return frozenset(line for line in out.stdout.splitlines() if "::" in line and not line.startswith(" "))


def _durations() -> dict[str, float]:
    return json.loads((ROOT / "tests" / ".test_durations").read_text())


def test_the_matrix_runs_every_group_the_split_makes():
    groups, args = _shard_run()
    assert groups == list(range(1, int(_flag(args, "--splits")[0]) + 1))


def test_every_entry_names_a_test_the_job_runs():
    gone = sorted(_durations().keys() - _tests_the_job_runs())
    assert gone == [], "drop these from tests/.test_durations"


def test_every_test_the_job_runs_is_measured():
    unmeasured = sorted(_tests_the_job_runs() - _durations().keys())
    assert unmeasured == [], "unmeasured; refresh tests/.test_durations"


def test_each_shard_fits_its_own_timeout_on_a_slow_runner():
    _, args = _shard_run()
    timeout = int(args[args.index("timeout") + 1])
    splits = int(_flag(args, "--splits")[0])
    share = sum(_durations().values()) / splits
    assert share * SLOW_RUNNER + COLLECTION_S <= timeout, (
        f"{share:.0f} s a shard: {share * SLOW_RUNNER + COLLECTION_S:.0f} s on a slow runner "
        f"against timeout {timeout}; raise --splits")
