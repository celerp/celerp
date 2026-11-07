# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The test shards split the suite by the measured durations in tests/.test_durations.
The shard's own `timeout N` is the only budget: a shard costs about 80 s of collection
on top of its measured share, and a slow runner stretches the run by up to 1.6 times
(177 s against 110 s to the first 7% at one head), so the split keeps
share * 1.6 + 80 <= N, and the matrix runs every group the split makes. The share only
means something while the file measures the tests the job runs: every file the job runs
has entries (unless its module mark keeps it out of the job), and no entry names a file
the job does not run. Within a file the same holds test by test: every test the job runs
has an entry (a missing one counts as the average, however slow it is), and no entry
names a test that is gone."""

from __future__ import annotations

import ast
import json
import re
import shlex
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SLOW_RUNNER = 1.6
COLLECTION_S = 80


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
    left_out = _left_out(args)
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


def _left_out(args: list[str]) -> tuple[str, ...]:
    return tuple(re.findall(r"not (\w+)", _flag(args, "-m")[0]))


def _marked_out(node: ast.AST, left_out: tuple[str, ...]) -> bool:
    return any(re.search(rf"\bpytest\.mark\.{m}\b", ast.unparse(d)) for d in node.decorator_list
               for m in left_out)


@cache
def _tests_in(rel: str, left_out: tuple[str, ...]) -> frozenset[str]:
    """The test ids, less parameters, that `rel` defines and the job's -m keeps."""
    tests = set()
    for node in ast.parse((ROOT / rel).read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            if not _marked_out(node, left_out):
                tests.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test") and not _marked_out(node, left_out):
            tests |= {f"{node.name}::{m.name}" for m in node.body
                      if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and m.name.startswith("test") and not _marked_out(m, left_out)}
    return frozenset(tests)


def _measured_tests() -> set[tuple[str, str]]:
    return {(rel, test.split("[")[0]) for rel, test in (k.split("::", 1) for k in _durations())}


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


def test_every_entry_names_a_test_that_is_there():
    gone = sorted(f"{rel}::{test}" for rel, test in _measured_tests()
                  if not (ROOT / rel).exists() or test not in _tests_in(rel, ()))
    assert gone == [], "drop these from tests/.test_durations"


def test_every_test_the_job_runs_is_measured():
    _, args = _shard_run()
    left_out = _left_out(args)
    measured = _measured_tests()
    unmeasured = sorted(f"{rel}::{test}" for rel in _files_the_job_runs(args)
                        for test in _tests_in(rel, left_out) if (rel, test) not in measured)
    assert unmeasured == [], "unmeasured; refresh tests/.test_durations"


def test_each_shard_fits_its_own_timeout_on_a_slow_runner():
    _, args = _shard_run()
    timeout = int(args[args.index("timeout") + 1])
    splits = int(_flag(args, "--splits")[0])
    share = sum(_durations().values()) / splits
    assert share * SLOW_RUNNER + COLLECTION_S <= timeout, (
        f"{share:.0f} s a shard: {share * SLOW_RUNNER + COLLECTION_S:.0f} s on a slow runner "
        f"against timeout {timeout}; raise --splits")
