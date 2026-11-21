# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Each assistant table and the bank statement line table is written by one module only.

The module that owns a table takes the locks its rows need (the company lock, the row
lock) before it writes: usage rows settle under record_usage's lock, a job's rows under
its runner's fence, a conversation's messages after the conversation row. A write from
any other module skips those and races the owner. The census reads every shipped source,
so a writer added anywhere, by a new tool or route, is caught."""

from __future__ import annotations

import ast

import pytest

from shipped_sources import shipped_sources

OWNERS = {
    "AIBatchJob": {"celerp/ai/jobs.py"},
    "AIJobStep": {"celerp/ai/jobs.py"},
    "AIConversation": {"celerp/ai/conversations.py"},
    "AIMessage": {"celerp/ai/conversations.py"},
    "AIUsage": {"celerp/ai/usage.py"},
    "BankStatementLine": {"default_modules/celerp-accounting/celerp_accounting/bank_statements.py"},
}
_WRITES = {"insert", "pg_insert", "update", "delete"}


def _writers() -> dict[str, set[str]]:
    """Model name -> the shipped files that insert, update or delete its rows or build one."""
    found: dict[str, set[str]] = {m: set() for m in OWNERS}
    for rel, path in shipped_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            target = getattr(node.args[0], "id", None) if name in _WRITES and node.args else name
            if target in found:
                found[target].add(rel)
    return found


_FOUND = _writers()


@pytest.mark.parametrize("model", sorted(OWNERS))
def test_only_the_owner_writes(model):
    assert _FOUND[model] == OWNERS[model]
