# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Census of the agreement fields only billing and migration write, read from the code.

``writes()`` finds every place the subscriptions module writes an agreement (each
``write_agreement`` call and the migration's own ``doc.updated``) and resolves the keys it
sets or drops to strings, following wrappers back to their callers, local assignments, helper
returns and the request model a route dumps. An expression it cannot resolve is kept as an
unresolved row, so a new way of building the changes is reported instead of skipped.

``doors()`` finds every writer of a ``doc.created`` or ``doc.updated`` event in the code and
names the owned-field check the writing function runs, or None when it runs none.

Run as a script for the census table: ``python owned_fields_census.py``.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "default_modules" / "celerp-subscriptions" / "celerp_subscriptions"
SOURCE_TOPS = ("celerp", "default_modules", "premium_modules")
SINK = "write_agreement"
DOC_WRITE_EVENTS = {"doc.created", "doc.updated"}
# What a writing function runs that keeps owned and removed fields out of a caller's data:
# the create payload's validator, the edit refusal, the import refusal, or a strip by the set.
OWNED_FIELD_CHECKS = {"_refuse_protected_fields", "schedule_fields_refusal", "LIFECYCLE_OWNED_FIELDS",
                      "_DOC_IMPORT_UPSERT_EXCLUDED", "DocCreatePayload"}


class Unresolved(Exception):
    pass


@dataclass
class Write:
    where: str
    sets: set[str] = field(default_factory=set)
    drops: set[str] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)


def _callee(call: ast.Call) -> str | None:
    return call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", None)


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


