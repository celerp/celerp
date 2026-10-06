# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared UI security predicates."""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

import ui.api_client as api
from ui.components.shell import toast_header
from ui.config import get_token
from ui.i18n import get_lang, t


def is_safe_authorize_url(url: str) -> bool:
    """A broker-supplied OAuth authorize URL is safe to open/inject only if it is https and
    carries nothing that could break out of a <script> tag or href (angle brackets / control
    chars) or use a non-web scheme (javascript:/data:)."""
    return (
        bool(url)
        and urlparse(url).scheme == "https"
        and not any(c in url for c in "<>")
        and not any(ord(c) < 0x20 for c in url)
    )


NOT_PERMITTED = "not_permitted"
NOTICE_COOKIE = "celerp_notice"


def hx_redirect(url: str) -> Response:
    """Send an HTMX request to ``url`` as a real navigation. A 302 or an error page
    would be swapped into the fragment that fired the request, or end as a toast."""
    return Response(status_code=200, headers={"HX-Redirect": url})


def not_permitted_redirect(request: Request) -> Response:
    """Where a page the caller's role may not open sends them: the dashboard,
    which says why (a silent bounce reads as a broken link). An HTMX request
    navigates the whole page there. The reason travels in a one-shot cookie,
    never the URL, so only a real refusal shows it and the page that shows it
    clears it (NoticeMiddleware)."""
    response = (hx_redirect("/dashboard") if request.headers.get("hx-request")
                else RedirectResponse("/dashboard", status_code=302))
    response.set_cookie(NOTICE_COOKIE, NOT_PERMITTED, max_age=60, httponly=True, samesite="lax")
    return response


def not_permitted_pending(request: Request | None) -> bool:
    """Whether this request arrives from a refusal whose notice is not yet shown."""
    return request is not None and request.cookies.get(NOTICE_COOKIE) == NOT_PERMITTED


def take_not_permitted(request: Request | None) -> bool:
    """Whether to show the one-shot notice; marks it shown so NoticeMiddleware
    clears it in this same response."""
    if not not_permitted_pending(request):
        return False
    request.state.notice_shown = True
    return True


class NoticeMiddleware:
    """Pure ASGI middleware: clears the one-shot notice cookie on the response of
    the page that displayed it, so a reload shows nothing."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        state = scope.setdefault("state", {})

        async def send_clearing(message):
            if message["type"] == "http.response.start" and state.get("notice_shown"):
                clear = f"{NOTICE_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=lax"
                message = {**message, "headers": [*message.get("headers", []), (b"set-cookie", clear.encode())]}
            await send(message)

        await self._app(scope, receive, send_clearing)


async def owner_refusal(request: Request) -> Response | None:
    """Pages that act on the whole installation (modules, backups, updates, the
    Celerp account) answer the installation owner only. Each such handler calls
    this first, before it reads the request body or calls anything else; None
    means carry on."""
    token = get_token(request)
    if not token:
        return RedirectResponse("/login", status_code=302)
    if await api.installation_owner(token):
        return None
    message = t("error.installation_owner_only", get_lang(request))
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers=toast_header(message, "error"))
    return PlainTextResponse(message, status_code=403)
