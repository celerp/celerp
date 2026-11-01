# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every error body the app answers with is built in one place (celerp.error_body), and
under tests a route the AI calls that refuses without a catalog key fails the test that
met it, so the whole suite checks the refusals the AI can keep and show."""
from __future__ import annotations

import json

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


def _examples() -> dict:
    """One exception of each type the app has a handler for, as the code that raises it
    builds it. The HTTPException handlers keep the detail they are given, which the
    boundary checks for every route (test_an_agent_route_refusal_without_a_key_is_recorded)."""
    from fastapi.exceptions import RequestValidationError
    from slowapi.errors import RateLimitExceeded

    from celerp.inventory_codes import BarcodeConflictError, CodeConflictError
    from celerp.projections.engine import UnhandledEventsError
    from celerp.services.account_roles import PostingRoleError
    from celerp.services.business_time import InvalidTimezoneError
    from celerp.services.journal_accounts import UnbalancedJournalEntry

    return {
        404: StarletteHTTPException(404),
        StarletteHTTPException: HTTPException(409, catalog_refusal("error.server_error")),
        Exception: RuntimeError("boom"),
        RequestValidationError: RequestValidationError([{"loc": ["body", "sku"], "msg": "Field required"}]),
        # The handler reads nothing from the exception; a Limit needs a live limiter to build.
        RateLimitExceeded: RateLimitExceeded.__new__(RateLimitExceeded),
        CodeConflictError: BarcodeConflictError("1001"),
        InvalidTimezoneError: InvalidTimezoneError("error.company_timezone_unknown"),
        UnhandledEventsError: UnhandledEventsError({"unknown.event"}),
        PostingRoleError: PostingRoleError([catalog_refusal("posting.problem.unset", role="sales")]),
        UnbalancedJournalEntry: UnbalancedJournalEntry("error.server_error"),
    }


async def test_every_exception_handler_answers_an_agent_route_with_a_keyed_refusal(recorded):
    """Registry guard: each exception type the app answers is answered keyed, so a refusal
    no route raises itself (a code conflict at the event boundary, a ledger this build
    cannot replay) still reads in the reader's language. A new handler needs an example."""
    examples = _examples()
    handled = {kind for kind in app.exception_handlers if kind is not WebSocketRequestValidationError}
    assert handled == set(examples), handled ^ set(examples)
    for kind, exc in examples.items():
        response = await app.exception_handlers[kind](_request(agent=True), exc)
        detail = json.loads(response.body)["detail"]
        assert error_body.keyed(detail), (kind, detail)
    assert recorded == []


async def test_a_signed_out_session_is_not_a_refusal_the_ai_keeps(recorded):
    """A 401 refuses the session: the AI answers it with its own signed-out text and the
    browser reads its detail as the sign-in redirect's reason."""
    handler = app.exception_handlers[StarletteHTTPException]
    await handler(_request(agent=True), HTTPException(401, "Session expired"))
    assert recorded == []


async def test_outside_tests_the_body_is_sent_as_built(monkeypatch):
    monkeypatch.setattr(error_body, "unkeyed", None)
    handler = app.exception_handlers[StarletteHTTPException]
    response = await handler(_request(agent=True), HTTPException(409, "Plain text", headers={"X-A": "1"}))
    assert response.body == b'{"detail":"Plain text"}' and response.headers["X-A"] == "1"
