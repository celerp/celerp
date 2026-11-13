# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Census of who a change reads as made by, read from the code.

Who made a change is decided in one place for every activity feed and History table:
``ledger_display._actor`` gives an entry its display fields (actor_*), and
``ui.components.activity.actor_label`` turns them into the text a person reads. Work nobody
started by hand is the work the modules' background loops do; ``celerp.main`` starts each of
them through ``engine.unattended``, so every event that work writes reads "Automatically".

- ``display_keys()``: the keys ``_actor`` can give an entry.
- ``key_readers()``: every function outside the tests that reads one of those keys by name,
  with whether it is allowed (the producer and actor_label). Any other reader shows who made a
  change its own way, so it must be none.
- ``loop_starts()``: every place the code reads the background_loop slot, with whether each
  task it starts goes through ``unattended``.
- ``unattended_doors()``: every module slot whose handlers do work nobody started by hand
  (background_loop and the operations the periodic dispatcher runs), each entry of it, and
  whether every reader of that slot is reached only from a background loop entry.

Run as a script for the census table: ``python who_census.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path
from shipped_sources import APP_ROOTS, MODULE_ROOT, PREMIUM_ROOT

ROOT = Path(__file__).resolve().parents[1]
SOURCE_TOPS = (*APP_ROOTS, PREMIUM_ROOT)
PRODUCER = ("celerp/services/ledger_display.py", "_actor")
LABEL = ("ui/components/activity.py", "actor_label")
LOOP_SLOT = "background_loop"
WRAPPER = "unattended"


def _sources():
    for top in SOURCE_TOPS:
        if (ROOT / top).is_dir():
            for path in sorted((ROOT / top).rglob("*.py")):
                if "tests" not in path.parts:
                    yield str(path.relative_to(ROOT)), ast.parse(path.read_text(encoding="utf-8"))


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _key(node) -> str | None:
    """The constant key ``node`` reads: ``x["k"]`` or ``x.get("k", ...)``."""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value if isinstance(node.slice.value, str) else None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return node.args[0].value
    return None


def _callee(call: ast.Call) -> str | None:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def display_keys() -> set[str]:
    """Every key of every dict ``_actor`` returns."""
    tree = ast.parse((ROOT / PRODUCER[0]).read_text(encoding="utf-8"))
    fn = next(f for f in _functions(tree) if f.name == PRODUCER[1])
    keys = set()
    for ret in (n for n in ast.walk(fn) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)):
        keys |= {k.value for k in ret.value.keys if isinstance(k, ast.Constant)}
    return keys


def key_readers() -> list[tuple[str, str, str, bool]]:
    """[(file:line, function, key, allowed)] for every read of a display key by name."""
    keys, rows = display_keys(), []
    for path, tree in _sources():
        for fn in _functions(tree):
            for node in ast.walk(fn):
                key = _key(node)
                if key in keys:
                    rows.append((f"{path}:{node.lineno}", fn.name, key, (path, fn.name) in (PRODUCER, LABEL)))
    return sorted(set(rows))


