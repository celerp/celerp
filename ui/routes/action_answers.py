# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""How a page answers an action: done (the page loads again with a toast saying what it did,
or the view is drawn again from a read after it) or refused (the API's reason, where the user
is, with the views its control changed drawn again from what is stored).

Every UI action route of the subscription and vertical pages answers through these helpers;
the subscriptions status census names them.
"""
from __future__ import annotations

from starlette.responses import Response

import ui.api_client as api
from ui.api_client import APIError
from ui.components.encode import html_response
from ui.components.shell import toast_header
from ui.i18n import t
from ui.routes.documents import _action_error


def _refused(e: APIError):
    """The API's refusal of an action, shown where the user is."""
    api.raise_if_signed_out(e)
    return _action_error(e)


def _done(back: str, message: str, kind: str = "success") -> Response:
    """An action that worked: go back to *back* and say what it did there."""
    return Response(status_code=204, headers={"HX-Redirect": back, **toast_header(message, kind)})


async def _redrawn(draw, fallback=None, said: str = ""):
    """What a done action shows next, read after it: ``draw()``'s answer. When a read fails,
    the action is still done: ``fallback()`` and ``said`` (what the action's own answer says)
    with a warning that offers Try again, never a refusal."""
    try:
        return await draw()
    except APIError as e:
        api.raise_if_signed_out(e)
        warning = f"{said} {t('subscriptions.done_not_redrawn')}".strip()
        return html_response(fallback() if fallback else (), headers=toast_header(warning, "warning"))


async def _refused_redrawn(e: APIError, draw):
    """A refused action whose control already shows the refused value: the refusal with every view
    drawn again from what is stored (``draw()``'s markup). When that read fails, the refusal alone."""
    refusal = _refused(e)
    try:
        views = await draw()
    except APIError as read_error:
        api.raise_if_signed_out(read_error)
        return refusal
    return html_response(views, headers={k: v for k, v in refusal.headers.items()
                                        if k not in ("content-length", "content-type")})
