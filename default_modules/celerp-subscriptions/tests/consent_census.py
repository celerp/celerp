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

Consent belongs to whoever sets the automatic terms, so the census also reads:

- ``automatic_writers()``: every function in the module whose agreement writes, followed to
  write_agreement, set or drop an automatic field (a consent term), in any status.
- ``consent_writers()``: every function in the code that writes the ``consents`` key.
- ``documents_gaps()``: every automatic field a Documents create or edit would take (each must be
  refused there, so write_agreement is the only writer of them).

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


def automatic_fields() -> set[str]:
    """Every field a mode's consent covers: what the automatic terms are."""
    from celerp_subscriptions.billing import CONSENT_TERMS

    return {f for terms in CONSENT_TERMS.values() for f in terms}


def mode_switches() -> set[str]:
    """The stored fields billing.configured_modes reads: the ones that choose a mode."""
    tree = ast.parse((PACKAGE / "billing.py").read_text(encoding="utf-8"))
    named = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    # An earlier head has no configured_modes: modes_in_force read them there.
    fn = named.get("configured_modes") or named["modes_in_force"]
    return {c.args[0].value for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "get"
            and c.args and isinstance(c.args[0], ast.Constant)}


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


def _nests(fn) -> bool:
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(fn) if n is not fn)


def automatic_writers() -> dict[str, tuple[list[str], list[str]]]:
    """{file::function: (automatic fields its agreement writes set or drop, unresolved)} for every
    function in the module (each route, and every other function, nested ones read on their own)
    whose writes, followed through the module's functions to write_agreement, reach one."""
    pkg, fields, found = _Package(PACKAGE), automatic_fields(), {}
    nesting = {name for name, fns in pkg.functions.items() if any(_nests(f) for f in fns)}
    for path, tree in pkg.trees.items():
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            # A function holding the routes (or calling the one that builds them) is read route by route.
            if fn.name == SINK or fn.name in nesting or any(
                    isinstance(c, ast.Call) and _callee(c) in nesting for c in ast.walk(fn)):
                continue
            _, keys, unresolved = _reach(pkg, fn)
            if keys & fields or unresolved:
                found[f"{path.name}::{fn.name}"] = (sorted(keys & fields), unresolved)
    return found


def _reads(node, parent) -> bool:
    """``node`` (the string "consents") is read, not written: a .get() argument or a subscript load."""
    if isinstance(parent, ast.Call) and getattr(parent.func, "attr", None) == "get" and parent.args[:1] == [node]:
        return True
    return isinstance(parent, ast.Subscript) and isinstance(parent.ctx, ast.Load)


def consent_writers() -> set[str]:
    """Every function in the code that writes the ``consents`` key: as a dict key, a keyword, a
    subscript store, or a key it drops. Module-level constants describe it and write nothing."""
    found = set()
    for path, tree in _sources():
        parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            hit = (isinstance(node, ast.Constant) and node.value == "consents" and not _reads(node, parent.get(node))
                   ) or (isinstance(node, ast.keyword) and node.arg == "consents")
            fn = node
            while hit and fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = parent.get(fn)
            if hit and fn is not None:
                found.add(f"{path.relative_to(ROOT)}::{fn.name}")
    return found


def documents_gaps() -> dict[str, list[str]]:
    """Automatic fields a Documents door would take: on create or import (which may carry
    schedule terms on a draft, never a mode switch or a consent), and on edit (which takes none)."""
    from celerp_docs.doc_constants import SCHEDULE_OWNED_FIELDS, SCHEDULE_TERM_FIELDS

    gaps = {"create": sorted((mode_switches() | {"consents"}) - SCHEDULE_OWNED_FIELDS),
            "edit": sorted(automatic_fields() - SCHEDULE_OWNED_FIELDS - SCHEDULE_TERM_FIELDS)}
    return {door: fields for door, fields in gaps.items() if fields}


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
    writers = automatic_writers()
    print(f"# automatic fields (every consent term): {sorted(automatic_fields())}")
    print(f"# mode switches (read by configured_modes): {sorted(mode_switches())}")
    print(f"# module functions whose writes reach an automatic field: {len(writers)}")
    for name, (keys, unresolved) in sorted(writers.items()):
        print(f"AUTOMATIC {name} writes={keys}" + (f" UNRESOLVED={unresolved}" if unresolved else ""))
    consents = sorted(consent_writers())
    print(f"# functions that write consents: {len(consents)}")
    for c in consents:
        print(f"CONSENT {c}")
    gaps = documents_gaps()
    print(f"# automatic fields a Documents door would take: {len(gaps)}")
    for door, fields in sorted(gaps.items()):
        print(f"DOCS-GAP {door} {fields}")
    routes = status_routes()
    print(f"# Documents routes that write a status (refused by the status guard on an agreement that runs a mode): {len(routes)}")
    for r in routes:
        print(f"STATUS {r}")
