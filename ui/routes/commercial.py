# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Commercial checkout mint route.

Hosts /commercial/checkout, the click-time handoff for every in-app subscribe or
top-up CTA. It resolves the destination through the central commercial policy
(``build_commercial_handoff``), and for a direct celerp.com checkout it mints a
single-use handoff token on the relay and 302-bounces the browser to the checkout
URL with that token appended. Minting at click (not at banner render) starts the
15-minute token clock at click and avoids a relay round-trip on every render.

Only the short-lived single-use handoff token ever rides the URL; the relay bearer
JWT and every long-lived instance credential stay server-side.
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx
from fasthtml.common import A, Div, H1, P, to_xml
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from ui.components.shell import auth_shell, page_title
from ui.config import get_token as _token
from ui.i18n import t

# The direct-checkout paths on the trusted Celerp handoff host that take a
# handoff token. A destination outside these (the Enterprise route, a partner
# support URL that happens to contain "/subscribe" in its own path) is not a
# self-serve Celerp checkout, so no token is minted for it.
_DIRECT_CHECKOUT_PATHS = ("/subscribe", "/subscribe/topup")

_MINT_TIMEOUT_S = 8.0


def _is_direct_checkout(destination: str) -> bool:
    """True only for a destination on the trusted Celerp handoff host at one of
    the allowed direct-checkout paths.

    Parses the URL rather than substring-matching it: a partner support URL such
    as ``https://partner.example/subscribe/help`` contains "/subscribe" but is not
    on the Celerp host, so it must never be classified as a direct checkout.
    ``HANDOFF_BASE`` (``celerp.gateway.state``) is the single source of truth for
    the trusted host, reused here rather than duplicated.
    """
    from celerp.gateway.state import HANDOFF_BASE

    trusted_host = urlparse(HANDOFF_BASE).hostname
    try:
        parsed = urlparse(destination)
    except ValueError:
        return False
    return parsed.hostname == trusted_host and parsed.path in _DIRECT_CHECKOUT_PATHS


def _mint_failed_page() -> str:
    """Neutral error page shown when the handoff mint could not complete. No
    redirect and no fabricated checkout URL: the user stays put and can retry.

    Renders through the standard app shell (auth_shell) and the same auth-card
    error pattern as ui/routes/auth.py::_api_error_page, rather than a bespoke
    inline-styled HTML fragment: same wrapper classes, the app stylesheet, and
    a single clear return action back into Web Access settings."""
    page = auth_shell(
        Div(
            Div(
                H1(t("commercial.checkout_unavailable_title"), cls="auth-title"),
                P(t("commercial.checkout_unavailable_body"), cls="auth-subtitle"),
                A(t("stars.back_to_celerp"), href="/settings/cloud", cls="btn btn--primary mt-md"),
                cls="auth-header",
            ),
            cls="auth-card",
        ),
        title=page_title("commercial.checkout_unavailable_title"),
    )
    return to_xml(page)


def _append_token(url: str, token: str) -> str:
    """Append the handoff token as a query param, keeping every existing param."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}handoff_token={token}"


async def _mint_handoff_token(purpose: str) -> str:
    """Mint a single-use handoff token on the relay for this authenticated
    instance and the given flow purpose.

    Reuses the app->relay authenticated call pattern (``fetch_relay_bearer``
    exchanges the instance API key for a short-lived bearer JWT, which gates the
    relay ``require_instance`` mint endpoint). The relay derives instance_id from
    the bearer, so the body carries only the purpose. Raises RuntimeError on any
    non-200 or transport failure so the caller degrades in one place.
    """
    from celerp.gateway.state import fetch_relay_bearer, relay_http_url

    async with httpx.AsyncClient(timeout=_MINT_TIMEOUT_S) as client:
        bearer = await fetch_relay_bearer(client)
        resp = await client.post(
            f"{relay_http_url()}/billing/checkout-handoff",
            json={"purpose": purpose},
            headers={"Authorization": f"Bearer {bearer}"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"handoff mint failed ({resp.status_code})")
    token = resp.json().get("handoff_token")
    if not token:
        raise RuntimeError("handoff mint returned no token")
    return token


def setup_routes(app):

    @app.get("/commercial/checkout")
    async def commercial_checkout(request: Request):
        """Click-time commercial handoff. Resolves the destination through the
        commercial policy; mints a single-use handoff token and appends it for a
        direct celerp.com checkout, or bounces token-free to a partner support or
        Enterprise destination. No app session bounces to login (mirrors
        ``/stars/claim``)."""
        if not _token(request):
            return RedirectResponse("/login", status_code=302)

        from celerp.config import ensure_instance_id
        from celerp.gateway.state import build_commercial_handoff

        intent = "topup" if request.query_params.get("intent") == "topup" else "subscribe"
        sku = request.query_params.get("sku", "")

        destination = build_commercial_handoff(ensure_instance_id(), intent, sku)

        # A partner support URL or the Enterprise route is not a self-serve
        # checkout, so no token is minted: bounce straight through.
        if not _is_direct_checkout(destination):
            return RedirectResponse(destination, status_code=302)

        try:
            token = await _mint_handoff_token("topup" if intent == "topup" else "subscribe")
        except (RuntimeError, httpx.HTTPError):
            return HTMLResponse(_mint_failed_page())

        return RedirectResponse(_append_token(destination, token), status_code=302)
