# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Census of the doors through which an agreement's stored schedule terms are written, and of
the readers that build a schedule for many agreements at once, read from the code.

Writers. Every live write of a document goes through emit_event (or connector_upsert, which
calls it), and emit_event hands each one that changes the document to the doc_status_guard
handlers, where guard_agreement_status refuses terms that build no schedule. So:

- ``doc_doors()``: every emit_event or connector_upsert call in the code that can write a
  document (entity type "doc", or one the census cannot read), with its event types. Each
  reaches the check by construction; the list is what the check covers.
- ``direct_writers()``: every function in the code that sets a projection's ``state`` itself
  or applies an event without emit_event (ledger replays and backfills), with the schedule
  term fields it names (each must name none: it rebuilds, never writes new terms).

Readers. A reader that loops over many agreements and reaches ``Schedule.from_state`` must
ask first whether the row's stored terms build one, or one bad row answers 500 for all:

- ``loop_readers()``: every function in the subscriptions module that loops over a source of
  many agreements (billing.agreements, search_subscription_templates, or a Projection query
  over SUBSCRIPTION_DOC_TYPES) and, from the loop, reaches ``Schedule.from_state`` or a read of
  a schedule term through the module's functions; each with the paths that reach it without passing a gate (a function
  that asks on_schedule, unscheduled, billable or refuse_unscheduled, a scheduled lock, a gate
  asked earlier in the same loop body, or a try that catches any error). An empty path
  list means the reader is guarded.

Terms. ``term_fields()`` is every term a writer may set or a consent covers; each must be
declared in ``periods.TERMS`` (``declared_terms()``), and no request model bounds one ahead
of the builder (``request_bounds()``), so every bad term gets the builder's keyed refusal.

