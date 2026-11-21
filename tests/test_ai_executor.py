# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Executor parity: the agent has no authorization behavior of its own.

Transport cases use a tiny app; the parity case re-enters the real Celerp app so
an agent-issued capability call returns byte-for-byte what the same bearer token
gets from the public API.
"""
from __future__ import annotations

import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import pytest

from test_helpers import perm_setup

from celerp.accounting_roles import refusal
from celerp.ai import tools as ai_tools
from celerp.modules.loader import _BUNDLED_MODULES_DIRS, is_core_folded, load_all, register_api_routes


_bearer = HTTPBearer()


def _core_module(app: FastAPI, *names: str) -> None:
    """Mark the named endpoints as core so the compiler treats them as eligible."""
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint and getattr(endpoint, "__name__", "") in names:
            endpoint.__module__ = "celerp.routers.agent_test"
    app.openapi_schema = None


async def test_executor_forwards_only_authorization_header():
    """The executor forwards the caller's Authorization and nothing the model can
    influence: request arguments cannot carry a headers section, and no ambient
    header (a cookie/session) is smuggled into the internal request."""
    app = FastAPI()

    @app.get("/echo", openapi_extra={"x-celerp-agent": True})
    async def echo(request: Request, auth: HTTPAuthorizationCredentials = Depends(_bearer)):
        return {
            "authorization": request.headers.get("authorization"),
            "cookie": request.headers.get("cookie"),
        }

    _core_module(app, "echo")
    cap = next(iter(ai_tools.compile_agent_capabilities(app, {}).values()))

    got = await ai_tools.execute_agent_capability(
        app, "Bearer user-token", cap, {}, "call-1",
    )
    assert got["ok"] is True
    assert got["data"]["authorization"] == "Bearer user-token"
    assert got["data"]["cookie"] is None

    # The model cannot even represent a header override: an unsupported section is
    # rejected before any internal request is made.
    smuggled = await ai_tools.execute_agent_capability(
        app, "Bearer user-token", cap,
        {"headers": {"Authorization": "Bearer attacker"}}, "call-2",
    )
    assert smuggled["error"]["code"] == "invalid_arguments"


async def test_executor_refuses_a_call_with_no_authorization_or_an_unknown_section_before_the_route_runs():
    """Without the caller's authorization there is nobody to call the route as, and an
    argument section the capability does not have is not dropped and the rest sent: each is
    refused with its own code and the route is never reached."""
    app = FastAPI()
    reached = []

    @app.get("/echo", openapi_extra={"x-celerp-agent": True})
    async def echo():
        reached.append(1)
        return {}

    _core_module(app, "echo")
    cap = next(iter(ai_tools.compile_agent_capabilities(app, {}).values()))

    for authorization in ("", None):
        got = await ai_tools.execute_agent_capability(app, authorization, cap, {}, "call-1")
        assert got["error"]["code"] == "missing_authorization", got
    got = await ai_tools.execute_agent_capability(app, "Bearer user-token", cap, {"extra": {"a": 1}}, "call-2")
    assert got["error"]["code"] == "invalid_arguments", got
    assert reached == []
    assert (await ai_tools.execute_agent_capability(app, "Bearer user-token", cap, {}, "call-3"))["ok"] is True
    assert reached == [1]


async def test_executor_403_from_target_route_returned_as_error():
    """A 403 raised by the target route is surfaced as a structured, unsuccessful
    tool result - never converted into a success."""
    app = FastAPI()

    @app.get("/forbidden", openapi_extra={"x-celerp-agent": True})
    async def forbidden():
        raise HTTPException(status_code=403, detail=refusal("error.permission_missing", "Not permitted", label="Edit"))

    _core_module(app, "forbidden")
    cap = next(iter(ai_tools.compile_agent_capabilities(app, {}).values()))

    result = await ai_tools.execute_agent_capability(
        app, "Bearer user-token", cap, {}, "call-1",
    )
    assert result["ok"] is False
    assert result["status"] == 403
    assert result["data"] == {"detail": refusal("error.permission_missing", "Not permitted", label="Edit")}


def _bundled_pluggable_names() -> set[str]:
    root = _BUNDLED_MODULES_DIRS[0]
    return {
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and not is_core_folded(p.name)
    }


async def test_executor_real_app_parity_get_item(client, session):
    """An inventory read through the executor returns exactly what the same bearer
    token gets from the public API - same status, same body. The agent adds no
    authorization behavior of its own."""
    from celerp.main import app

    ctx = await perm_setup(client, session)
    admin = ctx["admin_h"]
    item_id = ctx["item_id"]

    direct = await client.get(f"/items/{item_id}", headers=admin)
    assert direct.status_code == 200, direct.text

    # Populate the loader registry so the compiler recognises the inventory routes
    # as first-party; the autouse loader-reset fixture restores state at teardown.
    loaded = load_all(_BUNDLED_MODULES_DIRS[0], _bundled_pluggable_names())
    register_api_routes(FastAPI(docs_url=None, redoc_url=None), loaded)
    compiled = ai_tools.compile_agent_capabilities(app, {})
    get_item = next(
        cap for cap in compiled.values()
        if cap["method"] == "GET" and cap["path"] == "/items/{entity_id}"
    )

    result = await ai_tools.execute_agent_capability(
        app, admin["Authorization"], get_item,
        {"path": {"entity_id": item_id}}, "call-parity",
    )
    assert result["ok"] is True
    assert result["status"] == direct.status_code
    assert result["data"] == direct.json()


@pytest.mark.asyncio
async def test_successful_confirmed_write_large_result_stays_successful():
    """A 2xx mutation must never become an apparent failure solely due to result size."""
    from fastapi import FastAPI
    from celerp.ai.tools import execute_agent_capability

    app = FastAPI()

    @app.post("/write")
    async def write():
        return {"payload": "x" * 5000}

    capability = {
        "method": "POST", "path": "/write", "path_names": (), "query_names": (),
        "expects_body": False, "key_field": None, "keyed": False, "requires_confirmation": True,
    }
    result = await execute_agent_capability(
        app, "Bearer test", capability, {}, "tool-call-1", result_max_bytes=100,
    )
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["data"]["result_omitted"] is True


def _recording_app(status: int, body: bytes, content_type: bytes = b"application/json", delay: float = 0.0):
    """A stand-in for the app: it records each request it is sent and answers as told."""
    import asyncio

    seen: list[str] = []

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        seen.append(scope["path"])
        if delay:
            await asyncio.sleep(delay)
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", content_type)]})
        await send({"type": "http.response.body", "body": body})

    return app, seen


def _refused_calls(cap: dict) -> tuple[dict, list[tuple[object, str]]]:
    """A call this tool accepts, and each call one of the executor's checks refuses (with
    the code it answers), for the sections this tool's shape has."""
    path_names = list(cap.get("path_names") or ())
    good: dict = {}
    if path_names:
        good["path"] = {n: "x" for n in path_names}
    if cap.get("expects_body"):
        good["body"] = {}
    uploads = tuple(cap.get("uploads") or ())
    if uploads:
        good["file_ids"] = ["f"] * len(uploads)
    refused: list[tuple[object, str]] = [({**good, "other": {}}, "invalid_arguments")]
    for section, names in (("path", path_names), ("query", cap.get("query_names")), ("headers", cap.get("header_names"))):
        if names:
            # A list of the section's own names: every name is known, only the shape is wrong.
            refused.append(({**good, section: list(names)}, "invalid_arguments"))
            refused.append(({**good, section: {**good.get(section, {}), "not_a_name": "1"}}, "invalid_arguments"))
    for name in cap.get("header_names") or ():
        refused.append(({**good, "headers": {name: 1}}, "invalid_arguments"))
    if cap.get("expects_body"):
        refused.append(({**good, "body": []}, "invalid_arguments"))
    for name in path_names:
        refused.append(({**good, "path": {n: "x" for n in path_names if n != name}}, "invalid_arguments"))
        refused.append(({**good, "path": {**good["path"], name: "a/b"}}, "invalid_path_parameter"))
    if uploads and not cap.get("download"):
        refused.append(({**good, "file_ids": []}, "invalid_arguments"))
        if len(uploads) > 1:
            refused.append(({**good, "file_ids": ["f"]}, "invalid_arguments"))
    return good, refused


