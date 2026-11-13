# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refusal from the API reaches the user. A UI route that catches an APIError answers
with the refusal as short plain text, which the page's error banner shows, never with an
empty or unreadable error reply; and no button navigates away after its request whatever
the reply, which would wipe the refusal off the screen."""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from ui.api_client import APIError
from shipped_sources import ui_sources

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RESPONSES = {"Response", "_R", "_Resp", "HTMLResponse", "PlainTextResponse", "JSONResponse"}
# The app's own error page handler renders a whole page for an uncaught APIError.
_FULL_PAGE = {"ui/app.py"}


def _names(node) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Tuple):
        return [n for e in node.elts for n in _names(e)]
    return [node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")]


def _callee(call: ast.Call) -> str:
    return call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")


def test_a_refusal_is_answered_as_plain_text_with_its_status():
    from ui.api_client import refusal_response
    r = refusal_response(APIError(409, "A location called Store already exists."))
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("text/plain")
    assert r.body.decode() == "A location called Store already exists."


def test_an_ended_session_is_left_to_the_apps_sign_in_redirect():
    from ui.api_client import refusal_response
    with pytest.raises(APIError):
        refusal_response(APIError(401, "Session expired"))


def test_every_caught_api_refusal_is_answered_through_refusal_response():
    found = []
    for path in ui_sources():
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _FULL_PAGE:
            continue
        for handler in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(handler, ast.ExceptHandler)
                    and any(n.endswith("APIError") for n in _names(handler.type))):
                continue
            for node in ast.walk(handler):
                if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Call)):
                    continue
                call = node.value
                if _callee(call) not in _RESPONSES or _callee(call) == "JSONResponse":
                    continue
                status = next((k.value for k in call.keywords if k.arg == "status_code"), None)
                if status is None or (isinstance(status, ast.Constant) and status.value < 400):
                    continue
                if isinstance(status, ast.Constant) and status.value == 401 and any(
                        k.arg == "headers" for k in call.keywords):
                    continue
                found.append(f"{rel}:{node.lineno}")
    assert not found, f"an APIError answered with an error reply the page cannot show: {found}"


def test_no_button_navigates_away_after_its_request_whatever_the_reply():
    found = [f"{p.relative_to(_ROOT).as_posix()}:{n}"
             for p in ui_sources() for n, line in enumerate(p.read_text().splitlines(), 1)
             if "hx_on__after_request" in line and ("window.location" in line or "celerpGo(" in line)]
    assert not found, f"a request's refusal is wiped by a navigation after it: {found}"


def _trigger_names(value) -> list[str]:
    """The event names an HX-Trigger value written in place fires: a plain name list, or the keys
    of a json.dumps({...}) literal."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [n.strip() for n in value.value.split(",") if n.strip()]
    if isinstance(value, ast.Call) and _callee(value) == "dumps" and value.args and isinstance(value.args[0], ast.Dict):
        return [k.value for k in value.args[0].keys if isinstance(k, ast.Constant)]
    return []


def _hx_triggers(tree) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            pairs = zip(node.keys, node.values)
        elif isinstance(node, ast.Call) and len(node.args) == 2:
            pairs = [tuple(node.args)]
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
            pairs = [(node.targets[0].slice, node.value)]
        else:
            continue
        found += [(node.lineno, name) for k, v in pairs
                  if isinstance(k, ast.Constant) and k.value == "HX-Trigger" for name in _trigger_names(v)]
    return found


def test_every_hx_trigger_event_has_a_listener():
    """An event a reply fires through HX-Trigger with nobody listening is a reply the user never
    sees: a refusal sent that way leaves the page unchanged and silent."""
    sources = sorted({*_ROOT.glob("ui/**/*.py"), *_ROOT.glob("default_modules/*/*/**/*.py")} - {
        p for p in _ROOT.glob("default_modules/*/tests/**/*.py")})
    texts = [p.read_text() for p in sources] + [p.read_text() for p in _ROOT.glob("ui/static/**/*.js")]
    emitted = [(f"{p.relative_to(_ROOT).as_posix()}:{line}", name)
               for p in sources for line, name in _hx_triggers(ast.parse(p.read_text()))]
    assert emitted, "the walk found no HX-Trigger replies; it is broken"
    # A listener is an event listener, an hx-trigger "from:" clause, or a script reading the
    # parsed header's member (obj.celerpToast).
    unheard = [(where, name) for where, name in emitted if not any(re.search(
        rf"addEventListener\(\s*['\"]{name}['\"]|\b{name}\s+from:|htmx\.on\(\s*['\"]{name}['\"]|\.{name}\b(?!\s*\()",
        text) for text in texts)]
    assert not unheard, f"HX-Trigger events nothing listens for: {unheard}"


