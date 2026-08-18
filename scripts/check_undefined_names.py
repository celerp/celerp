# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Static checker for undefined names (NameError at runtime).

pyflakes and ruff stop reporting undefined names (F821) in any module that does
`from x import *`, because the star import could in principle bind the name.
Every UI route and component does `from fasthtml.common import *`, so that whole
layer has no undefined-name coverage from the normal linters. Issue 284 shipped
exactly there: a route referenced a constant that did not exist and 500'd the
moment the form opened.

This checker closes that gap. It resolves star exports by importing the named
module and reading `__all__` (or its public names), does proper per-scope binding
collection with a forward pass so global references defined later still resolve,
skips annotation subtrees (the repo uses `from __future__ import annotations`, so
annotations are strings and never raise), and reports every load of a name that
cannot resolve at runtime. It fails safe: if a star-import source cannot be
imported, that module's reports are suppressed rather than guessed at.

Usage: python scripts/check_undefined_names.py [<pkg_dir> ...]   (default: ui celerp scripts)
Exit 1 if any undefined load is found.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import sys
from pathlib import Path

BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__annotations__", "__dict__", "__class__",
}

DEFAULT_ROOTS = ["ui", "celerp", "scripts"]


def star_exports(module_name: str) -> set[str]:
    """Names `from module import *` would bind. Best-effort: import the module."""
    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return set()
    if hasattr(mod, "__all__"):
        return set(mod.__all__)
    return {n for n in dir(mod) if not n.startswith("_")}


class Scope:
    def __init__(self, parent, kind):
        self.parent = parent      # lexical parent Scope or None
        self.kind = kind          # 'module' | 'function' | 'class' | 'comp'
        self.names: set[str] = set()
        self.star = False         # saw an `import *` whose source could not resolve


class ScopeBuilder(ast.NodeVisitor):
    """Pass 1: collect the names bound in a scope, without descending into child
    function/class/comprehension scopes (each gets its own build)."""

    def __init__(self, scope: Scope):
        self.scope = scope

    def visit_FunctionDef(self, node):
        self.scope.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.scope.names.add(node.name)

    def visit_Lambda(self, node):
        pass

    def visit_ListComp(self, node): pass
    def visit_SetComp(self, node): pass
    def visit_DictComp(self, node): pass
    def visit_GeneratorExp(self, node): pass

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            self.scope.names.add(node.id)

    def visit_Global(self, node):
        for n in node.names:
            self.scope.names.add(n)

    def visit_Nonlocal(self, node):
        for n in node.names:
            self.scope.names.add(n)

    def visit_Import(self, node):
        for a in node.names:
            self.scope.names.add((a.asname or a.name).split(".")[0])

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.name == "*":
                exports = star_exports(node.module or "")
                if exports:
                    self.scope.names |= exports
                else:
                    self.scope.star = True
            else:
                self.scope.names.add(a.asname or a.name)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.scope.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.scope.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node):
        if node.name:
            self.scope.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchMapping(self, node):
        if node.rest:
            self.scope.names.add(node.rest)
        self.generic_visit(node)


def build_scope_names(body_nodes, scope: Scope):
    b = ScopeBuilder(scope)
    for n in body_nodes:
        b.visit(n)


class Checker:
    def __init__(self, path: Path):
        self.path = path
        self.errors: list[tuple[int, str]] = []

    def resolve(self, name: str, scope: Scope) -> bool:
        s = scope
        saw_star = False
        while s is not None:
            if s.star:
                saw_star = True
            if name in s.names:
                return True
            s = s.parent
        return name in BUILTINS or saw_star

    def check_body(self, body, scope: Scope):
        build_scope_names(body, scope)
        for stmt in body:
            self.visit(stmt, scope)

    def visit(self, node, scope: Scope):
        # Annotation subtrees are strings under `from __future__ import
        # annotations` and never raise NameError, so skip them.
        if isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self.visit(node.value, scope)
            self.visit(node.target, scope)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._visit_function(node, scope)
            return
        if isinstance(node, ast.ClassDef):
            self._visit_class(node, scope)
            return
        if isinstance(node, ast.Lambda):
            self._visit_lambda(node, scope)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            self._visit_comp(node, scope)
            return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and not self.resolve(node.id, scope):
                self.errors.append((node.lineno, node.id))
            return
        for child in ast.iter_child_nodes(node):
            self.visit(child, scope)

    def _visit_function(self, node, scope):
        # Defaults and decorators evaluate in the enclosing scope.
        for d in node.decorator_list:
            self.visit(d, scope)
        for d in (node.args.defaults + [x for x in node.args.kw_defaults if x]):
            self.visit(d, scope)
        fscope = Scope(scope, "function")
        a = node.args
        for arg in (a.posonlyargs + a.args + a.kwonlyargs):
            fscope.names.add(arg.arg)
        if a.vararg:
            fscope.names.add(a.vararg.arg)
        if a.kwarg:
            fscope.names.add(a.kwarg.arg)
        self.check_body(node.body, fscope)

    def _visit_lambda(self, node, scope):
        for d in (node.args.defaults + [x for x in node.args.kw_defaults if x]):
            self.visit(d, scope)
        lscope = Scope(scope, "function")
        a = node.args
        for arg in (a.posonlyargs + a.args + a.kwonlyargs):
            lscope.names.add(arg.arg)
        if a.vararg:
            lscope.names.add(a.vararg.arg)
        if a.kwarg:
            lscope.names.add(a.kwarg.arg)
        self.visit(node.body, lscope)

    def _visit_class(self, node, scope):
        for d in node.decorator_list:
            self.visit(d, scope)
        for b in node.bases:
            self.visit(b, scope)
        for k in node.keywords:
            self.visit(k.value, scope)
        cscope = Scope(scope, "class")
        self.check_body(node.body, cscope)

    def _visit_comp(self, node, scope):
        cscope = Scope(scope, "comp")
        for gen in node.generators:
            for n in ast.walk(gen.target):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    cscope.names.add(n.id)
        for i, gen in enumerate(node.generators):
            # The first iterable evaluates in the enclosing scope.
            self.visit(gen.iter, scope if i == 0 else cscope)
            for cond in gen.ifs:
                self.visit(cond, cscope)
        if isinstance(node, ast.DictComp):
            self.visit(node.key, cscope)
            self.visit(node.value, cscope)
        else:
            self.visit(node.elt, cscope)


def find_undefined(roots) -> list[tuple[Path, int, str]]:
    """Return (path, lineno, name) for every undefined load under the given roots."""
    findings: list[tuple[Path, int, str]] = []
    for d in roots:
        root = Path(d).resolve()
        for path in sorted(root.rglob("*.py")):
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src, filename=str(path))
            except SyntaxError as e:
                findings.append((path, getattr(e, "lineno", 0) or 0, f"SYNTAX ERROR {e}"))
                continue
            mscope = Scope(None, "module")
            mscope.names |= {"__file__", "__name__", "__doc__", "__package__",
                             "__spec__", "__loader__", "__builtins__"}
            c = Checker(path)
            c.check_body(tree.body, mscope)
            for lineno, name in c.errors:
                findings.append((path, lineno, name))
    return findings


def main(argv) -> int:
    roots = argv or DEFAULT_ROOTS
    findings = find_undefined(roots)
    for path, lineno, name in findings:
        print(f"{path}:{lineno}: undefined name '{name}'")
    print(f"\n== {len(findings)} undefined-name load(s) found ==")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
