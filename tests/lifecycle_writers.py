# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The lifecycle-writer census: every key a writer sets on an existing document or list, found
from the code. The documents projection names the keys of each event branch; for a branch that
applies its event's payload, every call that emits the event is a writer, with the keys its
payload can hold, followed through local names and callers. A key it cannot name is ``<expr>``.

    python tests/lifecycle_writers.py    # key, writer, event, where record_copy places it
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREATED = {"doc.created", "list.created"}
PROJ = Path("default_modules/celerp-docs/celerp_docs/doc_projections.py")
ROUTES = Path("default_modules/celerp-docs/celerp_docs/routes.py")


def app_sources():
    for top in ("celerp", "ui", "default_modules"):
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT)
            if "tests" in rel.parts or path.name.startswith(("test_", "conftest")):
                continue
            yield rel, path


def _callee(call):
    return getattr(call.func, "id", getattr(call.func, "attr", ""))

class _App:
    def __init__(self):
        self.trees = {rel: ast.parse(p.read_text(encoding="utf-8")) for rel, p in app_sources()}
        self.consts, self.collections, self.fns, self.calls = {}, {}, {}, {}
        for rel, tree in self.trees.items():
            for n in tree.body:
                if isinstance(n, (ast.Assign, ast.AnnAssign)) and not isinstance(n.value, ast.Constant):
                    for tg in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                        if isinstance(tg, ast.Name):
                            self.collections.setdefault(tg.id, n.value)
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                    for tg in n.targets:
                        if isinstance(tg, ast.Name):
                            self.consts.setdefault(tg.id, n.value.value)
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.fns.setdefault(fn.name, []).append((rel, fn))
                    for c in ast.walk(fn):
                        if isinstance(c, ast.Call):
                            self.calls.setdefault(_callee(c), []).append((rel, fn, c))

    def attr_values(self, attr):
        """Every string the app passes as ``attr=`` or assigns to ``attr`` (a field and its settings)."""
        out = set()
        for tree in self.trees.values():
            for n in ast.walk(tree):
                if isinstance(n, ast.keyword) and n.arg == attr and isinstance(n.value, ast.Constant) \
                        and isinstance(n.value.value, str):
                    out.add(n.value.value)
        return out

    def name_set(self, node):
        """The strings a literal or module-level collection of names holds, or None."""
        if isinstance(node, ast.Name):
            node = self.collections.get(node.id)
        if isinstance(node, ast.Call) and node.args:
            node = node.args[0]  # frozenset({...}) / tuple((...))
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            names = [self.name(e) for e in node.elts]
            return set(names) if None not in names else None
        return None

    def name(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self.consts.get(getattr(node, "id", getattr(node, "attr", None)))
        return None

    def entity_types(self):
        seen = {}
        for rel, fn, c in (x for v in self.calls.values() for x in v):
            kw = {k.arg: k.value for k in c.keywords}
            ev, et = self.name(kw.get("event_type")), kw.get("entity_type")
            if ev:
                seen.setdefault(ev, set()).add(self.name(et) if et is not None else None)
        return seen

app: _App = None  # built once, on the first census


def _data_part(node, data="data"):
    """Which part of the event's payload ``node`` stands for: None for the whole payload,
    a key for data[key] / data.get(key), False when it is not the payload."""
    if isinstance(node, ast.Name) and node.id == data:
        return None
    if isinstance(node, ast.Subscript) and getattr(node.value, "id", None) == data:
        return app.name(node.slice)
    if isinstance(node, ast.Call) and _callee(node) == "get" and getattr(node.func.value, "id", None) == data and node.args:
        return app.name(node.args[0])
    return False

def _applied_parts(body, cur="current"):
    """The parts of the payload a branch writes onto the record without naming their keys."""
    parts = set()
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "update" \
                    and getattr(n.func.value, "id", None) == cur and n.args:
                arg = n.args[0]
                if isinstance(arg, ast.Dict):
                    parts.update(_data_part(v) for k, v in zip(arg.keys, arg.values) if k is None)
                elif isinstance(arg, ast.DictComp):
                    parts.update(_data_part(g.iter.func.value) for g in arg.generators
                                 if isinstance(g.iter, ast.Call) and _callee(g.iter) == "items")
                else:
                    parts.add(_data_part(arg))
            if isinstance(n, ast.For) and isinstance(n.iter, ast.Call) and _callee(n.iter) == "items" \
                    and any(isinstance(a, (ast.Assign,)) and any(isinstance(t, ast.Subscript) and getattr(t.value, "id", None) == cur
                                                                 and app.name(t.slice) is None for t in a.targets)
                            for a in ast.walk(n)):
                parts.add(_data_part(n.iter.func.value))
    return parts - {False}

def _branch_keys(body, cur="current"):
    keys, loops = set(), {}
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.For) and isinstance(n.iter, (ast.Tuple, ast.List)):
                targets = [n.target] if isinstance(n.target, ast.Name) else list(getattr(n.target, "elts", []))
                for i, t in enumerate(targets):
                    if isinstance(t, ast.Name):
                        loops[t.id] = [app.name(e if len(targets) == 1 else e.elts[i]) for e in n.iter.elts]
    def key_of(k):
        v = app.name(k)
        if v is not None:
            return [v]
        if isinstance(k, ast.Name):
            return loops.get(k.id, [])  # a key from a loop over the payload is an applied part
        return [f"<{ast.unparse(k)}>"]
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, (ast.Assign, ast.AugAssign)):
                for tg in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    if isinstance(tg, ast.Subscript) and getattr(tg.value, "id", None) == cur:
                        keys.update(key_of(tg.slice))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and getattr(n.func.value, "id", None) == cur:
                if n.func.attr == "setdefault" and n.args:
                    keys.update(key_of(n.args[0]))
                if n.func.attr == "update" and n.args and isinstance(n.args[0], ast.Dict):
                    keys.update(app.name(k) for k in n.args[0].keys if k is not None)
    return keys

