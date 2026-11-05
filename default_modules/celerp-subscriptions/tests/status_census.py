# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Census of what the app answers after a subscription action, read from the code.

The rule it serves: the result, message and badge shown after an action are computed after
the action from what is stored, never from what the action intended.

- ``ui_actions()``: every UI route of the subscriptions pages that answers a POST, PATCH, PUT
  or DELETE, with the API actions and reads it calls (``ui.api_client`` functions, by the
  verb they send, followed through the page's own helpers). A read after the action must sit
  in a function the route hands to ``_redrawn``: one inside the action's refusal handler
  (``shared``) reports a done action as refused, one outside it (``bare``) fails the request.
  An action call whose refusal handler also holds an earlier action call of the request (an
  earlier call, or an earlier turn of a loop around it) is ``chained``: its failure would
  report the done work as refused.
- ``api_actions()``: every action route of the module's API, with the UI routes that reach
  it, so each API answer is shown through a UI row above.
- ``send_readers()``: every function of the module that reads a period's send record, and
  whether it is a writer or reads it through ``billing.send_status``.
- ``send_writers()``: every function of the module that writes a period's send record, and
  whether it is the one writer (``record_send``, which records who pressed Send).
- ``deliver_doors()``: every call of ``billing.deliver`` in the code, and whether one made on a
  person's behalf (``retry_by``) names the attempt that person saw (``attempt``).
- ``emailed_readers()``: every function of the module that reads a document's ``sent_via``,
  and every emitter of ``doc.sent`` in the code (the projection records how it went).
- ``badge_renders()``: every ``doc_detail_badges`` render a module registers, and whether it
  reads a document's status itself (a document cannot tell how its agreement bills it now).

Run as a script for the census table: ``python status_census.py``.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "default_modules" / "celerp-subscriptions" / "celerp_subscriptions"
API_CLIENT = ROOT / "ui" / "api_client.py"
ACTION_VERBS = {"post", "patch", "put", "delete"}
REDRAW = "_redrawn"
# The functions that own a period's send record; every other reader goes through send_status.
SEND_SOURCES = {"billing.deliver": "sends a period's document and records each attempt",
                "billing.waiting_sends": "the one predicate for an email the next pass sends; send_status calls it"}
SEND_STATUS = "send_status"


def ui_files() -> list[Path]:
    """The subscriptions pages' UI modules: ui/routes/subscriptions*.py and the module's own."""
    return [*sorted((ROOT / "ui" / "routes").glob("subscriptions*.py")), PACKAGE / "ui_routes.py"]


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _route(node: ast.AST) -> tuple[str, str] | None:
    """(verb, path) of a function decorated ``@app.<verb>("<path>")`` or ``@router.<verb>``."""
    for d in getattr(node, "decorator_list", []):
        if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name) and d.func.value.id in ("app", "router")
                and d.args and isinstance(d.args[0], ast.Constant)):
            return d.func.attr, d.args[0].value
    return None


def api_verbs() -> dict[str, set[str]]:
    """Each ui.api_client function and the HTTP verbs it sends."""
    verbs: dict[str, set[str]] = {}
    for node in ast.parse(API_CLIENT.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            verbs[node.name] = {c.func.attr for c in ast.walk(node)
                                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                                and c.func.attr in ACTION_VERBS | {"get"}
                                and isinstance(c.func.value, ast.Name) and c.func.value.id == "c"}
    return verbs


def api_paths() -> dict[str, list[str]]:
    """Each ui.api_client function and the API paths it calls, ``{...}`` read as one segment."""
    paths: dict[str, list[str]] = {}
    for node in ast.parse(API_CLIENT.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found = []
            for c in ast.walk(node):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr in ACTION_VERBS
                        and isinstance(c.func.value, ast.Name) and c.func.value.id == "c" and c.args):
                    arg = c.args[0]
                    if isinstance(arg, ast.Constant):
                        found.append(arg.value)
                    elif isinstance(arg, ast.JoinedStr):
                        found.append("".join(v.value if isinstance(v, ast.Constant) else "{}" for v in arg.values))
            paths[node.name] = found
    return paths


def is_action(verbs: dict[str, set[str]], name: str) -> bool:
    return bool(verbs.get(name, set()) & ACTION_VERBS)


def is_read(verbs: dict[str, set[str]], name: str) -> bool:
    return verbs.get(name) == {"get"}


@dataclass
class UiAction:
    where: str
    verb: str
    path: str
    actions: list[str]
    reads: list[str]
    shared: list[str] = field(default_factory=list)
    bare: list[str] = field(default_factory=list)
    chained: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.verb.upper()} {self.path}"


