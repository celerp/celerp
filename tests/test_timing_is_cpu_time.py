# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A test that bounds how long pure Python work takes measures the CPU time it spends, not the
wall clock: other processes on the machine (a parallel test run) stretch the wall clock of a
long run more than a short one, so a timing that held alone fails on a busy machine. A test
that compares runs times them in turn, round after round: the load on a busy machine changes
from moment to moment and stretches even CPU time (shared caches), so runs timed one after
the other are compared under different loads."""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from test_helpers import REPO_ROOT, fastest_cpu_seconds

# Tests whose timed work runs in the database server, which the test process's CPU time
# does not see: they time the wall clock on purpose.
_WALL_CLOCK = {"test_cost_follows_goods.py"}


def test_waiting_is_not_counted():
    assert fastest_cpu_seconds(lambda: time.sleep(0.2)) == [pytest.approx(0, abs=0.05)]


def test_runs_compared_are_timed_in_turn():
    ran = []
    fastest_cpu_seconds(lambda: ran.append("small"), lambda: ran.append("large"), times=3)
    assert ran == ["small", "large"] * 3


def test_every_timing_compared_is_taken_in_one_call():
    """A call in a loop or in a helper of the test times runs one after the other."""
    repeated = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for test in (n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for outer in ast.walk(test):
                if isinstance(outer, (ast.For, ast.While, ast.comprehension, ast.Lambda)) or (
                        outer is not test and isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef))):
                    repeated += [f"{path.name}:{call.lineno}" for call in ast.walk(outer)
                                 if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "fastest_cpu_seconds"]
    assert sorted(set(repeated)) == []


def test_pure_work_is_timed_in_cpu_time():
    timed = sorted(p.name for p in (REPO_ROOT / "tests").rglob("*.py")
                   if p.name != Path(__file__).name and "perf_counter" in p.read_text(encoding="utf-8"))
    assert [name for name in timed if name not in _WALL_CLOCK] == []
