# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The test shards split the suite by the measured durations in tests/.test_durations.
Collection takes about 90 s of a shard's 600 s and the same shard swings by a third from
run to run, so the split keeps each shard's measured share at 380 s or less, and the
matrix runs every group the split makes. The share only means something while the file
measures the tests the job runs: every file the job runs has entries (unless its module
mark keeps it out of the job), and no entry names a file the job does not run."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SHARE_CAP_S = 380


def _shard_run() -> tuple[list, list[str]]:
    job = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["shard"]
    run = next(s["run"] for s in job["steps"] if "--splits" in s.get("run", ""))
    return job["strategy"]["matrix"]["group"], shlex.split(run)


def _flag(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name] + [
        a.split("=", 1)[1] for a in args if a.startswith(name + "=")
    ]


def _files_the_job_runs(args: list[str]) -> set[str]:
    """Test files under the job's paths, less its --ignore paths and the files whose
    module mark is one the job's -m expression leaves out."""
    paths = [a for a in args[args.index("pytest") + 1:] if not a.startswith("-") and "/" in a]
    ignored = [i.rstrip("/") for i in _flag(args, "--ignore")]
    left_out = re.findall(r"not (\w+)", _flag(args, "-m")[0])
    files = set()
    for path in paths:
        for f in ROOT.glob(path.rstrip("/") + "/**/test_*.py"):
            rel = f.relative_to(ROOT).as_posix()
            if any(rel == i or rel.startswith(i + "/") for i in ignored):
                continue
            mark = re.search(r"^pytestmark\s*=(.*?)(?:^\S|\Z)", f.read_text(), re.M | re.S)
            if mark and any(re.search(rf"pytest\.mark\.{m}\b", mark.group(1)) for m in left_out):
                continue
            files.add(rel)
    return files


def _durations() -> dict[str, float]:
    return json.loads((ROOT / "tests" / ".test_durations").read_text())


def test_the_matrix_runs_every_group_the_split_makes():
    groups, args = _shard_run()
    assert groups == list(range(1, int(_flag(args, "--splits")[0]) + 1))


def test_the_durations_measure_exactly_the_files_the_job_runs():
    _, args = _shard_run()
    runs = _files_the_job_runs(args)
    measured = {k.split("::")[0] for k in _durations()}
    assert sorted(runs - measured) == [], "unmeasured; refresh tests/.test_durations"
    assert sorted(measured - runs) == [], "measured but not run by the shard job"


def test_each_shard_s_measured_share_stays_under_the_cap():
    _, args = _shard_run()
    splits = int(_flag(args, "--splits")[0])
    total = sum(_durations().values())
    assert total / splits <= SHARE_CAP_S, f"{total:.0f} s over {splits} shards; raise --splits"
