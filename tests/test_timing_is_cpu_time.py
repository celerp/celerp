# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A test that bounds how long pure Python work takes measures the CPU time it spends, not the
wall clock: other processes on the machine (a parallel test run) stretch the wall clock of a
long run more than a short one, so a timing that held alone fails on a busy machine."""

from __future__ import annotations

import time
from pathlib import Path

from test_helpers import REPO_ROOT, fastest_cpu_seconds

# Tests whose timed work runs in the database server, which the test process's CPU time
# does not see: they time the wall clock on purpose.
_WALL_CLOCK = {"test_cost_follows_goods.py"}


def test_waiting_is_not_counted():
    assert fastest_cpu_seconds(lambda: time.sleep(0.2)) < 0.05


def test_pure_work_is_timed_in_cpu_time():
    timed = sorted(p.name for p in (REPO_ROOT / "tests").rglob("*.py")
                   if p.name != Path(__file__).name and "perf_counter" in p.read_text(encoding="utf-8"))
    assert [name for name in timed if name not in _WALL_CLOCK] == []