Run as a script for the census table: ``python schedule_census.py``.
"""
from __future__ import annotations

import ast
import functools
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(p) for p in (HERE, HERE.parents[2], HERE.parent, HERE.parents[1] / "celerp-docs")]

from owned_fields_census import PACKAGE, ROOT, _callee, _event_types, _kw, _Package, _sources  # noqa: E402

# Where the multi-agreement sources come from.
SOURCES = {"agreements", "search_subscription_templates"}
# What asks whether stored terms build a schedule before one is built.
GATES = {"on_schedule", "unscheduled", "billable", "refuse_unscheduled", "locked_agreement"}
# What a try must catch to stand for a gate: any error, since a stored value of the wrong
# type fails with TypeError or AttributeError as readily as with the ScheduleError.
CAUGHT = {"Exception", "BaseException"}


@functools.cache
def term_fields() -> frozenset[str]:
    """Every term an agreement's schedule or unattended billing reads off its stored state:
    the terms a writer may set (SCHEDULE_TERM_FIELDS) and the ones a consent covers."""
    from celerp_docs.doc_constants import SCHEDULE_TERM_FIELDS
    from celerp_subscriptions.billing import CONSENT_TERMS

    return frozenset(SCHEDULE_TERM_FIELDS) | {f for fields in CONSENT_TERMS.values() for f in fields}


def declared_terms() -> dict:
    """periods.TERMS: the declared type and range of each term the schedule builder checks."""
    from celerp_subscriptions import periods

    return dict(getattr(periods, "TERMS", {}))


_BOUNDS = {"gt", "ge", "lt", "le", "multiple_of", "pattern", "min_length", "max_length", "strict"}


def _required_only(fn) -> bool:
    """A validator whose every refusal sits under ``if <value> is None``: it asks for a value,
    it bounds none."""
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    pkg_parent = {c: n for n in ast.walk(fn) for c in ast.iter_child_nodes(n)}

    def under_none(node) -> bool:
        while (node := pkg_parent.get(node)) is not None:
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
                    and isinstance(node.test.ops[0], ast.Is) and getattr(node.test.comparators[0], "value", 0) is None:
                return True
        return False

    return bool(raises) and all(under_none(r) for r in raises)


def request_bounds() -> list[tuple[str, str, str]]:
    """[(file:line, Model.field, what)] for every bound a request model of the code puts on a
    schedule term before the schedule builder sees it: a Field constraint, a Literal, or a
    field_validator that refuses more than a missing value. Each answers a generic 422 where
    the builder's TERMS give the term's own keyed refusal, so the list must stay empty."""
    terms, found = term_fields(), []
    for path, tree in _sources():
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            if not any("BaseModel" in ast.unparse(b) for b in cls.bases):
                continue
            where = f"{path.relative_to(ROOT)}"
            for node in cls.body:
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in terms:
                    name = node.target.id
                    if isinstance(node.value, ast.Call) and _callee(node.value) == "Field" \
                            and (bound := sorted(k.arg for k in node.value.keywords if k.arg in _BOUNDS)):
                        found.append((f"{where}:{node.lineno}", f"{cls.name}.{name}", f"Field({', '.join(bound)})"))
                    if "Literal" in ast.unparse(node.annotation):
                        found.append((f"{where}:{node.lineno}", f"{cls.name}.{name}", "Literal"))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _required_only(node):
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and _callee(dec) == "field_validator":
                            for arg in dec.args:
                                if isinstance(arg, ast.Constant) and arg.value in terms:
                                    found.append((f"{where}:{node.lineno}", f"{cls.name}.{arg.value}",
                                                  f"field_validator {node.name}"))
    return found


def doc_doors() -> list[tuple[str, str, str]]:
    """[(file:line, function, event types)] for every call that can write a document."""
    found = []
    for path, tree in _sources():
        constants = {n.targets[0].id: n.value.value for n in tree.body
                     if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                     and isinstance(n.value, ast.Constant)}
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            inner = {id(c) for g in ast.walk(fn) if g is not fn and isinstance(g, (ast.FunctionDef, ast.AsyncFunctionDef))
                     for c in ast.walk(g)}
            for call in ast.walk(fn):
                if id(call) in inner or not (isinstance(call, ast.Call) and _callee(call) in ("emit_event", "connector_upsert")):
                    continue
                entity = _kw(call, "entity_type")
                if isinstance(entity, ast.Name) and entity.id in constants:
                    entity = ast.Constant(constants[entity.id])
                if isinstance(entity, ast.Constant) and entity.value != "doc":
                    continue
                values = _event_types(_kw(call, "event_type"), fn, constants)
                if values is not None and entity is None and not any(v.startswith("doc.") for v in values):
                    continue
                event = ",".join(sorted(values)) if values else f"<{ast.unparse(_kw(call, 'event_type') or call.func)}>"
                found.append((f"{path.relative_to(ROOT)}:{call.lineno}", fn.name, event))
    return found


def direct_writers() -> dict[str, list[str]]:
    """{file::function: schedule term fields it names} for every function that sets a
    projection's state, or applies an event to one, without emit_event."""
    terms, found = term_fields(), {}
    for path, tree in _sources():
        if path.name == "engine.py" and path.parent.name in ("events", "projections"):
            continue  # emit_event itself and the projection engine it calls.
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            writes = any(isinstance(n, ast.Assign) and not isinstance(n.value, ast.Constant) and any(isinstance(t, ast.Attribute) and t.attr == "state"
                                                           and getattr(t.value, "id", "") in ("proj", "row", "projection")
                                                           for t in n.targets)
                         for n in ast.walk(fn)) or any(
                isinstance(n, ast.Call) and _callee(n) == "apply_event" and "ProjectionEngine" in ast.unparse(n.func)
                for n in ast.walk(fn))
            if writes and not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(fn) if n is not fn):
                named = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                found[f"{path.relative_to(ROOT)}::{fn.name}"] = sorted(named & terms)
    return found


