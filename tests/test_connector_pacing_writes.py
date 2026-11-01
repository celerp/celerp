# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""One pacing authority: execution._pace is the only code that writes when deferred
connector work is tried again, or how many times it was tried.

Read from the source, not from a list of loops: every SQL statement, ORM write and
attribute assignment in the connectors that sets an attempt or retry-time column (a
column of execution.PACED) must be inside _pace, and every such column of a connector
table is in PACED, so a new loop that schedules its own retries fails here."""
from __future__ import annotations

import ast
import re
from pathlib import Path

from celerp.connectors.execution import PACED

_ROOT = Path(__file__).resolve().parents[1]
_SOURCES = [*(_ROOT / "celerp" / "connectors").rglob("*.py"),
            *(_ROOT / "default_modules" / "celerp-connectors" / "celerp_connectors").rglob("*.py")]
_COLUMNS = {column for paced in PACED.values() for column in (paced.attempts, paced.retry_at)}
_SQL_WRITE = re.compile(r"\b(" + "|".join(sorted(_COLUMNS)) + r")\s*=(?!=)")


def _writes(tree: ast.AST) -> list[tuple[int, str, str]]:
    """(line, enclosing function, what) of each write to a pacing column."""
    found: list[tuple[int, str, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if _SQL_WRITE.search(text) or ("INSERT" in text.upper() and any(c in text for c in _COLUMNS)):
                found.append((node.lineno, function, f"SQL {text.strip()[:60]!r}"))
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and _SQL_WRITE.search(str(part.value)):
                    found.append((node.lineno, function, f"SQL {str(part.value).strip()[:60]!r}"))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in _COLUMNS:
                    found.append((node.lineno, function, f"{keyword.arg}="))
            if getattr(node.func, "attr", None) == "values":
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        for key in arg.keys:
                            name = getattr(key, "value", None) or getattr(key, "attr", None)
                            if name in _COLUMNS:
                                found.append((node.lineno, function, f"values {name}"))
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for item in ast.walk(target):
                    if isinstance(item, ast.Attribute) and item.attr in _COLUMNS:
                        found.append((node.lineno, function, f".{item.attr} ="))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "")
    return found


def test_only_pace_writes_an_attempt_or_retry_time():
    outside = [f"{path.relative_to(_ROOT)}:{line} in {function or 'module'}: {what}"
               for path in _SOURCES for line, function, what in _writes(ast.parse(path.read_text()))
               if not (path.name == "execution.py" and function == "_pace")]
    assert not outside, "pacing written outside execution._pace:\n" + "\n".join(outside)


def test_the_guard_sees_each_kind_of_write():
    source = '''
def schedule(row, s):
    s.execute(sa.text("UPDATE outbound_queue SET next_retry_at = now()"))
    s.execute(sa.update(T).values(retry_count=1))
    s.execute(sa.update(T).values({T.next_retry_at: None}))
    row.next_attempt_at = None
    s.execute(sa.text(f"UPDATE {t} SET rerun_attempts = 0"))
    s.execute(sa.text("INSERT INTO t (rerun_retry_at) VALUES (now())"))
'''
    assert len(_writes(ast.parse(source))) == 6
    assert not _writes(ast.parse('s.execute(sa.text("SELECT 1 WHERE next_retry_at <= now()"))'))


def test_every_attempt_or_retry_column_of_a_connector_table_is_paced():
    import celerp.models  # noqa: F401 - every model registered
    from celerp.models.base import Base

    unpaced = sorted(f"{table.name}.{column.name}" for table in Base.metadata.tables.values()
                     if table.name.startswith("connector_") or table.name == "outbound_queue"
                     for column in table.columns
                     if re.search(r"retry|attempt", column.name) and column.name not in PACED.get(table.name, ())[:2])
    assert not unpaced, f"connector pacing columns missing from execution.PACED: {unpaced}"
    for table, paced in PACED.items():
        assert {paced.attempts, paced.retry_at} <= set(Base.metadata.tables[table].columns.keys()), table
