# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Census of the doors through which an automatic billing mode can come into force, read from
the code.

A mode comes into force when an agreement becomes active with it chosen, or when a consent term
of a running mode changes. So a door is anything that can write an agreement's ``status`` or a
consent term (billing.CONSENT_TERMS):

- ``route_doors()``: every subscriptions route whose agreement writes, followed through the
  module's own functions to write_agreement, set or drop status or a consent term (an argument
  the census cannot resolve counts as a door).
- ``outside_writes()``: every write of an agreement in the module that does not go through
  write_agreement, with the status and consent-term keys it writes.
- ``marker_writers()``: every function in the code that sets billing's write marker, the mark the
  status guard reads to let the status of an agreement that runs a mode change.
- ``status_routes()``: every Documents route that writes a document's status. The status guard
  refuses each of them on an agreement that runs a mode before or after the change;
  emit_event hands every live status change to it (test_doc_status_guard_door holds that
  door shut).

Run as a script for the census table: ``python consent_census.py``.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(p) for p in (HERE, HERE.parents[2], HERE.parent, HERE.parents[1] / "celerp-docs")]

from owned_fields_census import (  # noqa: E402
    PACKAGE, ROOT, SINK, Unresolved, _callee, _kw, _Package, _sources, writes,
)

ROUTES = PACKAGE / "routes.py"
MARKER = "_WRITING"


def mode_fields() -> set[str]:
    from celerp_subscriptions.billing import CONSENT_TERMS

    return {"status", *(f for terms in CONSENT_TERMS.values() for f in terms)}


def _params(fn) -> list[str]:
    return [a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)]


def _arg(fn, call: ast.Call, param: str):
    positional = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]
    value = _kw(call, param)
    if value is None and param in positional and positional.index(param) < len(call.args):
        value = call.args[positional.index(param)]
    return value


def _reach(pkg: _Package, fn, seen: frozenset = frozenset()) -> tuple[set[str], set[str], list[str]]:
    """What the calls inside ``fn`` write on an agreement: (the parameters of ``fn`` that flow
    into the write, the keys resolved in its scope, what could not be resolved)."""
    params, keys, unresolved = set(), set(), []
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        name = _callee(call)
        if name == SINK:
            flowing = [call.args[3] if len(call.args) > 3 else _kw(call, "changes"), _kw(call, "drop")]
        elif name in pkg.functions and name not in seen and name != fn.name:
            flowing = []
            for callee in pkg.functions[name]:
                p, k, u = _reach(pkg, callee, seen | {fn.name})
                keys |= k
                unresolved += u
                flowing += [_arg(callee, call, q) for q in p]
        else:
            continue
        for expr in (e for e in flowing if e is not None):
            if isinstance(expr, ast.Name) and expr.id in _params(fn):
                params.add(expr.id)
                continue
            try:
                keys |= pkg.keys(expr, fn)
            except Unresolved as exc:
                unresolved.append(str(exc))
    return params, keys, unresolved


def _routes(pkg: _Package):
    for fn in ast.walk(pkg.trees[ROUTES]):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in fn.decorator_list:
                if (isinstance(d, ast.Call) and getattr(d.func, "attr", None) in ("post", "put", "patch", "delete")
                        and d.args and isinstance(d.args[0], ast.Constant)):
                    yield f"{d.func.attr.upper()} {d.args[0].value}", fn


def route_doors() -> dict[str, tuple[str, list[str], list[str]]]:
    """{route function: (method and path, status and consent-term keys it writes, unresolved)}
    for every subscriptions route that can write status or a consent term."""
    pkg, fields, found = _Package(PACKAGE), mode_fields(), {}
    for label, fn in _routes(pkg):
        _, keys, unresolved = _reach(pkg, fn)
        if keys & fields or unresolved:
            found[fn.name] = (label, sorted(keys & fields), unresolved)
    return found


def outside_writes() -> dict[str, list[str]]:
    """{function: status and consent-term keys} for every agreement write in the module that
    does not go through write_agreement."""
    pkg, fields = _Package(PACKAGE), mode_fields()
    own = {f"{path.relative_to(ROOT)}:{call.lineno}" for path, call in pkg.calls("emit_event")}
    found = {}
    for row in writes():
        where, function = row.where.split(" ")
        if where in own:
            found[function] = sorted((row.sets | row.drops) & fields) + [f"UNRESOLVED {u}" for u in row.unresolved]
    return found


def marker_writers() -> set[str]:
    """Every function in the code that sets or resets billing's write marker."""
    found = set()
    for path, tree in _sources():
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for call in ast.walk(fn):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("set", "reset")
                        and MARKER in {getattr(call.func.value, "id", None), getattr(call.func.value, "attr", None)}):
                    found.add(f"{path.relative_to(ROOT)}::{fn.name}")
    return found


def status_routes() -> list[str]:
    from test_doc_status_guard_door import _docs_routes

    return sorted(label for label, found in _docs_routes().items() if found)


if __name__ == "__main__":
    doors = route_doors()
    print(f"# mode fields (status and every consent term): {sorted(mode_fields())}")
    print(f"# subscriptions routes that write status or a consent term: {len(doors)}")
    for name, (label, keys, unresolved) in sorted(doors.items()):
        print(f"ROUTE {label} ({name}) writes={keys}" + (f" UNRESOLVED={unresolved}" if unresolved else ""))
    outside = outside_writes()
    print(f"# agreement writes in the module outside write_agreement: {len(outside)}")
    for name, keys in sorted(outside.items()):
        print(f"OUTSIDE {name} writes={keys}")
    marks = sorted(marker_writers())
    print(f"# functions that set the agreement write marker ({MARKER}): {len(marks)}")
    for m in marks:
        print(f"MARKER {m}")
    routes = status_routes()
    print(f"# Documents routes that write a status (refused by the status guard on an agreement that runs a mode): {len(routes)}")
    for r in routes:
        print(f"STATUS {r}")