def _api_calls(node: ast.AST, helpers: dict[str, ast.AST], seen: frozenset = frozenset()) -> list[tuple[int, str]]:
    """(line, api function) of every ui.api_client call under ``node``, a page helper's
    calls counted at the line that calls the helper."""
    found = []
    for c in ast.walk(node):
        if not isinstance(c, ast.Call):
            continue
        if (isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "api"):
            found.append((c.lineno, c.func.attr))
        elif isinstance(c.func, ast.Name) and c.func.id in helpers and c.func.id not in seen:
            found += [(c.lineno, name) for _, name in
                      _api_calls(helpers[c.func.id], helpers, seen | {c.func.id})]
    return found


def _refusal_handler(t: ast.Try) -> bool:
    """A try whose APIError handler answers a refusal."""
    return any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in ("_refused", "_action_error")
               for h in t.handlers for c in ast.walk(h))


def _redrawn_defs(route: ast.AST) -> list[ast.AST]:
    """The nested functions a route hands to ``_redrawn``."""
    passed = {a.id for c in ast.walk(route) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == REDRAW for a in c.args if isinstance(a, ast.Name)}
    return [n for n in ast.walk(route) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n is not route and n.name in passed]


def _span(node: ast.AST) -> range:
    return range(node.lineno, node.end_lineno + 1)


def _body(t: ast.Try) -> range:
    return range(t.body[0].lineno, t.body[-1].end_lineno + 1)


_LOOPS = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _chained(route: ast.AST, actions: list[tuple[int, str]]) -> list[str]:
    """The action calls whose innermost handling try answers a refusal and also holds an
    earlier action call of the request: an earlier call in its body, or a loop around the
    call inside its body (an earlier turn of the loop)."""
    tries = [t for t in ast.walk(route) if isinstance(t, ast.Try)]
    loops = [n for n in ast.walk(route) if isinstance(n, _LOOPS)]
    found = []
    for n, name in actions:
        around = [t for t in tries if n in _body(t)]
        if not around:
            continue
        inner = max(around, key=lambda t: t.lineno)
        if not _refusal_handler(inner):
            continue
        body = _body(inner)
        earlier = any(m < n and m in body for m, _ in actions)
        looped = any(n in _span(loop) and loop.lineno in body for loop in loops)
        if earlier or looped:
            found.append(f"{name}@{n}")
    return found


def ui_actions() -> list[UiAction]:
    verbs = api_verbs()
    rows = []
    for path in ui_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        helpers = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in ast.walk(tree):
            found = _route(node)
            if not found or found[0] not in ACTION_VERBS:
                continue
            calls = _api_calls(node, helpers)
            actions = [(n, name) for n, name in calls if is_action(verbs, name)]
            reads = [(n, name) for n, name in calls if is_read(verbs, name)]
            row = UiAction(f"{_rel(path)}:{node.lineno}", found[0], found[1],
                           sorted({a for _, a in actions}), sorted({r for _, r in reads}))
            if actions:
                row.chained = _chained(node, actions)
                first = min(n for n, _ in actions)
                redrawn = [line for d in _redrawn_defs(node) for line in _span(d)]
                refusals = [t for t in ast.walk(node) if isinstance(t, ast.Try) and _refusal_handler(t)]
                for n, name in reads:
                    if n < first or n in redrawn:
                        continue
                    inside = any(n in range(t.body[0].lineno, t.body[-1].end_lineno + 1) for t in refusals)
                    (row.shared if inside else row.bare).append(f"{name}@{n}")
            rows.append(row)
    return rows


@dataclass
class ApiAction:
    where: str
    verb: str
    path: str
    reached_by: list[str]


def _segments(path: str) -> list[str]:
    return path.strip("/").split("/")


def _wild(segment: str) -> bool:
    return segment.startswith("{")


def _fit(client: str, route: str) -> int | None:
    """How loosely an api_client path reaches an API route: None when it cannot, else the
    number of the route's fixed segments the client path fills from a parameter."""
    a, b = _segments(client), _segments(route)
    if len(a) != len(b) or any(x != y and not _wild(x) and not _wild(y) for x, y in zip(a, b)):
        return None
    return sum(_wild(x) and not _wild(y) for x, y in zip(a, b))


