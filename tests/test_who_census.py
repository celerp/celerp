# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Who a change reads as made by is decided in one place, over every reader and every
unattended door who_census finds in the code.

Only the producer (ledger_display._maker) and actor_label read the display keys, so every feed
and History table shows the same "who". Every task started from the background_loop slot runs
unattended, and every reader of the periodic_operation slot is reached only from such a task,
so work nobody started by hand reads "Automatically". A new reader, loop start or door turns
these red without anyone editing a list."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import who_census  # noqa: E402


def test_the_producer_gives_the_automatic_mark():
    assert {"actor_name", "actor_automatic"} <= who_census.display_keys()


def test_only_actor_label_reads_who_made_a_change():
    readers = who_census.key_readers()
    assert {r[2] for r in readers if r[3]} == who_census.display_keys()
    assert [r for r in readers if not r[3]] == []


def test_every_background_loop_task_runs_unattended():
    starts = who_census.loop_starts()
    assert starts and all(wrapped for _, _, wrapped in starts), starts


def test_every_unattended_door_is_reached_only_from_a_background_loop():
    doors = who_census.unattended_doors()
    assert any(d[0] == "periodic_operation" and d[1].startswith("reader ") for d in doors), doors
    assert [d for d in doors if d[3] == "NOT-FROM-LOOP"] == []
