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

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RESPONSES = {"Response", "_R", "_Resp", "HTMLResponse", "PlainTextResponse", "JSONResponse"}
# The app's own error page handler renders a whole page for an uncaught APIError.
_FULL_PAGE = {"ui/app.py"}


def _ui_sources() -> list[pathlib.Path]:
    return sorted({*_ROOT.glob("ui/**/*.py"), *_ROOT.glob("default_modules/*/*/ui*.py"),
                   *_ROOT.glob("default_modules/*/*/ui/**/*.py")})


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
    for path in _ui_sources():
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
             for p in _ui_sources() for n, line in enumerate(p.read_text().splitlines(), 1)
             if "hx_on__after_request" in line and "window.location" in line]
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
    for path in _ui_sources():
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
