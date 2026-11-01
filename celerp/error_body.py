# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The one builder of the JSON error body the app answers a refused request with.

Every exception handler the app registers answers through ``error_response``. A route
marked ``x-celerp-agent`` is one the AI calls, and the AI keeps what it answered to show
the person who confirmed the action, then and after a reload, in that person's language.
So its refusal carries a catalog key (celerp.accounting_roles.refusal). Under tests
``unkeyed`` is a list, and every agent-route refusal without a key is recorded in it for
the test that met it to fail on; outside tests it is None and the body is sent as built."""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers
from starlette.requests import Request

unkeyed: list[str] | None = None


def keyed(detail: Any) -> bool:
    """Whether a refusal detail reads in each reader's language: a ``refusal`` with its
    catalog key, a list of them under ``errors``, or field validation (a list, which the AI
    shows as its own keyed invalid-arguments text)."""
    if isinstance(detail, list):
        return True
    if not isinstance(detail, dict):
        return False
    if detail.get("message_key"):
        return True
    errors = detail.get("errors")
    return isinstance(errors, list) and bool(errors) and all(
        isinstance(e, dict) and e.get("message_key") for e in errors)


def agent_route(scope) -> bool:
    """Whether the route the request reached is one the AI calls. A request whose method
    the matched path does not take is answered 405 by the router, not by that route. A
    request signed with a connected company's integration key (``Authorization: Peer``)
    is that company's own sync, never the AI's."""
    if Headers(scope=scope).get("authorization", "").startswith("Peer "):
        return False
    route = scope.get("route")
    methods = getattr(route, "methods", None)
    if methods and scope.get("method") not in methods:
        return False
    return (getattr(route, "openapi_extra", None) or {}).get("x-celerp-agent") is True


def check(where: str, status_code: int, detail: Any, agent: bool) -> None:
    """Record an agent route's refusal that carries no key, when tests are watching. A 401
    refuses the session, not the request: the AI answers it with its own signed-out text
    (jobs.caller_refusal, celerp_ai routes._action_error) and the browser reads its detail
    as the sign-in redirect's reason, so it is not a refusal the AI keeps."""
    if unkeyed is not None and agent and status_code >= 400 and status_code != 401 and not keyed(detail):
        unkeyed.append(f"{where} answered {status_code} with an unkeyed refusal: {detail!r}")


def error_response(request: Request, status_code: int, detail: Any, *,
                   headers: dict | None = None, **fields) -> Response:
    """The error body ``{"detail": detail, **fields}`` for ``request``."""
    check(f"{request.method} {request.url.path}", status_code, detail, agent_route(request.scope))
    if status_code in (204, 304) or status_code < 200:
        return Response(status_code=status_code, headers=headers)
    return JSONResponse(status_code=status_code, content={"detail": detail, **fields}, headers=headers)