def _module_callee(call: ast.Call) -> str | None:
    """The module function a call names: a bare name, or one read off a module of the package
    (``billing.due_periods``); a method of a value (``schedule.next_bill_date``) is none."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) \
            and (PACKAGE / f"{call.func.value.id}.py").exists():
        return call.func.attr
    return None


def _term_read(node) -> str | None:
    """The schedule term (or the periods, which the schedule is built from too) ``node`` reads
    off a stored state (``x.get("term")``, ``x["term"]``, ``shown("term", ...)``), or None."""
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
            and ((isinstance(node.func, ast.Attribute) and node.func.attr == "get") or _callee(node) == "shown"):
        name = node.args[0].value
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.ctx, ast.Load):
        name = node.slice.value
    else:
        return None
    return name if isinstance(name, str) and name in term_fields() | {"periods"} else None


def _from_state(node) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "from_state" \
        and getattr(node.func.value, "id", None) == "Schedule"


def _builds(fn) -> bool:
    """``fn`` builds a schedule or reads a schedule term off a stored state."""
    return any(_from_state(n) or _term_read(n) for n in ast.walk(fn))


def _gated_call(call: ast.Call) -> bool:
    return _callee(call) in GATES and not (
        _callee(call) == "locked_agreement" and isinstance(_kw(call, "scheduled"), ast.Constant)
        and _kw(call, "scheduled").value is False)


def _gated(fn) -> bool:
    return any(isinstance(n, ast.Call) and _gated_call(n) for n in ast.walk(fn))


def _caught(pkg: _Package, node) -> bool:
    """``node`` sits inside a try whose handlers catch the ScheduleError."""
    while (node := pkg.parent.get(node)) is not None and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if isinstance(node, ast.Try):
            for h in node.handlers:
                names = ({getattr(n, "id", None) or getattr(n, "attr", None) for n in ast.walk(h.type)}
                         if h.type is not None else {"BaseException"})
                if names & CAUGHT:
                    return True
    return False


def _open_paths(pkg: _Package, nodes, fn_name: str, seen: frozenset) -> list[str]:
    """The call paths from ``nodes`` (inside ``fn_name``) to Schedule.from_state that pass no gate."""
    paths = []
    # A gate asked earlier in the same body, itself or through a module function that asks
    # one, guards what comes after it.
    asked = min((n.lineno for node in nodes for n in ast.walk(node) if isinstance(n, ast.Call) and (
        _gated_call(n) or any(_gated(f) for f in pkg.functions.get(_module_callee(n), ())))), default=None)
    for node in nodes:
        for read in (n for n in ast.walk(node) if isinstance(n, ast.Subscript)):
            if (term := _term_read(read)) and not _caught(pkg, read) and not (asked is not None and read.lineno > asked):
                paths.append(f"{fn_name} -> reads {term}")
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            if _caught(pkg, call) or (asked is not None and call.lineno > asked):
                continue
            if _from_state(call):
                paths.append(f"{fn_name} -> Schedule.from_state")
                continue
            if term := _term_read(call):
                paths.append(f"{fn_name} -> reads {term}")
                continue
            name = _module_callee(call)
            if name not in pkg.functions or name in seen or name in SOURCES or name in GATES:
                continue  # a gate is where the path stops: gate_catches() checks the one they ask
            for callee in pkg.functions[name]:
                if _gated(callee):
                    continue
                paths += [f"{fn_name} -> {p}" for p in _open_paths(pkg, [callee], name, seen | {name})]
    return sorted(set(paths))


def _source_names(pkg: _Package, fn) -> set[str]:
    """The local names in ``fn`` bound to a multi-agreement source."""
    names = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and _is_source(pkg, n.value):
            names |= {t.id for t in ast.walk(n.targets[0]) if isinstance(t, ast.Name)}
    return names


def _is_source(pkg: _Package, expr) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.Call) and _callee(n) in SOURCES:
            return True
        if isinstance(n, ast.Call) and _callee(n) == "select" and "Projection" in ast.unparse(n) \
                and "SUBSCRIPTION_DOC_TYPES" in ast.unparse(pkg.parent.get(pkg.parent.get(n)) or n):
            return True
    return False


def gate_catches() -> dict[str, bool]:
    """{gate: whether every schedule build and term check in it sits in a try that catches any
    error} for the gates that build: the one test the others ask (unscheduled) turns any stored
    value into the row's honest error, never an exception out of a reader."""
    pkg, found = _Package(PACKAGE), {}
    for name in sorted(GATES):
        for fn in pkg.functions.get(name, ()):
            builds = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and (_from_state(n) or _callee(n) == "check_terms")]
            if builds:
                found[name] = all(_caught(pkg, n) for n in builds)
    return found


