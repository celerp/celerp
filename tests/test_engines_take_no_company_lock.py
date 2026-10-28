# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The event and projection engines serve requests, imports, replay and internal work,
each with its own transaction. They observe the company lock and may refuse work that
lacks it; they never take it, so the operation that owns a write takes the lock at its
own boundary, before any narrower lock it holds (company_lock.lock_company)."""

from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINES = ("celerp/projections", "celerp/events/engine.py")
# The primitives that take a company's lock.
TAKES_THE_LOCK = {"lock_company", "locked_company", "lock_projections", "hold_company", "lock_company_for_deletion"}
# Taken before the change to an existing item is applied; it predates this rule and moves
# to the item writers' own boundary.
ALLOWED = {("celerp/events/engine.py", "emit_event", "lock_company")}


def _engine_files():
    for top in ENGINES:
        path = os.path.join(ROOT, top)
        if os.path.isfile(path):
            yield top
            continue
        for folder, _, files in os.walk(path):
            for name in files:
                if name.endswith(".py"):
                    yield os.path.relpath(os.path.join(folder, name), ROOT)


def _lock_calls(rel: str):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name in TAKES_THE_LOCK:
                    yield fn.name, name, node.lineno


def test_no_engine_takes_a_company_lock():
    taken = [f"{rel}:{line} {fn} calls {name}" for rel in _engine_files()
             for fn, name, line in _lock_calls(rel) if (rel, fn, name) not in ALLOWED]
    assert not taken, "an engine takes the company lock; its caller must take it instead:\n" + "\n".join(taken)


def test_the_allowed_lock_is_still_there():
    """An allowance its code no longer needs is removed with it."""
    found = {(rel, fn, name) for rel in _engine_files() for fn, name, _ in _lock_calls(rel)}
    assert ALLOWED <= found


def test_the_guard_sees_a_lock_taken_in_an_engine(tmp_path, monkeypatch):
    engine = tmp_path / "celerp" / "projections"
    engine.mkdir(parents=True)
    (engine / "x.py").write_text("async def replay(s, c):\n    await lock_company(s, c)\n")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", str(tmp_path))
    assert [c for rel in _engine_files() for c in _lock_calls(rel)] == [("replay", "lock_company", 2)]
