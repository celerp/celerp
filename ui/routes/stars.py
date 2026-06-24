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

from ui.config import API_BASE, get_token as _token


def _claim_result_page(title: str, body: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Celerp</title>"
        "<div style='font-family:system-ui,sans-serif;max-width:560px;margin:80px auto;"
        "text-align:center;line-height:1.5'>"
        f"<h1>{html.escape(title)}</h1><p style='color:#555'>{body}</p>"
        "<p><a href='/' style='display:inline-block;margin-top:12px;padding:10px 20px;"
        "background:#1f883d;color:#fff;border-radius:6px;text-decoration:none'>Back to Celerp</a></p>"
        "</div>"
    )


def setup_routes(app):

    @app.get("/stars/cta")
    async def proxy_star_cta(request: Request) -> Response:
        token = _token(request)
        if not token:
            return Response("{}", media_type="application/json")
        params = {"medium": request.query_params.get("medium", "footer")}
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            try:
                r = await c.get("/stars/cta", params=params, headers={"Authorization": f"Bearer {token}"})
                return Response(r.content, media_type="application/json", status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response("{}", media_type="application/json")

    @app.get("/stars/badge")
    async def proxy_star_badge(request: Request) -> Response:
        token = _token(request)
        if not token:
            return Response('{"badge":null}', media_type="application/json")
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            try:
                r = await c.get("/stars/badge", headers={"Authorization": f"Bearer {token}"})
                return Response(r.content, media_type="application/json", status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response('{"badge":null}', media_type="application/json")

    @app.post("/stars/dismiss")
    async def proxy_star_dismiss(request: Request) -> Response:
        token = _token(request)
        if not token:
            return Response(status_code=401)
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            try:
                r = await c.post("/stars/dismiss", headers={"Authorization": f"Bearer {token}"})
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
            async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
                try:
                    r = await c.post("/stars/badge", json={"cred": cred},
                                     headers={"Authorization": f"Bearer {token}"})
                    data = r.json()
                except Exception:
                    return HTMLResponse(_claim_result_page(
                        "Couldn't save your badge", "Please try claiming again from Celerp."))
            if data.get("error") or not data.get("badge"):
                return HTMLResponse(_claim_result_page(
                    "We couldn't verify a star", "Star Celerp on GitHub, then claim again."))
            label = html.escape(data["badge"]["label"])
            return HTMLResponse(_claim_result_page(
                "Thank you", f"You are recognised as <strong>{label}</strong>."))

        # No cred yet: ask the API for the relay verify-start URL, return here after.
        # Carry the public-listing opt-in choice (set by the claim card's checkbox).
        return_url = str(request.base_url).rstrip("/") + "/stars/claim"
        params = {"return_url": return_url}
        if request.query_params.get("opt_in"):
            params["opt_in"] = "1"
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            try:
                r = await c.get("/stars/badge/verify-url", params=params,
                                headers={"Authorization": f"Bearer {token}"})
                url = r.json().get("url")
            except Exception:
                url = None
        if not url:
            return HTMLResponse(_claim_result_page(
                "Cloud unavailable", "Couldn't reach the Celerp relay. Try again later."))
        return RedirectResponse(url, status_code=302)
