# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The one builder of the JSON error body the app answers a refused request with.

Every exception handler the app registers answers through ``error_response``. A route
marked ``x-celerp-agent`` is one the AI calls, and the AI keeps what it answered to show
the person who confirmed the action, then and after a reload, in that person's language.
So its refusal carries a catalog key (celerp.accounting_roles.refusal). One that arrives
without a key is answered under the generic key ``error.refusal_as_written``, carrying the
English the person reads today as its text. Under tests ``unkeyed`` is a list, and every
agent-route refusal without a key is recorded in it for the test that met it to fail on;
outside tests it is None. A refusal written with t() carries its key (ui.i18n.Text) and is
answered keyed on every route, whatever path raised it."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers
from starlette.requests import Request

unkeyed: list[str] | None = None


def keyed(detail: Any) -> bool:
    """Whether a refusal detail reads in each reader's language: a ``refusal`` with its
    catalog key, a list of them under ``errors``, or field validation (a list of entries
    each naming its field ``loc`` and its ``type``, which the AI shows as its own keyed
    invalid-arguments text). A list holding anything else, a sentence say, is not keyed."""
    if isinstance(detail, list):
        return bool(detail) and all(isinstance(e, dict) and "loc" in e and "type" in e for e in detail)
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


def unkeyed_refusal(status_code: int, detail: Any, agent: bool) -> bool:
    """Whether an agent route answers a refusal that carries no key. A 401 refuses the
    session, not the request: the AI answers it with its own signed-out text
    (jobs.caller_refusal, celerp_ai routes._action_error) and the browser reads its detail
    as the sign-in redirect's reason, so it is not a refusal the AI keeps."""
    return agent and status_code >= 400 and status_code != 401 and not keyed(detail)


def check(where: str, status_code: int, detail: Any, agent: bool) -> None:
    """Record an agent route's refusal that carries no key, when tests are watching."""
    if unkeyed is not None and unkeyed_refusal(status_code, detail, agent):
        unkeyed.append(f"{where} answered {status_code} with an unkeyed refusal: {detail!r}")


def as_written(detail: Any) -> dict:
    """An unkeyed refusal under the generic key, its text the English it reads as today;
    any other fields of a dict detail kept beside it."""
    from celerp.accounting_roles import refusal
    from ui.i18n import english, refusal_text

    with english():
        text = refusal_text(detail)
    return {**(detail if isinstance(detail, dict) else {}), **refusal("error.refusal_as_written", text, text=text)}


def as_refusal(detail: Any) -> Any:
    """``detail`` as the refusal it reads as: a t() text (ui.i18n.Text) as its key's
    ``refusal``, a t() text among its params as a refusal of its own; anything else as given."""
    from ui.i18n import Text

    if not isinstance(detail, Text):
        return detail
    from celerp.accounting_roles import catalog_refusal

    return catalog_refusal(detail.key, **{name: _param(value) for name, value in detail.params.items()})


def _param(value: Any) -> Any:
    """A t() param as JSON carries it: a t() text as its refusal, a date as its day (the
    ``refusal`` turns it into one), any other value that is not JSON as the text t() wrote."""
    if isinstance(value, str):
        return as_refusal(value)
    if value is None or isinstance(value, (int, float, date)):
        return value
    return str(value)


def error_response(request: Request, status_code: int, detail: Any, *,
                   headers: dict | None = None, **fields) -> Response:
    """The error body ``{"detail": detail, **fields}`` for ``request``, a t() text keyed and
    an agent route's unkeyed refusal under the generic key."""
    detail, agent = as_refusal(detail), agent_route(request.scope)
    check(f"{request.method} {request.url.path}", status_code, detail, agent)
    if unkeyed_refusal(status_code, detail, agent):
        detail = as_written(detail)
    if status_code in (204, 304) or status_code < 200:
        return Response(status_code=status_code, headers=headers)
    return JSONResponse(status_code=status_code, content={"detail": detail, **fields}, headers=headers)
