# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Census of the code that reads a period's send record, and of the states it can hold, read
from the code.

A period's email has one reader for display and for Send again: ``billing.send_status`` (with
``sends_again`` for whether a send is a new message). Everything else shows what it answers.

- ``readers()``: every function in the code that touches a send record or the status read from
  it, with how: ``raw`` (reads the stored ``state`` of a send record), ``status`` (calls
  send_status), ``shows`` (reads the ``send`` key of a row built from send_status, or of a send
  record without its state), ``writes`` (calls record_send or builds a send record).
- ``raw_outside_owner()``: the ``raw`` readers outside billing.py that are not History
  describers (a History row describes the change an event recorded, not the email as it reads
  now). Each one would show a stored state send_status does not decide, so it must be empty.
- ``written_states()``: every state the code can store in a send record (record_send's
  ``state`` and send record literals), so each one can be passed through send_status.

Run as a script for the census table: ``python send_census.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OWNER = ROOT / "default_modules" / "celerp-subscriptions" / "celerp_subscriptions" / "billing.py"
SOURCE_TOPS = ("celerp", "ui", "default_modules", "premium_modules")
STATUS_READERS = {"send_status"}
# What turns a status into what the page shows: whether Send again is a new message, the send
# button, and the tables keyed by status or summary kind (each named _SEND...).
SHOWERS = {"sends_again", "_send_button"}
WRITER = "record_send"


def _sources():
    for top in SOURCE_TOPS:
        if (ROOT / top).is_dir():
            for path in sorted((ROOT / top).rglob("*.py")):
                if "tests" not in path.parts:
                    yield path, ast.parse(path.read_text(encoding="utf-8"))


def _key(node) -> str | None:
    """The constant key ``node`` reads: ``x["k"]`` or ``x.get("k", ...)``."""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value if isinstance(node.slice.value, str) else None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return node.args[0].value
    return None


def _target(node):
    """What ``node`` reads its key from."""
    return node.value if isinstance(node, ast.Subscript) else node.func.value


def _holds_send(expr, names: set[str]) -> bool:
    """Whether ``expr`` evaluates to a send record: a ``send`` key read, a name bound to one,
    or either behind ``or {}`` / ``dict(...)``."""
    if isinstance(expr, ast.Name):
        return expr.id in names
    if isinstance(expr, ast.BoolOp):
        return any(_holds_send(v, names) for v in expr.values)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "dict" and expr.args:
        return _holds_send(expr.args[0], names)
    return _key(expr) == "send"


def _callee(call: ast.Call) -> str | None:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _own_nodes(fn):
    """The nodes of ``fn`` outside the functions nested in it."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _tables(tree) -> set[str]:
    """The module's tables keyed by send status or summary kind: dicts named _SEND..."""
    return {n.targets[0].id for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name) and n.targets[0].id.startswith("_SEND")
            and isinstance(n.value, ast.Dict)}


def _read(fn, tables: set[str]) -> set[str]:
    """How ``fn`` touches a send record (see the module docstring)."""
    nodes = list(_own_nodes(fn))
    names: set[str] = set()
    for _ in range(3):  # names bound to names bound to a send record
        for node in nodes:
            if (isinstance(node, ast.Assign) and _holds_send(node.value, names)
                    and all(isinstance(t, ast.Name) for t in node.targets)):
                names |= {t.id for t in node.targets}
    how = set()
    for node in nodes:
        key = _key(node)
        if key == "state" and _holds_send(_target(node), names):
            how.add("raw")
        elif key == "send":
            how.add("shows")
        if isinstance(node, ast.Call) and _callee(node) in STATUS_READERS:
            how.add("status")
        if ((isinstance(node, ast.Call) and _callee(node) in SHOWERS)
                or (isinstance(node, ast.Name) and node.id in tables)):
            how.add("shows")
        if isinstance(node, ast.Call) and _callee(node) == WRITER:
            how.add("writes")
        if isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "send"
                                              and isinstance(v, ast.Dict) for k, v in zip(node.keys, node.values)):
            how.add("writes")
    return how


def _describers(tree) -> set[str]:
    """The History describers a file registers (register_fields), with what they call in it."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node) == "register_fields" and node.args:
            roots |= {n.id for n in ast.walk(node.args[0]) if isinstance(n, ast.Name)}
    calls = {fn.name: {_callee(c) for c in ast.walk(fn) if isinstance(c, ast.Call)}
             for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found, todo = set(), list(roots)
    while todo:
        name = todo.pop()
        if name in found or name not in calls:
            continue
        found.add(name)
        todo += [c for c in calls[name] if c]
    return found


def readers() -> list[tuple[str, str, str, str]]:
    """[(file:line, function, how, owner)] for every function that touches a send record;
    owner is "billing" (the owning module), "history" (a History describer) or ""."""
    rows = []
    for path, tree in _sources():
        describers, tables = _describers(tree), _tables(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            how = _read(fn, tables)
            if not how:
                continue
            owner = "billing" if path == OWNER else "history" if fn.name in describers else ""
            rows.append((f"{path.relative_to(ROOT)}:{fn.lineno}", fn.name, ",".join(sorted(how)), owner))
    return rows


def raw_outside_owner() -> list[tuple[str, str]]:
    return [(where, fn) for where, fn, how, owner in readers() if "raw" in how.split(",") and not owner]


def _strings(expr) -> set[str]:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.IfExp):
        return _strings(expr.body) | _strings(expr.orelse)
    return set()


def written_states() -> set[str]:
    """Every state a send record can be stored with; a name the census cannot read is kept as
    ``?name`` so it shows."""
    states: set[str] = set()
    for _path, tree in _sources():
        assigned = {n.targets[0].id: n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
        for node in ast.walk(tree):
            exprs = []
            if isinstance(node, ast.Call) and _callee(node) == WRITER:
                exprs = [k.value for k in node.keywords if k.arg == "state"]
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "send" and isinstance(v, ast.Dict):
                        exprs += [sv for sk, sv in zip(v.keys, v.values)
                                  if isinstance(sk, ast.Constant) and sk.value == "state"]
            for expr in exprs:
                if isinstance(expr, ast.Name):
                    found = _strings(assigned.get(expr.id)) if expr.id in assigned else set()
                    states |= found or {f"?{expr.id}"}
                else:
                    states |= _strings(expr) or {f"?{ast.unparse(expr)}"}
    return states


if __name__ == "__main__":
    rows = readers()
    print("# Send records: every function that touches one (how: raw = reads its stored state,")
    print("# status = calls send_status, shows = reads a row's send key, writes = stores one)")
    for where, fn, how, owner in rows:
        print(f"READER\t{where}\t{fn}\t{how}\t{owner or '-'}")
    print(f"WRITTEN_STATES\t{','.join(sorted(written_states()))}")
    raw = raw_outside_owner()
    print(f"RAW_OUTSIDE_OWNER\t{len(raw)}\t{raw}")
    print(f"# {len(rows)} readers")
