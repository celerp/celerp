# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""GitHub-star UI proxy routes + the badge-claim handshake.

The UI server (8080) proxies the star endpoints to the API (so the shell JS can call
same-origin) and hosts /stars/claim, which bounces the browser to the relay verify
flow and, once the user confirms, stores the returned credential.
"""
from __future__ import annotations

import httpx
from fasthtml.common import A, Body, Button, Div, Form, H1, Head, Html, Input, Meta, P, Strong, Title
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.config import get_token as _token
from ui.i18n import get_lang, t
from ui.components.encode import html_response


def _claim_success_body(label: str) -> tuple:
    """Body copy for a successful badge claim: the translated sentence with the label in bold.
    {label} marks where the label sits so each locale can place it in its own sentence."""
    pre, _, post = t("stars.claim_success_body").partition("{label}")
    return pre, Strong(label), post


_CLAIM_BUTTON = ("display:inline-block;margin-top:12px;padding:10px 20px;background:#1f883d;color:#fff;"
                 "border:0;border-radius:6px;font:inherit;text-decoration:none;cursor:pointer")


def _claim_result_page(title: str, body, action=None):
    """A standalone result page; *body* is text or nodes, *action* replaces the back link."""
    if action is None:
        action = P(A(t("stars.back_to_celerp"), href="/", style=_CLAIM_BUTTON))
    return Html(
        Head(Meta(charset="utf-8"), Title("Celerp")),
        Body(Div(
            H1(title), P(body, style="color:#555"), action,
            style="font-family:system-ui,sans-serif;max-width:560px;margin:80px auto;"
                  "text-align:center;line-height:1.5",
        )),
    )


def _claim_confirm_page(cred: str):
    """The relay returns the browser here by a GET carrying the credential; the badge is
    saved only when the signed-in user confirms it with a POST, so a link alone never
    stores a credential to whoever opens it."""
    action = (
        Form(
            Input(type="hidden", name="cred", value=cred),
            Button(t("stars.claim_confirm_button"), type="submit", style=_CLAIM_BUTTON),
            method="post", action="/stars/claim",
        ),
        P(A(t("stars.back_to_celerp"), href="/", style="color:#555")),
    )
    return _claim_result_page(t("stars.claim_confirm_title"), t("stars.claim_confirm_body"), action)


def setup_routes(app):

    async def _proxy_get(request: Request, path: str, nothing: str, params: dict | None = None) -> Response:
        """The API's answer for the signed-in user, or *nothing* when there is no
        sign-in, the API no longer accepts it, or the API is unreachable."""
        token = _token(request)
        if not token:
            return Response(nothing, media_type="application/json")
        async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
            try:
                r = await c.get(path, params=params)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response(nothing, media_type="application/json")
        if r.status_code == 401:
            return Response(nothing, media_type="application/json")
        return Response(r.content, media_type="application/json", status_code=r.status_code)

    @app.get("/stars/cta")
    async def proxy_star_cta(request: Request) -> Response:
        params = {"medium": request.query_params.get("medium", "footer"), "lang": get_lang(request)}
        return await _proxy_get(request, "/stars/cta", "{}", params)

    @app.get("/stars/badge")
    async def proxy_star_badge(request: Request) -> Response:
        return await _proxy_get(request, "/stars/badge", '{"badge":null}')

    @app.post("/stars/dismiss")
    async def proxy_star_dismiss(request: Request) -> Response:
        token = _token(request)
        if not token:
            return Response(status_code=401)
        async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
            try:
                r = await c.post("/stars/dismiss")
                return Response(r.content, status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response(status_code=503)

    @app.get("/stars/claim")
    async def stars_claim(request: Request):
        """Badge-claim handshake. No cred -> bounce to the relay verify flow with this
        page as the return URL. With cred -> ask the user to confirm saving the badge."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        cred = request.query_params.get("cred")
        if cred:
            return html_response(_claim_confirm_page(cred))

        # No cred yet: ask the API for the relay verify-start URL, return here after.
        return_url = str(request.base_url).rstrip("/") + "/stars/claim"
        async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
            try:
                r = await c.get("/stars/badge/verify-url", params={"return_url": return_url})
                url = r.json().get("url")
            except Exception:
                url = None
        if not url:
            return html_response(_claim_result_page(
                t("stars.claim_unavailable_title"), t("stars.claim_unavailable_body")))
        return RedirectResponse(url, status_code=302)

    @app.post("/stars/claim")
    async def stars_claim_save(request: Request):
        """Store the confirmed badge and say how it went."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=303)
        cred = (await request.form()).get("cred")
        if not cred:
            return RedirectResponse("/stars/claim", status_code=303)
        async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
            try:
                r = await c.post("/stars/badge", json={"cred": cred})
                data = r.json()
            except Exception:
                return html_response(_claim_result_page(
                    t("stars.claim_save_failed_title"), t("stars.claim_save_failed_body")))
        if data.get("error") or not data.get("badge"):
            return html_response(_claim_result_page(
                t("stars.claim_no_star_title"), t("stars.claim_no_star_body")))
        return html_response(_claim_result_page(
            t("stars.claim_success_title"), _claim_success_body(data["badge"]["label"])))