def api_actions() -> list[ApiAction]:
    """The module API's action routes and the UI routes whose api_client calls reach each."""
    routes = PACKAGE / "routes.py"
    client = {name: [p.replace("{}", "{x}") for p in ps] for name, ps in api_paths().items()}
    ui = ui_actions()
    rows = []
    for node in ast.walk(ast.parse(routes.read_text(encoding="utf-8"))):
        found = _route(node)
        if not found or found[0] not in ACTION_VERBS:
            continue
        full = "/subscriptions" + found[1]
        fits = {name: f for name, ps in client.items() for p in ps if (f := _fit(p, full)) is not None}
        names = {name for name, f in fits.items() if f == min(fits.values())}
        rows.append(ApiAction(f"{_rel(routes)}:{node.lineno}", found[0], full,
                              sorted(r.key for r in ui if set(r.actions) & names)))
    return rows


@dataclass
class SendReader:
    where: str
    function: str
    kind: str  # "source", "send_status", or "unnamed"


def _reads_send(node: ast.AST) -> bool:
    for c in ast.walk(node):
        if isinstance(c, ast.Subscript) and isinstance(c.slice, ast.Constant) and c.slice.value == "send":
            return True
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "get"
                and c.args and isinstance(c.args[0], ast.Constant) and c.args[0].value == "send"):
            return True
    return False


def _calls(node: ast.AST, name: str) -> bool:
    return any(isinstance(c, ast.Call) and (getattr(c.func, "id", None) == name or getattr(c.func, "attr", None) == name)
               for c in ast.walk(node))


def send_readers() -> list[SendReader]:
    """Every module function that reads a period's send record (``["send"]`` or
    ``.get("send")``): a source (SEND_SOURCES), send_status itself or a function that calls
    it, or unnamed."""
    rows = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _reads_send(node):
                continue
            name = f"{path.stem}.{node.name}"
            kind = ("source" if name in SEND_SOURCES
                    else SEND_STATUS if node.name == SEND_STATUS or _calls(node, SEND_STATUS) else "unnamed")
            rows.append(SendReader(f"{_rel(path)}:{node.lineno}", name, kind))
    return rows


@dataclass
class SendWriter:
    where: str
    function: str
    writes: int


SEND_WRITER = "billing.record_send"


def _writes_send(node: ast.AST) -> int:
    """Writes of a period's send record under ``node``: an assignment to ``[...]["send"]``,
    or a dict display whose ``"send"`` key holds a record (a dict display)."""
    n = 0
    for c in ast.walk(node):
        targets = c.targets if isinstance(c, ast.Assign) else [c.target] if isinstance(c, (ast.AugAssign, ast.AnnAssign)) else []
        n += sum(1 for t in targets for s in ast.walk(t)
                 if isinstance(s, ast.Subscript) and isinstance(s.slice, ast.Constant) and s.slice.value == "send")
        if isinstance(c, ast.Dict):
            n += sum(1 for k, v in zip(c.keys, c.values)
                     if isinstance(k, ast.Constant) and k.value == "send" and isinstance(v, ast.Dict))
    return n


def send_writers() -> list[SendWriter]:
    """Every module function that writes a period's send record; only SEND_WRITER may."""
    rows = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (n := _writes_send(node)):
                rows.append(SendWriter(f"{_rel(path)}:{node.lineno}", f"{path.stem}.{node.name}", n))
    return rows


@dataclass
class DeliverDoor:
    where: str
    caller: str
    on_behalf: bool  # passes retry_by: a person pressed Send
    names_attempt: bool


def _code_files() -> list[Path]:
    files = []
    for top in ("celerp", "ui", "default_modules", "premium_modules"):
        files += [p for p in (ROOT / top).rglob("*.py") if "tests" not in p.parts]
    return sorted(files)


def _own(fn: ast.AST):
    """The nodes of ``fn``'s own body, not of the functions nested in it."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _is_deliver(call: ast.Call, in_billing: bool) -> bool:
    f = call.func
    return ((isinstance(f, ast.Attribute) and f.attr == "deliver" and isinstance(f.value, ast.Name)
             and f.value.id == "billing") or (in_billing and isinstance(f, ast.Name) and f.id == "deliver"))


def deliver_doors() -> list[DeliverDoor]:
    """Every call of billing.deliver outside the tests, by the function it is written in."""
    billing = PACKAGE / "billing.py"
    rows = []
    for path in _code_files():
        text = path.read_text(encoding="utf-8")
        if "deliver(" not in text:
            continue
        for fn in ast.walk(ast.parse(text)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for c in _own(fn):
                if isinstance(c, ast.Call) and _is_deliver(c, path == billing):
                    named = {k.arg for k in c.keywords}
                    rows.append(DeliverDoor(f"{_rel(path)}:{c.lineno}", f"{path.stem}.{fn.name}",
                                            "retry_by" in named, "attempt" in named))
    return sorted(rows, key=lambda r: r.where)


@dataclass
class EmailedReader:
    where: str
    function: str


EMAILED = "billing.emailed"


def emailed_readers() -> list[EmailedReader]:
    """Every module function that reads a document's ``sent_via``; only EMAILED may."""
    rows = []
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    isinstance(c, ast.Constant) and c.value == "sent_via" for c in ast.walk(node)):
                rows.append(EmailedReader(f"{_rel(path)}:{node.lineno}", f"{path.stem}.{node.name}"))
    return rows


