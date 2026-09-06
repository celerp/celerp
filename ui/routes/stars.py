# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""GitHub-star UI proxy routes + the badge-claim handshake.

The UI server (8080) proxies the star endpoints to the API (so the shell JS can call
same-origin) and hosts /stars/claim, which bounces the browser to the relay verify
flow and stores the returned credential.
"""
from __future__ import annotations

import html

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import ui.api_client as api
from ui.config import get_token as _token
from ui.i18n import t


def _claim_success_body(escaped_label: str) -> str:
    """Body copy for a successful badge claim. *escaped_label* must already be
    HTML-escaped by the caller; this only performs the t() interpolation."""
    return t("stars.claim_success_body", label=f"<strong>{escaped_label}</strong>")


def _claim_result_page(title: str, body: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Celerp</title>"
        "<div style='font-family:system-ui,sans-serif;max-width:560px;margin:80px auto;"
        "text-align:center;line-height:1.5'>"
        f"<h1>{html.escape(title)}</h1><p style='color:#555'>{body}</p>"
        "<p><a href='/' style='display:inline-block;margin-top:12px;padding:10px 20px;"
        f"background:#1f883d;color:#fff;border-radius:6px;text-decoration:none'>{t('stars.back_to_celerp')}</a></p>"
        "</div>"
    )


def setup_routes(app):

    @app.get("/stars/cta")
    async def proxy_star_cta(request: Request) -> Response:
        token = _token(request)
        if not token:
            return Response("{}", media_type="application/json")
        params = {"medium": request.query_params.get("medium", "footer")}
        async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
            try:
                r = await c.get("/stars/cta", params=params)
                return Response(r.content, media_type="application/json", status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response("{}", media_type="application/json")

    @app.get("/stars/badge")
    async def proxy_star_badge(request: Request) -> Response:
        token = _token(request)
        if not token:
            return Response('{"badge":null}', media_type="application/json")
        async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
            try:
                r = await c.get("/stars/badge")
                return Response(r.content, media_type="application/json", status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response('{"badge":null}', media_type="application/json")

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
        page as the return URL. With cred -> store the badge and confirm."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        cred = request.query_params.get("cred")
        if cred:
            async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
                try:
                    r = await c.post("/stars/badge", json={"cred": cred})
                    data = r.json()
                except Exception:
                    return HTMLResponse(_claim_result_page(
                        t("stars.claim_save_failed_title"), t("stars.claim_save_failed_body")))
            if data.get("error") or not data.get("badge"):
                return HTMLResponse(_claim_result_page(
                    t("stars.claim_no_star_title"), t("stars.claim_no_star_body")))
            label = html.escape(data["badge"]["label"])
            return HTMLResponse(_claim_result_page(
                t("stars.claim_success_title"), _claim_success_body(label)))

        # No cred yet: ask the API for the relay verify-start URL, return here after.
        return_url = str(request.base_url).rstrip("/") + "/stars/claim"
        async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
            try:
                r = await c.get("/stars/badge/verify-url", params={"return_url": return_url})
                url = r.json().get("url")
            except Exception:
                url = None
        if not url:
            return HTMLResponse(_claim_result_page(
                t("stars.claim_unavailable_title"), t("stars.claim_unavailable_body")))
        return RedirectResponse(url, status_code=302)
