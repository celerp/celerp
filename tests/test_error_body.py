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


def _request(agent: bool, headers: list | None = None) -> Request:
    route = next(r for _p, _m, r in _api_routes(app)
                 if ((r.openapi_extra or {}).get("x-celerp-agent") is True) == agent)
    return Request({"type": "http", "method": sorted(route.methods)[0], "path": route.path,
                    "headers": headers or [], "query_string": b"", "route": route})


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


@pytest.mark.parametrize("detail", ["Barcode '1001' already exists", {"code": "x", "message": "No key."},
                                    ["That name is taken"], [catalog_refusal("error.server_error"), "That name is taken"], []])
async def test_an_agent_route_refusal_without_a_key_is_recorded(recorded, detail):
    handler = app.exception_handlers[StarletteHTTPException]
    response = await handler(_request(agent=True), HTTPException(409, detail))
    assert response.status_code == 409
    assert len(recorded) == 1 and repr(detail) in recorded[0]


@pytest.mark.parametrize("detail", [catalog_refusal("error.server_error"),
                                    {"errors": [catalog_refusal("error.server_error")]},
                                    [{"loc": ["body", "sku"], "msg": "Field required", "type": "missing"}]])
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

    from celerp.accounting_roles import KeyedRefusal
    from celerp.inventory_codes import BarcodeConflictError
    from celerp.services.account_roles import PostingRoleError
    from celerp.services.business_time import InvalidTimezoneError
    from celerp.services.journal_accounts import UnbalancedJournalEntry

    return {
        404: StarletteHTTPException(404),
        StarletteHTTPException: HTTPException(409, catalog_refusal("error.server_error")),
        Exception: RuntimeError("boom"),
        RequestValidationError: RequestValidationError([{"loc": ["body", "sku"], "msg": "Field required", "type": "missing"}]),
        # The handler reads nothing from the exception; a Limit needs a live limiter to build.
        RateLimitExceeded: RateLimitExceeded.__new__(RateLimitExceeded),
        KeyedRefusal: BarcodeConflictError("1001"),
        InvalidTimezoneError: InvalidTimezoneError("error.company_timezone_unknown"),
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


async def test_a_connected_companys_key_is_not_the_ai(recorded):
    """A request signed with a connected company's integration key is that company's own
    sync, never the AI's, so its refusal is not one the AI keeps."""
    handler = app.exception_handlers[StarletteHTTPException]
    await handler(_request(agent=True, headers=[(b"authorization", b"Peer k")]),
                  HTTPException(403, "Integration operation unavailable"))
    assert recorded == []


@pytest.mark.parametrize("detail, text", [
    ("Barcode '1001' already exists", "Barcode '1001' already exists"),
    (["That name is taken", "Try another"], "That name is taken Try another"),
    ({"code": "x", "message": "No key."}, "No key."),
])
async def test_an_unkeyed_agent_route_refusal_is_answered_keyed_with_its_own_text(recorded, detail, text):
    """Class closure: a refusal no guard saw reaches the AI keyed all the same, under one
    generic key carrying the English the person reads today, so nothing is invented and
    the AI's record still reads after a reload. Other fields of the detail are kept."""
    from ui.i18n import refusal_text, set_lang

    handler = app.exception_handlers[StarletteHTTPException]
    response = await handler(_request(agent=True), HTTPException(409, detail, headers={"X-A": "1"}))
    body = json.loads(response.body)["detail"]
    assert response.status_code == 409 and response.headers["X-A"] == "1"
    assert error_body.keyed(body) and body["message_key"] == "error.refusal_as_written"
    assert body["message"] == text and body["params"] == {"text": text}
    if isinstance(detail, dict):
        assert body["code"] == detail["code"]
    set_lang("th")
    try:
        assert refusal_text(body) == text
    finally:
        set_lang("en")
    assert len(recorded) == 1


async def test_a_keyed_agent_route_refusal_is_answered_as_given(recorded):
    given = catalog_refusal("error.permission_missing", label="Sales")
    handler = app.exception_handlers[StarletteHTTPException]
    response = await handler(_request(agent=True), HTTPException(403, given))
    assert json.loads(response.body)["detail"] == given and recorded == []


async def test_a_route_the_ai_does_not_call_answers_as_built(recorded):
    handler = app.exception_handlers[StarletteHTTPException]
    response = await handler(_request(agent=False), HTTPException(409, "Plain text", headers={"X-A": "1"}))
    assert response.body == b'{"detail":"Plain text"}' and response.headers["X-A"] == "1"


@pytest.mark.parametrize("agent", [True, False])
async def test_a_refusal_written_with_t_is_answered_keyed_and_reads_as_before(recorded, agent):
    """A t() text carries its key, so whatever path raised it (an instance method, a
    dispatch by name, a middleware), the one builder answers it keyed; the person reads the
    same sentence as before, in their language, live and after a reload."""
    from ui.i18n import english, refusal_text, set_lang, t

    set_lang("th")
    try:
        text = t("error.permission_missing", label=t("error.access_changed"))
        assert text == str(text) and json.dumps(text) == json.dumps(str(text))
        handler = app.exception_handlers[StarletteHTTPException]
        response = await handler(_request(agent=agent), HTTPException(403, text))
        detail = json.loads(response.body)["detail"]
        assert detail["message_key"] == "error.permission_missing"
        assert refusal_text(detail) == text
        assert recorded == []
        set_lang("es")
        assert refusal_text(detail) == t("error.permission_missing", label=t("error.access_changed"))
        with english():
            assert detail["message"] == refusal_text(detail)
    finally:
        set_lang("en")


class _Refuser:
    """Code the static walker cannot reach by name: a refusal raised from an instance
    method, and one reached by a dispatch on a name built at run time."""

    def refuse(self):
        from ui.i18n import t

        raise HTTPException(409, t("error.access_changed"))


def _dispatched():
    from ui.i18n import t

    raise HTTPException(409, t("error.access_changed"))


def _agent_app():
    """An app whose routes the AI calls, answered by the app's own exception handlers."""
    import sys

    from fastapi import FastAPI

    probe = FastAPI()
    for kind, handler in app.exception_handlers.items():
        probe.add_exception_handler(kind, handler)
    agent = {"x-celerp-agent": True}
    probe.add_api_route("/instance", lambda: _Refuser().refuse(), methods=["POST"], openapi_extra=agent)
    probe.add_api_route("/dispatch", lambda: getattr(sys.modules[__name__], "_dis" + "patched")(),
                        methods=["POST"], openapi_extra=agent)
    return probe


@pytest.mark.parametrize("path", ["/instance", "/dispatch"])
async def test_a_t_refusal_no_walker_can_reach_is_answered_keyed(recorded, path):
    """Whatever path raised a t() refusal, the one builder answers it keyed: neither the
    static walker (direct calls only) nor the strict check (exercised paths only) is
    needed to keep it in the reader's language."""
    import httpx

    from ui.i18n import refusal_text, set_lang, t

    set_lang("th")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_agent_app()), base_url="http://t") as c:
            response = await c.post(path)
        detail = response.json()["detail"]
        assert response.status_code == 409 and detail["message_key"] == "error.access_changed"
        assert refusal_text(detail) == t("error.access_changed")
        assert recorded == []
    finally:
        set_lang("en")