def doc_sent_emitters() -> list[str]:
    """Every function outside the tests that emits a ``doc.sent`` event."""
    rows = []
    for path in _code_files():
        text = path.read_text(encoding="utf-8")
        if '"doc.sent"' not in text:
            continue
        for fn in ast.walk(ast.parse(text)):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    isinstance(c, ast.Call) and any(k.arg == "event_type" and isinstance(k.value, ast.Constant)
                                                    and k.value.value == "doc.sent" for k in c.keywords)
                    for c in ast.walk(fn)):
                rows.append(f"{_rel(path)}:{fn.lineno} {path.stem}.{fn.name}")
    return rows


@dataclass
class BadgeRender:
    module: str
    render: str
    reads_doc_status: bool


def badge_renders() -> list[BadgeRender]:
    """Every doc_detail_badges render a bundled or premium module's manifest names."""
    rows = []
    for top in ("default_modules", "premium_modules"):
        for manifest in sorted((ROOT / top).glob("*/__init__.py")):
            text = manifest.read_text(encoding="utf-8")
            if '"doc_detail_badges"' not in text:
                continue
            block = text[text.index('"doc_detail_badges"'):]
            block = block[:block.index("]") + 1]
            for target in re.findall(r'"render":\s*"([\w.]+):(\w+)"', block):
                source = ROOT / top / manifest.parent.name / Path(*target[0].split("."))
                fn = next(n for n in ast.parse(source.with_suffix(".py").read_text(encoding="utf-8")).body
                          if isinstance(n, ast.FunctionDef) and n.name == target[1])
                status = any(isinstance(c, ast.Constant) and c.value == "status" for c in ast.walk(fn))
                rows.append(BadgeRender(manifest.parent.name, ":".join(target), status))
    return rows


def main() -> None:
    ui = ui_actions()
    print(f"# UI action routes ({len(ui)})  [actions | reads | read after the action in the refusal | outside _redrawn]")
    for r in ui:
        print(f"{r.key:58} {r.where:42} {','.join(r.actions) or '-'} | {','.join(r.reads) or '-'}"
              f" | shared={','.join(r.shared) or '-'} | bare={','.join(r.bare) or '-'}"
              f" | chained={','.join(r.chained) or '-'}")
    api = api_actions()
    print(f"\n# API action routes ({len(api)})  [reached by UI route]")
    for a in api:
        print(f"{a.verb.upper()} {a.path:52} {a.where:62} {'; '.join(a.reached_by) or 'UNREACHED'}")
    readers = send_readers()
    print(f"\n# Readers of a period's send record ({len(readers)})")
    for s in readers:
        print(f"{s.function:36} {s.where:70} {s.kind}"
              f"{'  (' + SEND_SOURCES[s.function] + ')' if s.function in SEND_SOURCES else ''}")
    writers = send_writers()
    print(f"\n# Writers of a period's send record ({len(writers)})  [only {SEND_WRITER}]")
    for w in writers:
        print(f"{w.function:36} {w.where:70} writes={w.writes}")
    doors = deliver_doors()
    print(f"\n# Calls of deliver ({len(doors)})  [on a person's behalf | names the attempt]")
    for d in doors:
        print(f"{d.caller:36} {d.where:70} on_behalf={d.on_behalf} attempt={d.names_attempt}")
    readers = emailed_readers()
    print(f"\n# Readers of a document's sent_via in the module ({len(readers)})  [only {EMAILED}]")
    for e in readers:
        print(f"{e.function:36} {e.where}")
    emitters = doc_sent_emitters()
    print(f"\n# Emitters of doc.sent ({len(emitters)})")
    for e in emitters:
        print(e)
    badges = badge_renders()
    print(f"\n# doc_detail_badges renders ({len(badges)})")
    for b in badges:
        print(f"{b.module:24} {b.render:52} reads document status: {b.reads_doc_status}")


if __name__ == "__main__":
    main()
