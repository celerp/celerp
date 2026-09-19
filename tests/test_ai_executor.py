# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Executor parity: the agent has no authorization behavior of its own.

Transport cases use a tiny app; the parity case re-enters the real Celerp app so
an agent-issued capability call returns byte-for-byte what the same bearer token
gets from the public API.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from test_helpers import perm_setup

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


async def test_executor_403_from_target_route_returned_as_error():
    """A 403 raised by the target route is surfaced as a structured, unsuccessful
    tool result - never converted into a success."""
    app = FastAPI()

    @app.get("/forbidden", openapi_extra={"x-celerp-agent": True})
    async def forbidden():
        raise HTTPException(status_code=403, detail="Not permitted")

    _core_module(app, "forbidden")
    cap = next(iter(ai_tools.compile_agent_capabilities(app, {}).values()))

    result = await ai_tools.execute_agent_capability(
        app, "Bearer user-token", cap, {}, "call-1",
    )
    assert result["ok"] is False
    assert result["status"] == 403
    assert result["data"] == {"detail": "Not permitted"}


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