async def test_every_tool_refuses_without_reaching_the_route_and_never_reports_a_failed_answer_done(monkeypatch):
    """Over every tool the assistant has (read from the live app, so a new tool is covered
    the day it is added): each call the executor refuses is answered with its own code and
    the route is never sent anything; an attached file that cannot be read is refused the
    same way; and once the route has answered, an answer that is not JSON, a success that
    carries an error, an answer too large to keep and an answer that never comes are each
    reported as not done. A download tool answers with the screen's link and sends nothing."""
    from celerp.ai import deadlines
    from test_helpers import real_agent_app

    def loaded(file_id):
        return b"a,b\n", {"filename": "a.csv", "content_type": "text/csv"}

    def unreadable(file_id):
        raise FileNotFoundError(file_id)

    oversized = json.dumps({"detail": refusal("ai.err_invalid_arguments", "x" * 400)}).encode()
    step_s = deadlines.TOOL_STEP_S
    problems: list[str] = []
    caps = ai_tools.compile_agent_capabilities(real_agent_app(), {})
    assert caps
    for name, cap in sorted(caps.items()):
        good, refused = _refused_calls(cap)
        app, seen = _recording_app(200, b"{}")
        calls = [("", good, None, "missing_authorization")] + [("Bearer t", args, loaded, code) for args, code in refused]
        if cap.get("uploads") and not cap.get("download"):
            calls += [("Bearer t", good, None, "file_not_found"), ("Bearer t", good, unreadable, "file_not_found")]
        for authorization, args, load_file, code in calls:
            got = await ai_tools.execute_agent_capability(app, authorization, cap, args, "c", load_file=load_file)
            if got.get("ok") is not False or got["error"]["code"] != code:
                problems.append(f"{name} {args!r}: expected {code}, got {got!r}")
        if cap.get("download"):
            got = await ai_tools.execute_agent_capability(app, "Bearer t", cap, good, "c", load_file=loaded)
            if not (got.get("ok") is True and str(got["data"].get("download", "")).startswith("/")):
                problems.append(f"{name}: download answered {got!r}")
            if seen:
                problems.append(f"{name}: the route was sent {seen}")
            continue
        if seen:
            problems.append(f"{name}: a refused call reached the route: {seen}")
        for code, (status, body, ctype, delay) in (
                ("non_json_response", (500, b"boom", b"text/plain", 0.0)),
                ("refused_in_answer", (200, b'{"error": "not done"}', b"application/json", 0.0)),
                ("result_too_large", (500, oversized, b"application/json", 0.0)),
                ("capability_timeout", (200, b"{}", b"application/json", 0.5))):
            app, seen = _recording_app(status, body, ctype, delay)
            # Only the answer that never comes runs against a short deadline: over every tool
            # the step budget is paid once per tool, so it is kept far below the stub's delay.
            monkeypatch.setattr(deadlines, "TOOL_STEP_S", 0.01 if delay else step_s)
            got = await ai_tools.execute_agent_capability(app, "Bearer t", cap, good, "c", load_file=loaded,
                                                          result_max_bytes=200)
            if got.get("ok") is not False or got["error"]["code"] != code or not seen:
                problems.append(f"{name} answered {status}: expected {code}, got {got!r}, sent {seen}")
    assert problems == []
    # A screen template naming a placeholder the call has no value for is refused, not sent
    # with the braces in it; screen_of then falls back to the list screen.
    assert ai_tools.filled_path("/items/{item_id}", [], {})["error"]["code"] == "invalid_path_parameter"


@pytest.mark.parametrize("raw", [None, "{not json", "[]", '"text"', "null", "1"])
def test_tool_arguments_that_are_not_one_json_object_are_refused_before_any_tool_runs(raw):
    """The one check of the arguments' shape: a call whose arguments are missing, not JSON or
    JSON but not an object is answered invalid_arguments and carries no arguments onward."""
    from celerp.ai import service

    caps = {"read_items": {"method": "GET", "path": "/items"}}
    call = {"id": "c", "function": {"name": "read_items", "arguments": raw}}
    _, _, _, arguments, error = service._parse_call(call, caps, {"read_items"})
    assert arguments is None and error["error"]["code"] == "invalid_arguments"
