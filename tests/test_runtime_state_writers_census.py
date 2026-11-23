# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Every write to the runtime-state row goes through ``lock_runtime_state``.

The row is one JSON dict with several writers; a writer that reads it unlocked and writes the
whole dict back wipes a change another writer committed in between
(tests/test_location_default_notice_lock.py). So outside ``celerp/services/runtime_state.py``
no app module touches the model, no SQL in the app writes the table except the revisions named
below, and every function that replaces a row's ``value`` got that row from the helper."""

from __future__ import annotations

import ast
import re

from shipped_sources import APP_ROOTS, REPO

HELPER_MODULE = "celerp/services/runtime_state.py"
MODEL_MODULES = {"celerp/models/auth.py", "celerp/models/__init__.py"}
# Revisions that write the table: the one that creates it seeds the row, and the one-default
# revision merges its record under an EXCLUSIVE table lock, which waits for any writer holding
# the row FOR UPDATE and makes any later one wait for the revision's commit.
SQL_WRITERS = {
    "celerp/migrations/versions/q2r3s4t5u6v7_add_system_runtime_state.py",
    "celerp/migrations/versions/c4d1e7a2f9b3_one_default_location.py",
}
_SQL_WRITE = re.compile(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE)\s+\"?system_runtime_state", re.I)


def _sources():
    for root in APP_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if not any(p in {"tests", "__pycache__"} or p.startswith("test_") for p in rel.split("/")):
                yield rel, path.read_text(encoding="utf-8")


def _imports(tree: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.ImportFrom) and any(a.name == name for a in n.names) for n in ast.walk(tree))


def _value_writes_off_the_helper(tree: ast.AST) -> list[str]:
    """Functions that assign ``<name>.value`` where <name> is not bound from
    ``await lock_runtime_state(...)`` in the same function."""
    bad = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        locked = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                  and isinstance(n.value, ast.Await) and isinstance(n.value.value, ast.Call)
                  and getattr(n.value.value.func, "id", getattr(n.value.value.func, "attr", "")) == "lock_runtime_state"
                  for t in n.targets if isinstance(t, ast.Name)}
        for n in ast.walk(fn):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AugAssign) else []
            for t in targets:
                if (isinstance(t, ast.Attribute) and t.attr == "value" and isinstance(t.value, ast.Name)
                        and t.value.id not in locked):
                    bad.append(f"{fn.name}:{n.lineno}")
    return bad


def test_only_the_helper_module_touches_the_runtime_state_model():
    offenders = [rel for rel, src in _sources() if rel != HELPER_MODULE and rel not in MODEL_MODULES
                 and _imports(ast.parse(src), "SystemRuntimeState")]
    assert offenders == [], f"use celerp.services.runtime_state (lock_runtime_state to write): {offenders}"


def test_no_app_sql_writes_the_runtime_state_table():
    offenders = [rel for rel, src in _sources() if rel not in SQL_WRITERS and _SQL_WRITE.search(src)]
    assert offenders == [], f"write the runtime state through lock_runtime_state: {offenders}"


def test_the_writing_revision_locks_the_table_before_it_reads():
    src = (REPO / "celerp/migrations/versions/c4d1e7a2f9b3_one_default_location.py").read_text(encoding="utf-8")
    assert src.index('lock_tables(conn, "locations", "system_runtime_state")') < src.index("SELECT value FROM system_runtime_state")


def test_every_value_write_is_on_the_row_the_helper_returned():
    writers = [(rel, src) for rel, src in _sources()
               if rel == HELPER_MODULE or _imports(ast.parse(src), "lock_runtime_state")]
    offenders = {rel: bad for rel, src in writers if (bad := _value_writes_off_the_helper(ast.parse(src)))}
    assert offenders == {}, offenders
    assert {rel for rel, _ in writers} >= {HELPER_MODULE, "celerp/services/location_default_notice.py"}