def projection_branches():
    """(events, keys it names, payload parts it applies, creates) for each event branch of the
    documents projection; a branch that sets entity_type makes a new record."""
    fn = next(f for r, f in app.fns["apply_documents_event"] if r == PROJ)
    node = next(s for s in fn.body if isinstance(s, ast.If))
    out = []
    while isinstance(node, ast.If):
        c = node.test.comparators[0]
        evs = {app.name(e) for e in getattr(c, "elts", [c])}
        helpers = [f for n in (n for s in node.body for n in ast.walk(s) if isinstance(n, ast.Call))
                   for r, f in app.fns.get(_callee(n), []) if r == PROJ]
        keys = _branch_keys(node.body)
        parts = _applied_parts(node.body) | {p for h in helpers for p in _applied_parts(h.body)}
        out.append((evs, keys, parts, "entity_type" in keys))
        node = node.orelse[0] if len(node.orelse) == 1 else None
    return out


def _params(fn):
    a = fn.args
    return [x.arg for x in (*a.posonlyargs, *a.args)], {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)}

class _Resolver:
    """The keys a payload expression can hold, followed through local assignments and, for a
    parameter, through every caller in the app. A value it cannot follow is ``<expr>``."""
    def __init__(self):
        self.seen = set()

    def keys(self, expr, fn, part=None, depth=0):
        if isinstance(expr, ast.Dict):
            if part is not None:
                for k, v in zip(expr.keys, expr.values):
                    if k is not None and app.name(k) == part:
                        return self.keys(v, fn, None, depth)
                return set()
            out = set()
            for k, v in zip(expr.keys, expr.values):
                out |= self.keys(v, fn, None, depth) if k is None else {app.name(k) or f"<{ast.unparse(k)}>"}
            return out
        if isinstance(expr, ast.DictComp):
            g = expr.generators[0]
            if isinstance(g.iter, ast.Call) and _callee(g.iter) == "items" and part is None:
                return self.keys(g.iter.func.value, fn, None, depth)
            if part is None and isinstance(g.target, ast.Name) and isinstance(expr.key, ast.Name) \
                    and expr.key.id == g.target.id and (names := app.name_set(g.iter)) is not None:
                return names  # {f: ... for f in FIELDS}: the fields named
            return {f"<{ast.unparse(expr)[:60]}>"}
        if isinstance(expr, ast.Await):
            return self.keys(expr.value, fn, part, depth)
        if isinstance(expr, ast.IfExp):
            return self.keys(expr.body, fn, part, depth) | self.keys(expr.orelse, fn, part, depth)
        if isinstance(expr, ast.Call):
            if _callee(expr) == "dict" and len(expr.args) == 1 and part is None:
                return self.keys(expr.args[0], fn, None, depth) | {k.arg for k in expr.keywords if k.arg}
            kw = {k.arg: k.value for k in expr.keywords}
            if part is not None and part in kw:  # a model built with the part: DocPatch(fields_changed=...)
                return self.keys(kw[part], fn, None, depth)
            made = [f for _, f in app.fns.get(expr.func.id, [])] if isinstance(expr.func, ast.Name) and depth < 4 else []
            if made:  # what an app function returns
                return {k for f in made for r in ast.walk(f) if isinstance(r, ast.Return) and r.value is not None
                        for k in self.keys(r.value, f, part, depth + 1)}
            return {f"<{ast.unparse(expr)[:60]}>"}
        if isinstance(expr, ast.Attribute) and part is None:
            return self.keys(expr.value, fn, expr.attr, depth)
        if isinstance(expr, ast.Name):
            return self._name(expr.id, fn, part, depth)
        return {f"<{ast.unparse(expr)[:60]}>"}

    def _subscript_key(self, key, fn, depth):
        if (v := app.name(key)) is not None:
            return {v}
        for n in ast.walk(fn):  # x[k] = ... inside ``for k, v in source.items()`` writes source's keys
            if isinstance(n, ast.For) and isinstance(key, ast.Name) and key.id in {
                    t.id for t in ast.walk(n.target) if isinstance(t, ast.Name)} and isinstance(n.iter, ast.Call) \
                    and _callee(n.iter) == "items":
                return self.keys(n.iter.func.value, fn, None, depth)
        if isinstance(key, ast.Name):  # x[k] with k = <obj>.attr: every value the app gives attr,
            for n in ast.walk(fn):     # less those the writer only compares k against (a sentinel)
                if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == key.id for t in n.targets) \
                        and isinstance(n.value, ast.Attribute):
                    return app.attr_values(n.value.attr) - {
                        app.name(c.comparators[0]) for c in ast.walk(fn) if isinstance(c, ast.Compare)
                        and getattr(c.left, "id", None) == key.id and isinstance(c.ops[0], ast.Eq)}
        return {f"<{ast.unparse(key)}>"}

    def _name(self, name, fn, part, depth):
        if (id(fn), name, part) in self.seen:
            return set()
        self.seen.add((id(fn), name, part))
        out, bound = set(), False
        for n in ast.walk(fn):
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                for tg in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    if isinstance(tg, ast.Name) and tg.id == name and n.value is not None:
                        out |= self.keys(n.value, fn, part, depth); bound = True
                    if part is None and isinstance(tg, ast.Subscript) and getattr(tg.value, "id", None) == name:
                        out |= self._subscript_key(tg.slice, fn, depth)
            if part is None and isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and getattr(n.func.value, "id", None) == name and n.func.attr in ("update", "setdefault") and n.args:
                out |= self.keys(n.args[0], fn, None, depth) if n.func.attr == "update" else {app.name(n.args[0]) or "<?>"}
        positional, params = _params(fn)
        if name in params and not bound:
            callers = [(cfn, c) for r, cfn, c in app.calls.get(fn.name, [])] if depth < 3 else []
            if not callers:
                return {f"<{fn.name}:{name}>"}
            for cfn, c in callers:
                kw = {k.arg: k.value for k in c.keywords}
                i = positional.index(name) - (1 if positional[:1] in (["self"], ["cls"]) else 0) if name in positional else None
                arg = kw.get(name, c.args[i] if i is not None and i < len(c.args) else None)
                if arg is not None and not isinstance(arg, ast.Starred):
                    out |= self.keys(arg, cfn, part, depth + 1)
        return out

