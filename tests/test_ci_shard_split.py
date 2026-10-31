# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The test shards split the suite by the measured durations in tests/.test_durations.
A shard's run swings by a third from run to run and collection takes about a minute of
its 600 s, so the split keeps each shard's measured share at 450 s or less, and the
matrix runs every group the split makes."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SHARE_CAP_S = 450


def _shard_job() -> tuple[list, int]:
    job = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["shard"]
    run = next(s["run"] for s in job["steps"] if "--splits" in s.get("run", ""))
    return job["strategy"]["matrix"]["group"], int(re.search(r"--splits (\d+)", run).group(1))


def test_the_matrix_runs_every_group_the_split_makes():
    groups, splits = _shard_job()
    assert groups == list(range(1, splits + 1))


def test_each_shard_s_measured_share_stays_under_the_cap():
    _, splits = _shard_job()
    total = sum(json.loads((ROOT / "tests" / ".test_durations").read_text()).values())
    assert total / splits <= SHARE_CAP_S, f"{total:.0f} s over {splits} shards; raise --splits"
