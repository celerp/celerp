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
