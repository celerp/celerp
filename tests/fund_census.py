# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The census of every journal producer and every variable event emitter in the app's source.

A producer is a function that posts or voids a journal entry: it calls _emit_auto_posted_je,
passes ``event_type`` acc.journal_entry.created or acc.journal_entry.voided (a literal or the
JOURNAL_CREATED / ACC_JOURNAL_ENTRY_CREATED constants) to any call, or is an ``async def
create_for_*`` in auto_je. A variable emitter calls emit_event with an ``event_type`` that is not
a literal (a name, attribute, call, f-string or a ``**`` spread), so it is not provably
non-journal. Each call counts for the innermost function holding it, named
``module.path:Outer.inner``."""

from __future__ import annotations

import ast
import pathlib

from shipped_sources import APP_ROOTS, MODULE_ROOT, REPO

JOURNAL_EVENTS = frozenset({"acc.journal_entry.created", "acc.journal_entry.voided"})
_CONSTANTS = frozenset({"JOURNAL_CREATED", "ACC_JOURNAL_ENTRY_CREATED"})
_AUTO_JE = "celerp.services.auto_je"


def _name(node: ast.AST) -> str:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""


def _event_type(call: ast.Call) -> ast.AST | None:
    return next((kw.value for kw in call.keywords if kw.arg == "event_type"), None)


def _journal(call: ast.Call) -> bool:
    value = _event_type(call)
    return (isinstance(value, ast.Constant) and value.value in JOURNAL_EVENTS) or (
        isinstance(value, (ast.Name, ast.Attribute)) and _name(value) in _CONSTANTS)


def _variable(call: ast.Call) -> bool:
    value = _event_type(call)
    if value is None:
        return any(kw.arg is None for kw in call.keywords) or len(call.args) > 1
    return not isinstance(value, ast.Constant) and _name(value) not in _CONSTANTS


class _Census(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module, self.stack = module, []
        self.producers: set[str] = set()
        self.variable: set[str] = set()

    def _function(self, node) -> None:
        self.stack.append(node.name)
        if (self.module == _AUTO_JE and isinstance(node, ast.AsyncFunctionDef) and len(self.stack) == 1
                and node.name.startswith("create_for_")):
            self.producers.add(self._here())
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _name(node.func)
        if name == "_emit_auto_posted_je" or _journal(node):
            self.producers.add(self._here())
        elif name == "emit_event" and _variable(node):
            self.variable.add(self._here())
        self.generic_visit(node)

    def _here(self) -> str:
        return f"{self.module}:{'.'.join(self.stack) or '<module>'}"


def sources(repo: pathlib.Path = REPO, roots=APP_ROOTS):
    """(module name, path) for every Python file under ``roots``, tests left out."""
    for root in roots:
        for path in sorted((repo / root).rglob("*.py")):
            rel = path.relative_to(repo)
            if any(p in {"tests", "__pycache__"} or p.startswith("test_") for p in rel.parts):
                continue
            parts = rel.with_suffix("").parts
            # A bundled module imports from inside its own directory: default_modules/<dir>/<package>.
            yield ".".join(parts[2:] if root == MODULE_ROOT else parts), path


def census(repo: pathlib.Path = REPO, roots=APP_ROOTS) -> tuple[set[str], set[str]]:
    """(journal producers, variable event emitters), each a set of qualified function names.
    _emit_auto_posted_je itself is the posting helper, not a producer."""
    producers: set[str] = set()
    variable: set[str] = set()
    for module, path in sources(repo, roots):
        visitor = _Census(module)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        producers |= visitor.producers
        variable |= visitor.variable
    producers.discard(f"{_AUTO_JE}:_emit_auto_posted_je")
    return producers, variable - producers
