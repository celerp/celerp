# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Census of the doors into billing eligibility, read from the code.

The rule it serves: an agreement's stored status decides whether a period can bill, by one
allow-list predicate (billing.billable) that the preview and every writer read.

- ``billing_doors()``: every call in the code of the functions that bill a period
  (billing.claim_period, billing._bill), with the route of the function that makes it. A call
  inside a savepoint whose block ends by raising keeps nothing, so it is not a door.
- ``reads_rule()``: whether a function reaches billing.billable through the package's own calls.
- ``status_writers()``: every route that writes an agreement's status: the Documents status routes
  (refused by billing.guard_agreement_status unless the agreement keeps a lifecycle status) and
  the module's own routes.
- ``status_values()``: every status those writers can store: each constant the Documents
  projection gives ``current["status"]`` (directly, as a default, or through the status helpers
  it calls) and each status the module writes.

Run as a script for the census table: ``python eligibility_census.py``.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(p) for p in (HERE, HERE.parents[2], HERE.parent, HERE.parents[1] / "celerp-docs")]

from consent_census import route_doors, status_routes  # noqa: E402
from owned_fields_census import PACKAGE, ROOT, _callee, _sources  # noqa: E402

BILLERS = ("claim_period", "_bill")
RULE = "billable"
PROJECTION = ROOT / "default_modules" / "celerp-docs" / "celerp_docs" / "doc_projections.py"


def _own_calls(fn) -> list[ast.Call]:
    """The calls in ``fn``'s own body, not in the functions it defines."""
    found, todo = [], list(ast.iter_child_nodes(fn))
    while todo:
        n = todo.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call):
            found.append(n)
        todo += ast.iter_child_nodes(n)
    return found


def _unkept_calls(fn) -> set[int]:
    """The calls inside a ``savepoint`` block that ends by raising: rolled back, so nothing kept."""
    return {id(c) for w in ast.walk(fn) if isinstance(w, (ast.With, ast.AsyncWith))
            and any(isinstance(i.context_expr, ast.Call) and _callee(i.context_expr) == "savepoint" for i in w.items)
            and isinstance(w.body[-1], ast.Raise)
            for c in ast.walk(w) if isinstance(c, ast.Call)}


def _route(fn) -> str | None:
    for d in fn.decorator_list:
        if (isinstance(d, ast.Call) and getattr(d.func, "attr", None) in ("get", "post", "put", "patch", "delete")
                and d.args and isinstance(d.args[0], ast.Constant)):
            return f"{d.func.attr.upper()} {d.args[0].value}"
    return None


def billing_doors() -> dict[str, tuple[str, str | None]]:
    """``{path::function: (the biller it calls, its route or None)}`` for every caller."""
    found = {}
    for path, tree in _sources():
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            unkept = _unkept_calls(fn)
            for call in _own_calls(fn):
                if _callee(call) in BILLERS and _callee(call) != fn.name and id(call) not in unkept:
                    found[f"{path.relative_to(ROOT)}::{fn.name}"] = (_callee(call), _route(fn))
    return found


def _package_functions() -> dict[str, list]:
    funcs: dict[str, list] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault(n.name, []).append(n)
    return funcs


def reads_rule(name: str, *, before: str | None = None) -> bool:
    """``name`` reaches billing.billable through the package's own calls; with ``before``, a
    call that reaches it comes ahead of the first call to ``before`` in ``name``'s body."""
    funcs = _package_functions()

    def reaches(fn_name: str, seen: frozenset) -> bool:
        if fn_name == RULE:
            return True
        return any(reaches(_callee(c), seen | {fn_name}) for f in funcs.get(fn_name, [])
                   for c in _own_calls(f) if _callee(c) in funcs and _callee(c) not in seen)

    for fn in funcs.get(name, []):
        calls = sorted(_own_calls(fn), key=lambda c: (c.lineno, c.col_offset))
        stop = next((c.lineno for c in calls if before and _callee(c) == before), None)
        if any(reaches(_callee(c), frozenset({name})) for c in calls
               if (_callee(c) == RULE or _callee(c) in funcs) and (stop is None or c.lineno < stop)):
            return True
    return False


def guarded(name: str, biller: str) -> bool:
    """A door reads the rule before it bills: itself, or through claim_period, which reads it
    before it calls _bill."""
    return reads_rule(name, before=biller) or (biller == "claim_period" and reads_rule("claim_period", before="_bill"))


def status_writers() -> list[str]:
    own = sorted(f"SUBSCRIPTIONS {label}" for label, keys, _ in route_doors().values() if "status" in keys)
    return own + [f"DOCUMENTS {r}" for r in status_routes()]


def _strings(expr) -> set[str]:
    """The string constants an expression assigned as a status can be: a constant, a choice
    between expressions, a default (``x or "final"``, ``data.get(k, "final")``), or the
    returns of a projection status helper it calls."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.IfExp):
        return _strings(expr.body) | _strings(expr.orelse)
    if isinstance(expr, ast.BoolOp):
        return set().union(*(_strings(v) for v in expr.values))
    if isinstance(expr, ast.Call):
        return set().union(*(_strings(a) for a in expr.args[1:])) | {f"@{_callee(expr)}"}
    return set()


def status_values() -> list[str]:
    tree = ast.parse(PROJECTION.read_text(encoding="utf-8"))
    helpers = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    found = set()
    for n in ast.walk(helpers["apply_documents_event"]):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Subscript) and getattr(t.slice, "value", None) == "status" for t in n.targets):
            found |= _strings(n.value)
    done = set()
    while todo := {s for s in found if s.startswith("@") and s not in done}:
        for ref in todo:
            done.add(ref)
            if (fn := helpers.get(ref[1:])) is not None:
                for r in (r for r in ast.walk(fn) if isinstance(r, ast.Return) and r.value is not None):
                    found |= _strings(r.value)
    found = {s for s in found if not s.startswith("@")}
    for path in sorted(PACKAGE.glob("*.py")):
        for d in (n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Dict)):
            found |= {v.value for k, v in zip(d.keys, d.values)
                      if isinstance(k, ast.Constant) and k.value == "status"
                      and isinstance(v, ast.Constant) and isinstance(v.value, str)}
    return sorted(found)


if __name__ == "__main__":
    doors = billing_doors()
    print(f"# callers of {', '.join(BILLERS)}: {len(doors)}")
    for name, (biller, route) in sorted(doors.items()):
        print(f"DOOR {name} calls={biller} route={route} reads_rule={guarded(name.split('::')[1], biller)}")
    writers = status_writers()
    print(f"# routes that write an agreement's status: {len(writers)}")
    for w in writers:
        print(f"WRITER {w}")
    values = status_values()
    print(f"# statuses those writers can store: {len(values)}")
    for v in values:
        print(f"STATUS {v}")
