# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""What the bundled source can reach, read from its syntax alone.

A call graph over celerp/, default_modules/ and ui/: each function, the places it writes
or takes a company's lock itself, and the calls it makes that resolve by name to another
function of the source (a function in the same scope or module, one imported by name, a
function of an imported module, a method of the same class). `reaches` follows those
calls to their end, so a guard sees a lock or a write however many wrappers stand between.

A call on any other object (a method of an argument, a callable passed in) does not
resolve: the graph reports what the source names, never a guess.

Standard library only, so the census job reads it without the app installed."""

from __future__ import annotations

import ast
import os
from collections import ChainMap
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = ("celerp", "default_modules", "ui")
_SKIP_PARTS = ("/tests/", "/migrations/", "/__pycache__/")
# The primitives that take a company's lock (celerp/services/company_lock.py).
TAKES_THE_LOCK = frozenset({"lock_company", "locked_company", "lock_projections", "hold_company",
                            "lock_company_for_deletion"})
MUTATING = frozenset({"post", "put", "patch", "delete"})
_CLIENTS = frozenset({"c", "client", "_c", "http"})  # names the UI gives an HTTP client into the API
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass
class Function:
    module: str
    qualname: str
    path: str  # relative to the repository
    first: int  # the line its source starts on: its first decorator, else its def
    node: ast.AST = field(repr=False)
    effects: list[tuple[int, str]] = field(default_factory=list)  # (line, what) it does itself
    calls: list[tuple[int, str]] = field(default_factory=list)  # (line, the key of a function it calls)
    scope: ChainMap = field(default_factory=ChainMap, repr=False)  # the names visible in it
    cls: str | None = None  # the class it is a method of

    @property
    def key(self) -> str:
        return f"{self.module}.{self.qualname}"


def _called(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _receiver(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        value = func.value
        if isinstance(value, ast.Attribute):
            return value.attr
        if isinstance(value, ast.Name):
            return value.id
    return ""


def effect(node: ast.Call) -> str | None:
    """What a call does itself: takes a company's lock, writes through a session, commits,
    emits an event, or sends a mutating request to the API."""
    name, receiver = _called(node), _receiver(node)
    if name in TAKES_THE_LOCK:
        return f"lock {name}"
    if name == "emit_event":
        return "write emit_event"
    if name == "commit" and isinstance(node.func, ast.Attribute):
        return "commit"
    if name in ("add", "add_all", "delete", "merge") and "session" in receiver.lower():
        return f"write session.{name}"
    if name == "execute" and "session" in receiver.lower() and node.args \
            and isinstance(node.args[0], ast.Call) and _called(node.args[0]) in ("insert", "update", "delete"):
        return f"write {_called(node.args[0])}"
    if name in MUTATING and receiver in _CLIENTS:
        return f"api {name.upper()}"
    if name == "request" and receiver in _CLIENTS and node.args and isinstance(node.args[0], ast.Constant) \
            and str(node.args[0].value).lower() in MUTATING:
        return f"api {str(node.args[0].value).upper()}"
    return None


def _module_name(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[0] == "default_modules":
        parts = parts[2:]  # default_modules/<module>/<package>/...
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or rel


def _python_files():
    for top in SOURCES:
        for folder, _, files in os.walk(os.path.join(ROOT, top)):
            for name in files:
                path = os.path.join(folder, name)
                if name.endswith(".py") and not any(p in path + "/" for p in _SKIP_PARTS):
                    yield os.path.relpath(path, ROOT)


class Graph:
    def __init__(self, files=None) -> None:
        self.functions: dict[str, Function] = {}
        self.by_source: dict[tuple[str, int], Function] = {}  # (path, first line) -> function
        self._modules: dict[str, dict] = {}  # module -> {"names": {name: key or module}, "path": rel}
        trees = {}
        for rel in files if files is not None else _python_files():
            try:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                    trees[rel] = ast.parse(fh.read())
            except (SyntaxError, UnicodeDecodeError):
                continue
        for rel, tree in trees.items():
            self._index(rel, tree)
        for function in self.functions.values():
            self._link(function)

    # -- indexing --------------------------------------------------------------

    def _index(self, rel: str, tree: ast.Module) -> None:
        module = _module_name(rel)
        package = module if rel.endswith("__init__.py") else module.rpartition(".")[0]
        info = self._modules.setdefault(module, {"path": rel, "names": {}, "package": package})

        def walk(body, prefix: str, scope: ChainMap, cls: str | None) -> None:
            for node in body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    self._imports(node, scope, package)
                elif isinstance(node, _FUNCTIONS):
                    qual = f"{prefix}{node.name}"
                    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    # Names resolve when the call runs: its own nested definitions, then
                    # every name its enclosing scopes and its module end up holding.
                    fn = Function(module, qual, rel, first, node, scope=scope.new_child(), cls=cls)
                    self.functions[fn.key] = fn
                    self.by_source[(rel, first)] = fn
                    scope[node.name] = fn.key
                    walk(node.body, f"{qual}.", fn.scope, None)
                elif isinstance(node, ast.ClassDef):
                    walk(node.body, f"{prefix}{node.name}.", scope.new_child(), f"{prefix}{node.name}")
                elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
                    blocks = [node.body, getattr(node, "orelse", []), getattr(node, "finalbody", [])]
                    blocks += [h.body for h in getattr(node, "handlers", [])]
                    for block in blocks:
                        walk(block, prefix, scope, cls)
        walk(tree.body, "", ChainMap(info["names"]), None)

    def _imports(self, node, scope: dict, package: str) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    scope[alias.asname] = ("module", alias.name)
                else:
                    scope[alias.name.split(".")[0]] = ("module", alias.name.split(".")[0])
            return
        base = node.module or ""
        if node.level:
            parts = package.split(".")
            parts = parts[:len(parts) - node.level + 1] if node.level > 1 else parts
            base = ".".join(p for p in (*parts, base) if p)
        for alias in node.names:
            scope[alias.asname or alias.name] = ("from", base, alias.name)

    # -- resolution ------------------------------------------------------------

    def _resolve_name(self, target, depth: int = 0):
        """A scope entry to a function key, a module name, or None."""
        if isinstance(target, str) or target is None or depth > 8:
            return target if isinstance(target, str) else None
        if target[0] == "module":
            return ("module", target[1])
        _, base, name = target
        if f"{base}.{name}" in self._modules:
            return ("module", f"{base}.{name}")
        module = self._modules.get(base)
        if module is None:
            return None
        return self._resolve_name(module["names"].get(name), depth + 1)

    def _attribute(self, module: str, name: str):
        info = self._modules.get(module)
        if info is None:
            return None
        if f"{module}.{name}" in self._modules:
            return ("module", f"{module}.{name}")
        return self._resolve_name(info["names"].get(name))

    def _link(self, fn: Function) -> None:
        own = [n for n in ast.walk(fn.node) if isinstance(n, _FUNCTIONS) and n is not fn.node]
        nested = {id(c) for n in own for c in ast.walk(n)}
        local = dict(fn.scope)
        for node in ast.walk(fn.node):  # a function's own imports are visible in all of it
            if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in nested:
                self._imports(node, local, self._modules[fn.module]["package"])
        for node in ast.walk(fn.node):
            if not isinstance(node, ast.Call) or id(node) in nested:
                continue
            if what := effect(node):
                fn.effects.append((node.lineno, what))
            key = self._callee(node, local, fn)
            if key is not None:
                fn.calls.append((node.lineno, key))

    def _callee(self, node: ast.Call, scope: dict, fn: Function) -> str | None:
        func = node.func
        if isinstance(func, ast.Name):
            found = self._resolve_name(scope.get(func.id))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in ("self", "cls") and fn.cls:
                found = f"{fn.module}.{fn.cls}.{func.attr}"
            else:
                owner = self._resolve_name(scope.get(func.value.id))
                found = self._attribute(owner[1], func.attr) if isinstance(owner, tuple) else None
        else:
            found = None
        return found if isinstance(found, str) and found in self.functions else None

    # -- reachability ----------------------------------------------------------

    def closure(self, starts) -> dict[str, tuple[str, int] | None]:
        """Every function the *starts* reach through calls the graph resolves, each with
        the function and line it is first reached from (None for a start)."""
        parents: dict[str, tuple[str, int] | None] = {key: None for key in starts}
        frontier = list(parents)
        while frontier:
            following = []
            for current in frontier:
                for line, callee in self.functions[current].calls:
                    if callee not in parents:
                        parents[callee] = (current, line)
                        following.append(callee)
            frontier = following
        return parents

    def doing(self, starts, wanted) -> list[list[tuple[str, int, str]]]:
        """Each reached function that does something *wanted* accepts itself, as the chain
        of calls to it: [(function key, line, what it calls or does there), ...]."""
        parents = self.closure(starts)
        found = []
        for key in parents:
            for line, what in self.functions[key].effects:
                if wanted(what):
                    chain, step = [(key, line, what)], key
                    while parents[step] is not None:
                        parent, at = parents[step]
                        chain.insert(0, (parent, at, step))
                        step = parent
                    found.append(chain)
        return found


def describe(chain) -> str:
    return " -> ".join(f"{key.rsplit('.', 1)[-1]}:{line} {what.rsplit('.', 1)[-1]}" for key, line, what in chain)