def _payload_arg(call):
    """The payload an emitting call passes: its ``data`` argument, by keyword or by the position the
    callee's own definition gives it."""
    kw = {k.arg: k.value for k in call.keywords}
    if "data" in kw:
        return kw["data"]
    for _, fn in app.fns.get(_callee(call), []):
        positional, _ = _params(fn)
        if "data" in positional and positional.index("data") < len(call.args):
            return call.args[positional.index("data")]
    return None


def lifecycle_writers():
    """(key, writer, event): every key a writer sets on an existing document or list. The
    projection's own branches name theirs; for a branch that applies its payload, every caller
    that emits the event is a writer, with the keys its payload can hold."""
    global app
    app = app or _App()
    types = app.entity_types()
    rows, applied = [], {}
    for evs, keys, parts, creates in projection_branches():
        evs = {e for e in evs if e not in CREATED and types.get(e, {None}) & {"doc", "list", None}}
        if creates or not evs:
            continue
        for e in sorted(evs):
            rows += [(k, f"{PROJ}:apply_documents_event", e) for k in keys]
            if parts:
                applied[e] = parts
    for callee, sites in app.calls.items():
        for rel, fn, c in sites:
            vals = [*c.args, *(k.value for k in c.keywords)]
            for i, v in enumerate(vals):
                e = app.name(v)
                if e not in applied or rel == PROJ:
                    continue
                data = _payload_arg(c)
                if data is None:
                    continue  # the event named by a call that emits nothing (a replay lookup)
                for part in applied[e]:
                    for k in _Resolver().keys(data, fn, part):
                        rows.append((k, f"{rel}:{fn.name}", e))
    return sorted(set(rows))


def _class_fields(tree, names):
    return {s.target.id for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name in names
            for s in n.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}


def content_keys() -> frozenset[str]:
    """What the code declares a user enters on a document or list: the create payloads' fields,
    the contact snapshot, the commercial terms a caller chooses, the money inputs and the totals
    worked out from them, and the corrective fields a finalized document still takes."""
    from celerp_docs import doc_money, routes

    fields = _class_fields(ast.parse((ROOT / ROUTES).read_text(encoding="utf-8")), {"DocCreatePayload", "ListCreatePayload"})
    return frozenset(fields | set(routes._CONTACT_SNAPSHOT_FIELDS) | set(routes.CHOSEN_TERMS)
                     | routes._DOC_MONEY_INPUTS | set(doc_money.LIST_TOTAL_FIELDS) | routes._FINALIZED_EDITABLE_FIELDS)


def census() -> list[tuple[str, str, str, str]]:
    """(key, writer, event, place): place is ``never copied`` (record_copy.NEVER_COPIED),
    ``content`` (content_keys) or ``NO``."""
    from celerp_docs.record_copy import NEVER_COPIED

    content = content_keys()
    return [(k, w, e, "never copied" if k in NEVER_COPIED else "content" if k in content else "NO")
            for k, w, e in lifecycle_writers()]


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "default_modules/celerp-docs"), str(ROOT / "default_modules/celerp-contacts")]
    for row in census():
        print("\t".join(row))