def test_every_create_blank_door_answers_a_refusal_through_refusal_response():
    """A New button that cannot create shows why, the same way at every door."""
    doors = {}
    for path in ui_sources():
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, ast.AsyncFunctionDef) and any(
                    isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant)
                    and str(d.args[0].value).endswith("/create-blank") for d in fn.decorator_list):
                handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)
                            and any(n.endswith("APIError") for n in _names(h.type))]
                doors[f"{path.relative_to(_ROOT).as_posix()}:{fn.name}"] = handlers and all(
                    any(isinstance(r, ast.Return) and isinstance(r.value, ast.Call)
                        and _callee(r.value) == "refusal_response" for r in ast.walk(h)) for h in handlers)
    assert len(doors) >= 3, doors
    assert all(doors.values()), doors


# ── Create doors: an ended session goes to sign-in, a refusal is shown ─────

_DOOR_CALL = re.compile(r"^(create_\w*|duplicate\w*|convert\w*|change_list_type)$")
_LINE_BUILDERS = {"_line_items_from_inventory", "_reorder_lines_from_inventory"}
# The shared renderers a door hands its caught error to.
_RENDERERS = {"refusal_response", "error_message", "_action_error", "_refused", "_wc_error_response"}
# The calls that let an ended session reach the app's sign-in handler.
_SIGN_IN_THROUGH = {"refusal_response", "raise_if_signed_out", "_refused"}


def _own_nodes(fn):
    """The nodes of fn, not of the functions nested in it."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _is_door_call(call: ast.Call) -> bool:
    f = call.func
    return (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "api"
            and bool(_DOOR_CALL.match(f.attr))) or (isinstance(f, ast.Name) and f.id in _LINE_BUILDERS)


def _door_tries(tree):
    """Every try whose body creates something through the API or builds lines for a create."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                isinstance(x, ast.Call) and _is_door_call(x) for s in node.body for x in ast.walk(s)):
            yield node


def _catches_api_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    return any(n.endswith("APIError") or n in ("Exception", "BaseException") for n in _names(handler.type))


def test_raise_if_signed_out_raises_only_an_ended_session():
    from ui.api_client import raise_if_signed_out
    ended = APIError(401, "Session expired")
    with pytest.raises(APIError) as raised:
        raise_if_signed_out(ended)
    assert raised.value is ended
    # A wrong password on a confirmation prompt is also a 401 from the API, but only the
    # callers that render refusals as text decide that; every other status is never raised.
    for status in (400, 403, 404, 409, 422, 500):
        assert raise_if_signed_out(APIError(status, "No.")) is None


def test_create_doors_send_ended_session_to_sign_in():
    """Every create door hands its caught error to a shared renderer, and lets an ended session
    through to the app's sign-in handler (which clears the session cookies) instead of showing
    "session expired" as text in the form."""
    doors, wrong = 0, []
    for path in ui_sources():
        rel = path.relative_to(_ROOT).as_posix()
        for node in _door_tries(ast.parse(path.read_text())):
            for h in node.handlers:
                if not _catches_api_error(h):
                    continue
                doors += 1
                body = [n for s in h.body for n in ast.walk(s)]
                shown = any(isinstance(n, ast.Call) and _callee(n) in _RENDERERS and any(
                    isinstance(a, ast.Name) and a.id == h.name for a in n.args) for n in body)
                through = any(isinstance(n, ast.Call) and _callee(n) in _SIGN_IN_THROUGH for n in body) \
                    or any(isinstance(n, ast.Raise) for n in body)
                if not (shown and through):
                    wrong.append(f"{rel}:{h.lineno} shown={shown} sign_in_through={through}")
    assert doors >= 20, f"the door walk found only {doors} handlers; it is broken"
    assert not wrong, f"create doors that hide a refusal or an ended session: {wrong}"


