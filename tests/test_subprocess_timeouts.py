# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A unit or process test runs under the global --timeout of pyproject's addopts. One that
waits on a subprocess longer than that is killed by the global timeout first, so the wait it
asks for never happens: such a test carries a timeout mark at least as long as its longest
wait. The browser and integration jobs set their own --timeout and are not judged here."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The calls that wait on a subprocess, and how long, when given a timeout literal.
_WAITS = {("subprocess", "run"), ("subprocess", "check_output"), ("subprocess", "call")}
_WAIT_METHODS = {"wait", "communicate"}
_OWN_TIMEOUT = {"browser", "integration"}


def _global_timeout() -> float:
    addopts = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["addopts"]
    return float(re.search(r"--timeout=(\d+)", addopts).group(1))


def _mark(node: ast.expr) -> tuple[str, ast.Call | None] | None:
    """The name of a ``pytest.mark.<name>`` or ``pytest.mark.<name>(...)``, with its call."""
    call = node if isinstance(node, ast.Call) else None
    target = call.func if call else node
    if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Attribute)
            and target.value.attr == "mark" and isinstance(target.value.value, ast.Name)
            and target.value.value.id == "pytest"):
        return target.attr, call
    return None


def _marks(nodes) -> list[tuple[str, ast.Call | None]]:
    found = []
    for node in nodes:
        items = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node]
        found += [m for item in items if (m := _mark(item)) is not None]
    return found


def _pytestmark(body) -> list[tuple[str, ast.Call | None]]:
    return _marks(stmt.value for stmt in body if isinstance(stmt, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets))


def _timeout_of(call: ast.Call | None, where: str) -> float:
    """The mark's timeout, which must be a literal so the guard can always read it."""
    arg = (call.args[:1] or [k.value for k in call.keywords if k.arg == "timeout"]) if call else []
    assert arg and isinstance(arg[0], ast.Constant) and isinstance(arg[0].value, (int, float)), \
        f"{where}: give the timeout mark a number literal"
    return float(arg[0].value)


def _waits(fn: ast.AST) -> tuple[list[float], set[str]]:
    """The subprocess wait literals a function's own body gives, and the names it calls or takes
    as fixtures (a fixture's setup runs under the timeout of the test that requests it)."""
    longest, called = [], set()
    for node in ast.walk(fn):
        if isinstance(node, ast.arg):
            called.add(node.arg)
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
            continue
        if not isinstance(func, ast.Attribute):
            continue
        waits = func.attr in _WAIT_METHODS or (
            isinstance(func.value, ast.Name) and (func.value.id, func.attr) in _WAITS)
        for k in node.keywords if waits else ():
            if k.arg == "timeout" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, (int, float)):
                longest.append(float(k.value.value))
    return longest, called


def _uncovered(path: Path, limit: float) -> list[str]:
    tree = ast.parse(path.read_text(), str(path))
    rel = path.relative_to(ROOT).as_posix()
    functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    own = {name: _waits(fn) for name, fn in functions.items()}

    def reach(name: str, seen: set[str]) -> float:
        seen.add(name)
        waits, called = own[name]
        return max([0.0, *waits, *(reach(c, seen) for c in called if c in own and c not in seen)])

    module = _pytestmark(tree.body)
    tests = [(fn, []) for fn in functions.values() if fn.name.startswith("test_")]
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name.startswith("Test")):
        scope = _marks(cls.decorator_list) + _pytestmark(cls.body)
        tests += [(fn, scope) for fn in cls.body
                  if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test_")]
    bad = []
    for fn, scope in tests:
        marks = module + scope + _marks(fn.decorator_list)
        timeouts = [_timeout_of(call, f"{rel}::{fn.name}") for name, call in marks if name == "timeout"]
        if any(name in _OWN_TIMEOUT for name, _ in marks):
            continue
        waits, called = own.get(fn.name) or _waits(fn)
        longest = max([0.0, *waits, *(reach(c, {fn.name}) for c in called if c in own)])
        # The nearest mark wins: the function's own, then its class's, then the module's.
        allowed = timeouts[-1] if timeouts else limit
        if longest > allowed:
            bad.append(f"{rel}::{fn.name} waits {longest:g} s under a {allowed:g} s timeout")
    return bad


def test_subprocess_tests_carry_a_covering_timeout():
    limit = _global_timeout()
    files = sorted({*ROOT.glob("tests/**/test_*.py"), *ROOT.glob("default_modules/*/tests/**/test_*.py")})
    bad = [line for path in files if "test_browser" not in path.parts for line in _uncovered(path, limit)]
    assert bad == [], "mark these with pytest.mark.timeout(N), N at least the wait:\n" + "\n".join(bad)