def test_a_middleware_answers_through_the_one_builder():
    """A middleware answers before any route is matched, so it is never an agent route to
    the strict check; it answers only through error_response, which keys a t() refusal."""
    import inspect

    built = [m.cls.__qualname__ for m in app.user_middleware
             if any(f"{kind}(" in inspect.getsource(m.cls)
                    for kind in ("JSONResponse", "PlainTextResponse", "HTMLResponse", " Response"))]
    assert not built, built


async def test_a_middleware_refusal_is_answered_keyed(recorded, monkeypatch):
    """A module route asked before Celerp has finished starting is refused by a
    middleware; the AI reaches it the same way, so the refusal is keyed."""
    from celerp.middleware import ModuleStartupMiddleware
    from celerp.modules import loader, outcome

    async def not_ready(*_args):
        return False

    monkeypatch.setattr(outcome, "ui_report_applied", lambda: False)
    monkeypatch.setattr(outcome, "confirm_ui_report", not_ready)
    monkeypatch.setattr(loader, "route_module", lambda _scope: "module")
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "GET", "path": "/m", "headers": [], "query_string": b"", "app": app}
    await ModuleStartupMiddleware(app)(scope, receive, send)
    assert sent[0]["status"] == 503
    assert json.loads(sent[1]["body"])["detail"]["message_key"] == "error.still_starting"
    assert recorded == []
