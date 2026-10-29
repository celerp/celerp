# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Every lock on an account row is taken to change it, under the company's chart lock.

A posting reads the accounts it posts to under the shared chart lock and locks no account
row; a change to the chart takes the chart lock exclusively before the rows it edits. Rows
locked any other way (FOR SHARE, or FOR UPDATE without the chart lock first) can be taken in
opposite orders by a posting and a change, and PostgreSQL aborts one as a deadlock
(tests/test_chart_lock_concurrency_pg.py)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _chain_names(node: ast.AST, assigned: dict[str, list[ast.AST]]) -> set[str]:
    """The model names a ``select(...)`` in this call chain selects, following a statement
    built in a local variable to what the function assigns it."""
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in assigned:
            for value in assigned.pop(sub.id):
                names |= _chain_names(value, assigned)
        if isinstance(sub, ast.Call) and getattr(sub.func, "id", getattr(sub.func, "attr", "")) == "select":
            names |= {a.id for a in sub.args if isinstance(a, ast.Name)}
            names |= {a.value.id for a in sub.args if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name)}
    return names


def _exclusive_chart_lock(call: ast.Call) -> bool:
    return getattr(call.func, "id", getattr(call.func, "attr", "")) == "lock_chart" and not any(
        kw.arg == "shared" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
        for kw in call.keywords)


def account_row_locks_outside_the_chart_lock(root: Path) -> list[str]:
    """Each ``with_for_update`` on an Account select that is FOR SHARE, or that its function
    does not precede with an exclusive ``lock_chart``."""
    wrong = []
    for path in sorted([*(root / "celerp").rglob("*.py"), *(root / "default_modules").rglob("*.py")]):
        rel = path.relative_to(root).as_posix()
        if "/tests/" in rel or "/migrations/" in rel:
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            charted = [c.lineno for c in calls if _exclusive_chart_lock(c)]
            assignments: dict[str, list[ast.AST]] = {}
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign):
                    for target in n.targets:
                        if isinstance(target, ast.Name):
                            assignments.setdefault(target.id, []).append(n.value)
            for c in calls:
                if not (isinstance(c.func, ast.Attribute) and c.func.attr == "with_for_update"):
                    continue
                if "Account" not in _chain_names(c.func.value, {k: list(v) for k, v in assignments.items()}):
                    continue
                if any(kw.arg in ("read", "key_share") for kw in c.keywords):
                    wrong.append(f"{rel}:{c.lineno} {fn.name}: an account row FOR SHARE")
                elif not any(line <= c.lineno for line in charted):
                    wrong.append(f"{rel}:{c.lineno} {fn.name}: an account row FOR UPDATE without lock_chart first")
    return wrong


def test_every_account_row_lock_sits_under_the_exclusive_chart_lock():
    assert account_row_locks_outside_the_chart_lock(ROOT) == []


def test_the_census_finds_a_share_lock_and_an_unguarded_update(tmp_path):
    module = tmp_path / "default_modules" / "m"
    module.mkdir(parents=True)
    (tmp_path / "celerp").mkdir()
    (module / "x.py").write_text(
        "async def share(s, c):\n"
        "    return await s.execute(select(Account).where(Account.code == c).with_for_update(read=True))\n"
        "async def update(s, c):\n"
        "    return await s.execute(select(Account.id).with_for_update())\n"
        "async def guarded(s, c):\n"
        "    await lock_chart(s, c)\n"
        "    return await s.execute(select(Account).with_for_update())\n"
        "async def guarded_shared(s, c):\n"
        "    await lock_chart(s, c, shared=True)\n"
        "    return await s.execute(select(Account).with_for_update())\n"
        "async def built(s, c):\n"
        "    stmt = select(Account).where(Account.code == c)\n"
        "    stmt = stmt.with_for_update()\n"
        "    return await s.execute(stmt)\n"
        "async def other(s, c):\n"
        "    return await s.execute(select(BankAccount).with_for_update())\n")
    assert account_row_locks_outside_the_chart_lock(tmp_path) == [
        "default_modules/m/x.py:2 share: an account row FOR SHARE",
        "default_modules/m/x.py:4 update: an account row FOR UPDATE without lock_chart first",
        "default_modules/m/x.py:10 guarded_shared: an account row FOR UPDATE without lock_chart first",
        "default_modules/m/x.py:13 built: an account row FOR UPDATE without lock_chart first",
    ]
