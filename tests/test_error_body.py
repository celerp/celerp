# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every error body the app answers with is built in one place (celerp.error_body), and
under tests a route the AI calls that refuses without a catalog key fails the test that
met it, so the whole suite checks the refusals the AI can keep and show."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.exceptions import WebSocketRequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from celerp import error_body
from celerp.accounting_roles import catalog_refusal
from celerp.ai.tools import _api_routes
from celerp.main import app


def _request(agent: bool) -> Request:
    route = next(r for _p, _m, r in _api_routes(app)
                 if ((r.openapi_extra or {}).get("x-celerp-agent") is True) == agent)
    return Request({"type": "http", "method": "POST", "path": route.path, "headers": [], "query_string": b"",
                    "route": route})


@pytest.fixture
def recorded(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(error_body, "unkeyed", seen)
    return seen


def test_every_exception_handler_answers_through_the_one_builder():
    """A handler that built its own body would answer past the strict check. FastAPI's
    websocket validation handler closes the socket and answers no body."""
    bypass = [f"{kind}: {handler.__qualname__}" for kind, handler in app.exception_handlers.items()
              if kind is not WebSocketRequestValidationError and "error_response" not in handler.__code__.co_names]
    assert not bypass, bypass


@pytest.mark.parametrize("detail", ["Barcode '1001' already exists", {"code": "x", "message": "No key."}])
async def test_an_agent_route_refusal_without_a_key_is_recorded(recorded, detail):
    handler = app.exception_handlers[StarletteHTTPException]
    response = await handler(_request(agent=True), HTTPException(409, detail))
    assert response.status_code == 409
    assert len(recorded) == 1 and repr(detail) in recorded[0]


@pytest.mark.parametrize("detail", [catalog_refusal("error.server_error"),
                                    {"errors": [catalog_refusal("error.server_error")]},
                                    [{"loc": ["body", "sku"], "msg": "Field required"}]])
async def test_a_keyed_agent_route_refusal_is_not_recorded(recorded, detail):
    handler = app.exception_handlers[StarletteHTTPException]
    await handler(_request(agent=True), HTTPException(409, detail))
    assert recorded == []


async def test_a_route_the_ai_does_not_call_is_not_checked(recorded):
    handler = app.exception_handlers[StarletteHTTPException]
    await handler(_request(agent=False), HTTPException(409, "Plain text"))
    assert recorded == []


async def test_outside_tests_the_body_is_sent_as_built(monkeypatch):
    monkeypatch.setattr(error_body, "unkeyed", None)
    handler = app.exception_handlers[StarletteHTTPException]
    response = await handler(_request(agent=True), HTTPException(409, "Plain text", headers={"X-A": "1"}))
    assert response.body == b'{"detail":"Plain text"}' and response.headers["X-A"] == "1"
