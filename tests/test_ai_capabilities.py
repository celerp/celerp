# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Generic OpenAPI capability compiler/executor tests."""
from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from celerp.ai import tools as ai_tools


_bearer = HTTPBearer()


class _WriteBody(BaseModel):
    name: str
    idempotency_key: str | None = None


class _UnsafeWriteBody(BaseModel):
    name: str


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/things/{item_id}", openapi_extra={"x-celerp-agent": True})
    async def get_thing(
        item_id: str,
        q: list[str] | None = None,
        auth: HTTPAuthorizationCredentials = Depends(_bearer),
    ):
        return {"item_id": item_id, "q": q, "token": auth.credentials}

    @app.post("/things", openapi_extra={"x-celerp-agent": True})
    async def create_thing(
        body: _WriteBody,
        auth: HTTPAuthorizationCredentials = Depends(_bearer),
    ):
        return {"name": body.name, "idempotency_key": body.idempotency_key, "token": auth.credentials}

    @app.post("/unsafe", openapi_extra={"x-celerp-agent": True})
    async def unsafe_write(body: _UnsafeWriteBody):
        return {"name": body.name}

    @app.delete("/things/{item_id}", openapi_extra={"x-celerp-agent": True})
    async def delete_thing(item_id: str):
        return {"deleted": item_id}

    @app.get("/text", openapi_extra={"x-celerp-agent": True}, response_class=PlainTextResponse)
    async def text_response():
        return "not json"

    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint and getattr(endpoint, "__name__", "") in {
            "get_thing", "create_thing", "unsafe_write", "delete_thing", "text_response"
        }:
            endpoint.__module__ = "celerp.routers.agent_test"
    app.openapi_schema = None
    return app


def test_compile_uses_openapi_and_fails_closed_for_unsafe_shapes():
    app = _app()
    compiled = ai_tools.compile_agent_capabilities(app, {})

    names = set(compiled)
    get_name = next(name for name in names if name.startswith("get_thing_"))
    post_name = next(name for name in names if name.startswith("create_thing_"))

    assert all("unsafe_write" not in name for name in names)
    assert all("delete_thing" not in name for name in names)
    assert all("text_response" not in name for name in names)

    get_params = compiled[get_name]["tool"]["function"]["parameters"]
    assert set(get_params["properties"]) == {"path", "query"}
    assert get_params["properties"]["path"]["required"] == ["item_id"]
    assert get_params["properties"]["query"]["properties"]["q"]["type"] == "array"

    post_body = compiled[post_name]["tool"]["function"]["parameters"]["properties"]["body"]
    assert "idempotency_key" not in post_body["properties"]
    assert compiled[post_name]["inject_idempotency"] is True


def test_compile_filters_disabled_and_third_party_modules(monkeypatch):
    app = FastAPI()

    @app.get("/module-read", openapi_extra={"x-celerp-agent": True})
    async def module_read():
        return {"ok": True}

    route = next(r for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == "module_read")
    route.endpoint.__module__ = "example_module.routes"
    app.openapi_schema = None

    monkeypatch.setattr(
        ai_tools,
        "loaded_modules",
        lambda: [{"name": "celerp-example", "api_routes": "example_module.api_setup", "first_party": True}],
    )
    assert ai_tools.compile_agent_capabilities(app, {})
    assert not ai_tools.compile_agent_capabilities(app, {"enabled_modules": []})

    monkeypatch.setattr(
        ai_tools,
        "loaded_modules",
        lambda: [{"name": "celerp-example", "api_routes": "example_module.api_setup", "first_party": False}],
    )
    assert not ai_tools.compile_agent_capabilities(app, {})


async def test_executor_reenters_fastapi_with_user_auth_and_server_idempotency():
    app = _app()
    compiled = ai_tools.compile_agent_capabilities(app, {})
    get_cap = next(cap for name, cap in compiled.items() if name.startswith("get_thing_"))
    post_cap = next(cap for name, cap in compiled.items() if name.startswith("create_thing_"))

    got = await ai_tools.execute_agent_capability(
        app,
        "Bearer user-token",
        get_cap,
        {"path": {"item_id": "item:1"}, "query": {"q": ["red", "blue"]}},
        "call-read",
    )
    assert got == {
        "ok": True,
        "status": 200,
        "data": {"item_id": "item:1", "q": ["red", "blue"], "token": "user-token"},
    }

    created = await ai_tools.execute_agent_capability(
        app,
        "Bearer user-token",
        post_cap,
        {"body": {"name": "A", "idempotency_key": "model-controlled"}},
        "call-write",
    )
    assert created["ok"] is True
    assert created["data"]["name"] == "A"
    assert created["data"]["token"] == "user-token"
    assert created["data"]["idempotency_key"].startswith("agent:")
    assert created["data"]["idempotency_key"] != "model-controlled"


async def test_executor_rejects_request_infrastructure_and_path_escape():
    app = _app()
    cap = next(
        cap for name, cap in ai_tools.compile_agent_capabilities(app, {}).items()
        if name.startswith("get_thing_")
    )

    bad_section = await ai_tools.execute_agent_capability(
        app,
        "Bearer user-token",
        cap,
        {"path": {"item_id": "item:1"}, "headers": {"Authorization": "Bearer attacker"}},
        "call-1",
    )
    assert bad_section["error"]["code"] == "invalid_arguments"

    bad_path = await ai_tools.execute_agent_capability(
        app,
        "Bearer user-token",
        cap,
        {"path": {"item_id": "../secret"}},
        "call-2",
    )
    assert bad_path["error"]["code"] == "invalid_path_parameter"


async def test_executor_bounds_json_result():
    app = FastAPI()

    @app.get("/big", openapi_extra={"x-celerp-agent": True})
    async def big():
        return {"value": "x" * 200}

    route = next(r for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == "big")
    route.endpoint.__module__ = "celerp.routers.agent_test"
    app.openapi_schema = None
    cap = next(iter(ai_tools.compile_agent_capabilities(app, {}).values()))

    result = await ai_tools.execute_agent_capability(
        app, "Bearer user-token", cap, {}, "call-big", result_max_bytes=32
    )
    assert result["error"]["code"] == "result_too_large"