# A failed read that turns into an invented value is allowed only here, each with its reason.
_FALLBACK_ALLOWED = {
    # Page chrome on every page: a failed company read shows the default menu while the page
    # body reports its own read; nothing from it is stored or sent.
    "base_shell": "page chrome",
    # An in-process registry lookup, not an API read.
    "module_nav": "registry lookup",
    # Decodes the session cookie locally; an unreadable cookie means no claims, which the
    # API's own 401 then answers.
    "get_claims": "local cookie decode",
}


def _route_path(fn, verb: str):
    for d in fn.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == verb \
                and d.args and isinstance(d.args[0], ast.Constant):
            return d.args[0].value
    return None


def _invented(value) -> bool:
    return value is None or (isinstance(value, (ast.List, ast.Tuple, ast.Set)) and not value.elts) \
        or (isinstance(value, ast.Dict) and not value.keys) \
        or (isinstance(value, ast.Constant) and value.value in (None, 0, "", False))


def _falls_back(body) -> bool:
    return all(isinstance(s, (ast.Continue, ast.Pass)) or (isinstance(s, ast.Assign) and _invented(s.value))
               or (isinstance(s, ast.Return) and _invented(s.value)) for s in body)


def test_door_reads_never_fall_back_to_an_invented_value():
    """The closed list: every create door function, every UI function it calls (followed
    transitively), and every GET route on a door's POST path (its form loader). None of them
    turns a failed read into an empty value the user is not told about."""
    trees = {p: ast.parse(p.read_text()) for p in ui_sources()}
    imported = {p: {a.asname or a.name for n in ast.walk(t) if isinstance(n, ast.ImportFrom) for a in n.names}
                for p, t in trees.items()}
    defs: dict[str, list] = {}
    for p, t in trees.items():
        for fn in ast.walk(t):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(fn.name, []).append((p, fn))
    doors = {(p, fn.lineno): (p, fn) for p, t in trees.items() for fn in ast.walk(t)
             if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                 isinstance(n, ast.Try) and any(isinstance(x, ast.Call) and _is_door_call(x)
                                                for s in n.body for x in ast.walk(s)) for n in _own_nodes(fn))}
    assert len(doors) >= 20, f"the door walk found only {len(doors)} door functions; it is broken"
    scope, work = dict(doors), list(doors.values())
    while work:
        p, fn = work.pop()
        for n in _own_nodes(fn):
            if isinstance(n, ast.Call) and _callee(n) in defs:
                for dp, dfn in defs[_callee(n)]:
                    if (dp == p or _callee(n) in imported[p]) and (dp, dfn.lineno) not in scope:
                        scope[(dp, dfn.lineno)] = (dp, dfn)
                        work.append((dp, dfn))
    posts = {_route_path(fn, "post") for p, fn in doors.values()} - {None}
    for p, t in trees.items():
        for fn in ast.walk(t):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _route_path(fn, "get") in posts:
                scope.setdefault((p, fn.lineno), (p, fn))
    found = [f"{p.relative_to(_ROOT).as_posix()}:{fn.name}:{h.lineno}"
             for p, fn in scope.values() if fn.name not in _FALLBACK_ALLOWED
             for h in _own_nodes(fn) if isinstance(h, ast.ExceptHandler) and _catches_api_error(h)
             and _falls_back(h.body)]
    assert not found, f"a door read turns a failure into an invented value: {sorted(found)}"


def test_line_builders_are_called_inside_a_refusal_handler():
    """A line builder reads items and units; its refusal must reach a handler that shows it,
    never a 500 page."""
    calls, outside = 0, []
    for path in ui_sources():
        tree = ast.parse(path.read_text())
        guarded = {id(x) for node in ast.walk(tree) if isinstance(node, ast.Try)
                   and any(_catches_api_error(h) and h.type is not None for h in node.handlers)
                   for s in node.body for x in ast.walk(s)}
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in _LINE_BUILDERS:
                calls += 1
                if id(call) not in guarded:
                    outside.append(f"{path.relative_to(_ROOT).as_posix()}:{call.lineno} {call.func.id}")
    assert calls >= 3, f"the walk found only {calls} line builder calls; it is broken"
    assert not outside, f"line builders called outside an APIError handler: {outside}"
