# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refusal from the API reaches the user. A UI route that catches an APIError answers
with the refusal as short plain text, which the page's error banner shows, never with an
empty or unreadable error reply; and no button navigates away after its request whatever
the reply, which would wipe the refusal off the screen."""
from __future__ import annotations

import ast
import pathlib

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
