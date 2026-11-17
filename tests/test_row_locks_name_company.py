# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A row lock is taken by a statement that names its company. ``session.refresh(obj,
with_for_update=...)`` selects the row by its primary key alone, so the lock-order check
cannot tell that the company's lock covers it and counts it as a take outside that lock;
one such take of a class leaves every company-gated edge into that class unproven.
Lock the row with a select that names the company instead."""
from __future__ import annotations

import ast

from shipped_sources import shipped_sources


def refresh_row_locks(source: str) -> list[int]:
    """Lines of ``*.refresh(..., with_for_update=...)`` calls."""
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "refresh"
            and any(k.arg == "with_for_update" for k in node.keywords)]


def test_no_row_lock_is_taken_by_primary_key_alone():
    found = {rel: lines for rel, path in shipped_sources() if (lines := refresh_row_locks(path.read_text()))}
    assert found == {}


def test_the_check_finds_a_refresh_under_a_row_lock():
    assert refresh_row_locks("async def f(db, bank):\n    await db.refresh(bank, with_for_update=True)\n") == [2]
    assert refresh_row_locks("async def f(db, bank):\n    await db.refresh(bank)\n") == []