def loop_readers() -> dict[str, tuple[int, list[str]]]:
    """{file::function: (loops over many agreements, the open paths to Schedule.from_state)}
    for every function in the module that loops over many agreements and reaches it."""
    pkg, found = _Package(PACKAGE), {}
    for path, tree in pkg.trees.items():
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(fn) if n is not fn):
                inner = [n for n in fn.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            else:
                inner = [fn]
            names = _source_names(pkg, fn)
            loops = []
            for node in (n for part in inner for n in ast.walk(part)):
                if isinstance(node, (ast.For, ast.AsyncFor)):
                    it, body = node.iter, node.body
                elif isinstance(node, ast.comprehension):
                    owner = pkg.parent.get(node)
                    it, body = node.iter, [getattr(owner, "elt", None) or getattr(owner, "value", None), *node.ifs]
                else:
                    continue
                if _is_source(pkg, it) or ({n.id for n in ast.walk(it) if isinstance(n, ast.Name)} & names):
                    loops.append([b for b in body if b is not None])
            reach = [p for body in loops for p in _open_paths(pkg, body, fn.name, frozenset({fn.name}))]
            reaches_at_all = any(_reaches(pkg, body, frozenset({fn.name})) for body in loops)
            if loops and reaches_at_all:
                found[f"{path.name}::{fn.name}"] = (len(loops), sorted(set(reach)))
    return found


def _reaches(pkg: _Package, nodes, seen: frozenset) -> bool:
    """Whether ``nodes`` reach Schedule.from_state through the module's functions, gated or not."""
    for node in nodes:
        if _builds(node):
            return True
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            name = _module_callee(call)
            if name in pkg.functions and name not in seen and name not in SOURCES:
                if any(_reaches(pkg, [c], seen | {name}) for c in pkg.functions[name]):
                    return True
    return False


if __name__ == "__main__":
    declared = declared_terms()
    terms = sorted(term_fields() | set(declared))
    print(f"# schedule terms (each declared in periods.TERMS, which the schedule builder checks): {len(terms)}")
    for name in terms:
        term = declared.get(name)
        print(f"TERM {name} " + ("MISSING" if term is None else
              f"{term.kind} {term.key} choices={sorted(term.choices)} low={term.low} high={term.high}"))
    bounds = request_bounds()
    print(f"# request-model bounds on a term ahead of the builder (each must go): {len(bounds)}")
    for where, field, what in bounds:
        print(f"BOUND {where} {field} {what}")
    doors = doc_doors()
    print(f"# document write doors (each reaches emit_event, whose doc_status_guard handlers check the terms): {len(doors)}")
    for where, fn, event in doors:
        print(f"DOOR {where} {fn} {event}")
    direct = direct_writers()
    print(f"# projection writers outside emit_event (replays and backfills; each must name no schedule term): {len(direct)}")
    for name, terms in sorted(direct.items()):
        print(f"DIRECT {name} terms={terms}")
    gates = gate_catches()
    print(f"# gates that build a schedule (each must catch any error): {len(gates)}")
    for name, catches in gates.items():
        print(f"GATE {name} catches_any={catches}")
    readers = loop_readers()
    print(f"# readers that loop over many agreements and build a schedule or read a term (each must pass a gate first): {len(readers)}")
    for name, (loops, open_paths) in sorted(readers.items()):
        print(f"READER {name} loops={loops} open={open_paths}")
