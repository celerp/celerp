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


_VOIDED = "acc.journal_entry.voided"
_UNDO_HELPERS = frozenset({"_void_je_if_posted"})


class _Undo(ast.NodeVisitor):
    """Each function that voids an entry (a voided event, or the per-entry void helper), with
    every name its outermost function calls."""

    def __init__(self, module: str):
        self.module, self.stack = module, []
        self.undo: set[str] = set()
        self.calls: dict[str, set[str]] = {}

    def _function(self, node) -> None:
        self.stack.append(node.name)
        if len(self.stack) == 1:
            self.calls[node.name] = {_name(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)}
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        value = _event_type(node)
        if self.stack and ((isinstance(value, ast.Constant) and value.value == _VOIDED)
                           or _name(node.func) in _UNDO_HELPERS):
            self.undo.add(":".join((self.module, ".".join(self.stack))))
        self.generic_visit(node)



def undo_census(repo: pathlib.Path = REPO, roots=APP_ROOTS, extra=()) -> dict[str, set[str]]:
    """Every function that voids a journal entry, plus ``extra`` (the producers the fund rule
    table says reverse one) -> the names its outermost function calls (a nested helper is
    judged by the function holding it)."""
    out: dict[str, set[str]] = {}
    for module, path in sources(repo, roots):
        visitor = _Undo(module)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        for name in visitor.undo | {n for n in extra if n.split(":", 1)[0] == module}:
            outer = name.split(":", 1)[1].split(".", 1)[0]
            out[name] = visitor.calls.get(outer, set())
    for helper in _UNDO_HELPERS:
        out.pop(f"{_AUTO_JE}:{helper}", None)
    return out


_VOIDS = frozenset({"_void_je_if_posted", "void_record_postings", "reverse_journal"})
_POSTS = frozenset({"_emit_auto_posted_je", "repost_record_postings"})


def _leaf(qualified: str) -> str:
    return qualified.rsplit(":", 1)[1].rsplit(".", 1)[-1]


class _Multi(ast.NodeVisitor):
    """Each outermost function that voids or reverses an entry and also posts one, or calls
    repost_record_postings. It voids or reverses through a voided event, a ``reverses`` key or
    a call to a function that voids; it posts through a created event or a call to a producer."""

    def __init__(self, module: str, voids: frozenset[str], posts: frozenset[str]):
        self.module, self.voids, self.posts = module, voids, posts
        self.found: set[str] = set()

    def visit_ClassDef(self, node) -> None:
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._judge(child, f"{node.name}.{child.name}")

    def visit_FunctionDef(self, node) -> None:
        self._judge(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _judge(self, node, name: str) -> None:
        undo = post = repost = False
        for c in ast.walk(node):
            if isinstance(c, ast.Call):
                called, value = _name(c.func), _event_type(c)
                voided = isinstance(value, ast.Constant) and value.value == _VOIDED
                undo |= voided or called in self.voids
                post |= (_journal(c) and not voided) or called in self.posts
                repost |= called == "repost_record_postings"
            elif isinstance(c, ast.Dict):
                undo |= any(isinstance(k, ast.Constant) and k.value == "reverses" for k in c.keys)
        if (undo and post) or repost:
            self.found.add(f"{self.module}:{name}")


_MULTI_HELPERS = frozenset({f"{_AUTO_JE}:repost_record_postings", f"{_AUTO_JE}:void_record_postings"})


def multi_entry_census(repo: pathlib.Path = REPO, roots=APP_ROOTS) -> set[str]:
    """Every function that both voids or reverses a journal entry and posts one in the same
    call, directly or through a function that does, or calls repost_record_postings (the
    void and post helpers themselves left out)."""
    producers, _ = census(repo, roots)
    voiders = set(undo_census(repo, roots))
    voids = _VOIDS | {_leaf(n) for n in voiders}
    posts = _POSTS | {_leaf(n) for n in producers - voiders}
    found: set[str] = set()
    for module, path in sources(repo, roots):
        visitor = _Multi(module, frozenset(voids), frozenset(posts))
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found |= visitor.found
    return found - _MULTI_HELPERS