class _Package:
    def __init__(self, root: Path):
        self.trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in sorted(root.glob("*.py"))}
        self.functions: dict[str, list[ast.AST]] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.constants: dict[str, ast.AST] = {}
        self.parent: dict[ast.AST, ast.AST] = {}
        for path, tree in self.trees.items():
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    self.parent[child] = node
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.functions.setdefault(node.name, []).append(node)
                elif isinstance(node, ast.ClassDef):
                    self.classes[node.name] = node
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    self.constants[node.targets[0].id] = node.value

    def enclosing(self, node):
        while node is not None and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node = self.parent.get(node)
        return node

    def calls(self, name: str):
        for path, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _callee(node) == name:
                    yield path, node

    # -- resolving an expression to the string keys it names --------------------------------

    def keys(self, expr, fn, seen=frozenset()) -> set[str]:
        if expr is None or (isinstance(expr, ast.Constant) and expr.value is None):
            return set()
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.Dict):
            out = set()
            for k, v in zip(expr.keys, expr.values):
                out |= self.keys(v, fn, seen) if k is None else self.keys(k, fn, seen)
            return out
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            return set().union(*(self.keys(e, fn, seen) for e in expr.elts)) if expr.elts else set()
        if isinstance(expr, ast.IfExp):
            return self.keys(expr.body, fn, seen) | self.keys(expr.orelse, fn, seen)
        if isinstance(expr, (ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.ListComp)):
            key = expr.key if isinstance(expr, ast.DictComp) else expr.elt
            gen = expr.generators[0]
            targets = {n.id for n in ast.walk(gen.target) if isinstance(n, ast.Name)}
            first = gen.target.elts[0] if isinstance(gen.target, ast.Tuple) else gen.target
            if isinstance(key, ast.Name) and isinstance(first, ast.Name) and key.id == first.id:
                it = gen.iter
                if isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and it.func.attr in ("items", "keys"):
                    it = it.func.value
                return self.keys(it, fn, seen)
            if not (targets & {n.id for n in ast.walk(key) if isinstance(n, ast.Name)}):
                return self.keys(key, fn, seen)
        if isinstance(expr, ast.Name):
            return self._name(expr.id, fn, seen)
        if isinstance(expr, ast.Call):
            return self._call(expr, fn, seen)
        raise Unresolved(ast.unparse(expr))

    def _name(self, name: str, fn, seen) -> set[str]:
        if (id(fn), name) in seen:
            return set()
        seen = seen | {(id(fn), name)}
        params = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)] + [a.arg for a in fn.args.kwonlyargs]
        if name in params:
            return self._through_callers(fn, name, seen)
        out, bound = set(), False
        for node in ast.walk(fn):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id == name and node.value is not None:
                        out |= self.keys(node.value, fn, seen)
                        bound = True
                    elif (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == name):
                        out |= self.keys(t.slice, fn, seen)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name) and node.func.value.id == name):
                if node.func.attr == "update":
                    out |= {k.arg for k in node.keywords if k.arg} | set().union(
                        *(self.keys(a, fn, seen) for a in node.args))
                elif node.func.attr in ("pop", "setdefault"):
                    raise Unresolved(ast.unparse(node))
        if bound:
            return out
        if name in self.constants:
            return self.keys(self.constants[name], fn, seen)
        raise Unresolved(name)

    def _through_callers(self, fn, param: str, seen) -> set[str]:
        args = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]
        out, found = set(), False
        for _, call in self.calls(fn.name):
            caller = self.enclosing(call)
            arg = _kw(call, param)
            if arg is None and param in args and args.index(param) < len(call.args):
                arg = call.args[args.index(param)]
            found = True
            if arg is not None:
                out |= self.keys(arg, caller, seen)
        if not found:
            raise Unresolved(f"{fn.name}({param}) has no caller")
        return out

    def _call(self, call: ast.Call, fn, seen) -> set[str]:
        name = _callee(call)
        if isinstance(call.func, ast.Attribute) and name == "model_dump" and isinstance(call.func.value, ast.Name):
            model = next((a.annotation for a in fn.args.args if a.arg == call.func.value.id), None)
            if isinstance(model, ast.Name) and model.id in self.classes:
                return {n.target.id for n in self.classes[model.id].body
                        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
        if name in ("tuple", "list", "set", "dict", "frozenset", "sorted") and call.args:
            return self.keys(call.args[0], fn, seen)
        if name in self.functions:
            out = set()
            for helper in self.functions[name]:
                for ret in (n for n in ast.walk(helper) if isinstance(n, ast.Return)):
                    out |= self.keys(ret.value, helper, seen)
            return out
        raise Unresolved(ast.unparse(call))


def _resolve(pkg: _Package, row: Write, expr, fn, into: set[str]) -> None:
    try:
        into |= pkg.keys(expr, fn)
    except Unresolved as exc:
        row.unresolved.append(str(exc))


def writes() -> list[Write]:
    """Every agreement write in the subscriptions module, with the keys it sets and drops."""
    pkg = _Package(PACKAGE)
    rows: list[Write] = []
    for path, call in pkg.calls(SINK):
        fn = pkg.enclosing(call)
        if fn is not None and fn.name == SINK:
            continue
        row = Write(f"{path.relative_to(ROOT)}:{call.lineno} {fn.name}")
        changes = call.args[3] if len(call.args) > 3 else _kw(call, "changes")
        _resolve(pkg, row, changes, fn, row.sets)
        _resolve(pkg, row, _kw(call, "drop"), fn, row.drops)
        rows.append(row)
    for path, call in pkg.calls("emit_event"):
        fn = pkg.enclosing(call)
        event = _kw(call, "event_type")
        if fn.name == SINK or not (isinstance(event, ast.Constant) and event.value == "doc.updated"):
            continue
        row = Write(f"{path.relative_to(ROOT)}:{call.lineno} {fn.name}")
        data = _kw(call, "data")
        changed = next((v for k, v in zip(data.keys, data.values)
                        if isinstance(k, ast.Constant) and k.value == "fields_changed"), None) \
            if isinstance(data, ast.Dict) else None
        if not isinstance(changed, ast.Dict):
            row.unresolved.append(ast.unparse(data) if data is not None else "no data")
        else:
            # A key whose change says drop is dropped; every other key is set.
            for k, v in zip(changed.keys, changed.values):
                dropping = "drop" in {c.value for c in ast.walk(v) if isinstance(c, ast.Constant)}
                _resolve(pkg, row, v if k is None else k, fn, row.drops if dropping else row.sets)
        rows.append(row)
    return rows


def written_keys() -> set[str]:
    return set().union(*({*r.sets, *r.drops} for r in writes()))


@dataclass
class Door:
    where: str
    function: str
    event: str
    check: str | None


def _sources():
    for top in SOURCE_TOPS:
        if not (ROOT / top).is_dir():
            continue
        for path in sorted((ROOT / top).rglob("*.py")):
            if "tests" not in path.parts:
                yield path, ast.parse(path.read_text(encoding="utf-8"))


def _strings(expr) -> set[str] | None:
    """The strings an expression can only be (a string, or a choice between strings)."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.IfExp):
        body, orelse = _strings(expr.body), _strings(expr.orelse)
        return body | orelse if body is not None and orelse is not None else None
    return None


def _event_types(event, fn, constants: dict) -> set[str] | None:
    """The event types a call's ``event_type`` can be: strings, a module constant, or a local
    name only ever bound (or looped over) to strings; None when the census cannot read it."""
    if (found := _strings(event)) is not None:
        return found
    if not isinstance(event, ast.Name):
        return None
    if event.id in constants:
        return {constants[event.id]}
    found = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == event.id for t in n.targets):
            if (values := _strings(n.value)) is None:
                return None
            found |= values
        elif isinstance(n, ast.For) and event.id in {t.id for t in ast.walk(n.target) if isinstance(t, ast.Name)}:
            found |= {c.value for c in ast.walk(n.iter) if isinstance(c, ast.Constant) and isinstance(c.value, str)
                      and "." in c.value}
    return found or None


def doors() -> list[Door]:
    """Every call that writes a doc.created or doc.updated event (emit_event or
    connector_upsert), with the owned-field check its function runs. An event type that is
    not a string the census can read is kept as its source text."""
    found = []
    for path, tree in _sources():
        constants = {n.targets[0].id: n.value.value for n in tree.body
                     if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                     and isinstance(n.value, ast.Constant)}
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            inner = {id(c) for g in ast.walk(fn) if g is not fn and isinstance(g, (ast.FunctionDef, ast.AsyncFunctionDef))
                     for c in ast.walk(g)}
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {
                n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)} | {
                a.annotation.id for a in fn.args.args if isinstance(a.annotation, ast.Name)}
            for call in ast.walk(fn):
                if id(call) in inner or not (isinstance(call, ast.Call) and _callee(call) in ("emit_event", "connector_upsert")):
                    continue
                entity = _kw(call, "entity_type")
                if isinstance(entity, ast.Name) and entity.id in constants:
                    entity = ast.Constant(constants[entity.id])
                if isinstance(entity, ast.Constant) and entity.value != "doc":
                    continue
                values = _event_types(_kw(call, "event_type"), fn, constants)
                if values is None or values & DOC_WRITE_EVENTS:
                    value = ",".join(sorted(values)) if values else ast.unparse(call.func)
                    checks = sorted(names & OWNED_FIELD_CHECKS)
                    found.append(Door(f"{path.relative_to(ROOT)}:{call.lineno}", f"{path.relative_to(ROOT)}::{fn.name}",
                                      value, ",".join(checks) or None))
    return found


if __name__ == "__main__":
    rows = writes()
    print(f"# agreement writes in the subscriptions module: {len(rows)}")
    for r in rows:
        print(f"WRITE {r.where} sets={sorted(r.sets)} drops={sorted(r.drops)}"
              + (f" UNRESOLVED={r.unresolved}" if r.unresolved else ""))
    print(f"# written keys: {sorted(written_keys())}")
    ds = doors()
    print(f"# doc.created/doc.updated writers: {len(ds)}")
    for d in ds:
        print(f"DOOR {d.where} {d.function.split('::')[1]} {d.event} check={d.check}")