def _slot_reads(tree, slot_names: set[str]):
    """(function, call) for each call reading a slot named in ``slot_names`` (a string, or a
    module constant holding one)."""
    consts = {n.targets[0].id: n.value.value for n in tree.body
              if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
              and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
    for fn in _functions(tree):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and node.args and _callee(node) in ("get", "_get_slot", "get_slot"):
                arg = node.args[0]
                name = arg.value if isinstance(arg, ast.Constant) else consts.get(arg.id) if isinstance(arg, ast.Name) else None
                if name in slot_names:
                    yield fn, node, name


def loop_starts() -> list[tuple[str, str, bool]]:
    """[(file:line, function, unattended)] for each task started from the background_loop slot:
    a create_task in the comprehension or loop that reads the slot."""
    rows = []
    for path, tree in _sources():
        for fn, read, _ in _slot_reads(tree, {LOOP_SLOT}):
            for comp in ast.walk(fn):
                if not isinstance(comp, (ast.ListComp, ast.For, ast.AsyncFor)):
                    continue
                iters = [g.iter for g in comp.generators] if isinstance(comp, ast.ListComp) else [comp.iter]
                if not any(read in ast.walk(i) for i in iters):
                    continue
                starts = [c for c in ast.walk(comp) if isinstance(c, ast.Call) and _callee(c) == "create_task"]
                rows += [(f"{path}:{c.lineno}", fn.name, bool(c.args) and isinstance(c.args[0], ast.Call)
                          and _callee(c.args[0]) == WRAPPER) for c in starts]
            if not any(r[0].startswith(f"{path}:") and r[1] == fn.name for r in rows):
                rows.append((f"{path}:{read.lineno}", fn.name, False))
    return rows


def _manifest_entries(slot: str) -> list[tuple[str, str]]:
    """[(module manifest, handler)] for each entry of ``slot`` in a module's manifest."""
    found = []
    for top in (MODULE_ROOT, PREMIUM_ROOT):
        for path in sorted((ROOT / top).glob("*/__init__.py")) if (ROOT / top).is_dir() else []:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Dict):
                    continue
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == slot:
                        entries = v.elts if isinstance(v, (ast.List, ast.Tuple)) else [v]
                        for e in entries:
                            if isinstance(e, ast.Dict):
                                found += [(str(path.relative_to(ROOT)), hv.value) for hk, hv in zip(e.keys, e.values)
                                          if isinstance(hk, ast.Constant) and hk.value == "handler"]
    return found


def _module_of(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    return ".".join(parts[2:] if parts[0] in (MODULE_ROOT, PREMIUM_ROOT) else parts)


def _reached_from_loop(path: str, tree, fn_name: str, loops: set[str]) -> bool:
    """Whether every caller chain of ``fn_name`` in its module ends at a background loop entry."""
    module = _module_of(path)
    callers = {f.name: {_callee(c) for c in ast.walk(f) if isinstance(c, ast.Call)} for f in _functions(tree)}
    seen, todo = set(), [fn_name]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        if f"{module}:{name}" in loops:
            continue
        up = [c for c, calls in callers.items() if name in calls and c != name]
        if not up:
            return False
        todo += up
    return True


def unattended_doors() -> list[tuple[str, str, str, str]]:
    """[(slot, entry or reader, where, reached)] for the slots whose handlers run unattended."""
    loops = {h for _, h in _manifest_entries(LOOP_SLOT)}
    rows = [(LOOP_SLOT, h, m, "entry") for m, h in _manifest_entries(LOOP_SLOT)]
    for path, tree in _sources():
        for fn, read, slot in _slot_reads(tree, {"periodic_operation"}):
            ok = _reached_from_loop(path, tree, fn.name, loops)
            rows.append((slot, f"reader {fn.name}", f"{path}:{read.lineno}", "from-loop" if ok else "NOT-FROM-LOOP"))
    rows += [("periodic_operation", h, m, "entry") for m, h in _manifest_entries("periodic_operation")]
    return rows


if __name__ == "__main__":
    print(f"DISPLAY_KEYS\t{','.join(sorted(display_keys()))}")
    readers = key_readers()
    for where, fn, key, allowed in readers:
        print(f"KEY_READER\t{where}\t{fn}\t{key}\t{'allowed' if allowed else 'OTHER'}")
    starts = loop_starts()
    for where, fn, wrapped in starts:
        print(f"LOOP_START\t{where}\t{fn}\t{'unattended' if wrapped else 'NOT-UNATTENDED'}")
    doors = unattended_doors()
    for slot, what, where, reached in doors:
        print(f"UNATTENDED_DOOR\t{slot}\t{what}\t{where}\t{reached}")
    print(f"# {len(readers)} key readers ({sum(not r[3] for r in readers)} other), {len(starts)} loop starts "
          f"({sum(not s[2] for s in starts)} not unattended), {len(doors)} unattended doors "
          f"({sum(d[3] == 'NOT-FROM-LOOP' for d in doors)} not from a loop)")
