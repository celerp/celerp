# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Cross-visitor authentication isolation on the gateway proxy path.

Security invariant these tests enforce:

    For every proxied HTTP request, the authentication presented to the local
    Celerp application must be derived EXCLUSIVELY from that external request.
    No authentication state from any previous, subsequent, or concurrent
    proxied request may affect it.

The batteries drive the REAL vulnerable path. A helper substitutes
httpx.AsyncClient with a real httpx.AsyncClient SUBCLASS that forces its
transport to an in-process ASGITransport pointed at a tiny local app. This keeps
httpx's real cookie jar (the stateful object the leak lives in) and only
redirects the outbound bytes in-process, so the jar-isolation property is tested
directly rather than mocked away. The local app records the cookies each request
presented (correlated by a test-controlled header) and can emit Set-Cookie or a
302 to /login, so a response's cookies feed back into the same jar exactly as
they do in production.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.routing import Route

from celerp.gateway.client import GatewayClient


# Header the gateway forwards verbatim; the local app echoes it back so a test can
# correlate each recorded request with the visitor that issued it.
_CORR_HEADER = "x-visitor-correlation"


def _make_local_app(seen: list[dict]):
    """A tiny local app standing in for the UI/API server.

    Every request records the exact Cookie header it received (what the local
    Celerp app would authenticate against) keyed by the correlation header, so a
    test can assert the local app saw only the cookies the external request
    supplied. A request to a protected route with no auth cookie redirects to
    /login, exactly as the real UI auth guard does; a request that names a cookie
    to set gets that Set-Cookie back so the jar records it.
    """

    async def _record(request):
        corr = request.headers.get(_CORR_HEADER, "")
        cookie_header = request.headers.get("cookie")
        seen.append({
            "corr": corr,
            "path": request.url.path,
            "cookie_header": cookie_header,
            "celerp_token": request.cookies.get("celerp_token"),
            "celerp_refresh": request.cookies.get("celerp_refresh"),
        })

        # A protected route with no valid auth cookie behaves like the real UI
        # guard: redirect to /login rather than serving authenticated content.
        if request.url.path == "/dashboard" and not request.cookies.get("celerp_token"):
            return RedirectResponse(url="/login", status_code=302)

        # The query drives an optional login response: ?set=<user> emits the
        # access + refresh cookies for <user>, mirroring a successful sign-in.
        set_user = request.query_params.get("set")
        resp = PlainTextResponse("ok")
        if set_user:
            resp.set_cookie("celerp_token", set_user, path="/")
            resp.set_cookie("celerp_refresh", f"refresh-{set_user}", path="/")
        return resp

    return Starlette(routes=[
        Route("/{path:path}", _record, methods=["GET", "POST"]),
    ])


def _install_asgi_client(monkeypatch, app):
    """Route every gateway-built AsyncClient through an in-process ASGITransport
    to `app`, keeping the real httpx cookie jar. Returns the list of every client
    the gateway constructed so a test can assert per-request construction."""
    constructed: list = []

    class _ASGIBackedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            # Drop any real network transport the gateway passes and force an
            # in-process one; keep every other kwarg (timeout, follow_redirects,
            # and crucially the default real cookie jar).
            kwargs.pop("transport", None)
            kwargs["transport"] = httpx.ASGITransport(app=app)
            super().__init__(*args, **kwargs)
            constructed.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", _ASGIBackedClient)
    return constructed


def _client() -> GatewayClient:
    return GatewayClient(
        gateway_token="test-gateway-token",
        instance_id="test-instance-id",
        gateway_url="wss://relay.celerp.com/ws/connect",
    )


async def _proxy(gw, *, corr, path="/x", query="", cookie=None):
    """Drive one proxied request through the real _handle_proxy_request path.

    `cookie`, when given, is the external browser Cookie header for this request;
    absent means the visitor supplied none (the anonymous case).
    """
    headers = {_CORR_HEADER: corr}
    if cookie is not None:
        headers["cookie"] = cookie
    await gw._handle_proxy_request({
        "id": corr, "method": "GET", "path": path,
        "query": query, "headers": headers, "body_b64": "",
    })


@pytest.fixture
def gw(monkeypatch):
    """A GatewayClient wired so _handle_proxy_request runs for real but _send is a
    no-op and no websocket is required."""
    g = _client()

    async def _noop_send(ws, msg):
        pass
    monkeypatch.setattr(g.__class__, "_send", staticmethod(_noop_send))
    g._ws = object()
    return g


def _for(seen, corr):
    """The single recorded local request for a correlation id."""
    matches = [r for r in seen if r["corr"] == corr]
    assert len(matches) == 1, f"expected exactly one local request for {corr}, got {matches}"
    return matches[0]


@pytest.mark.asyncio
async def test_proxied_auth_derived_exclusively__sequential(gw, monkeypatch):
    """Sequential identity isolation: after user A signs in (Set-Cookie
    celerp_token=USER_A), an anonymous visitor B reaches the local app with NO
    celerp cookie, and a later user C (Cookie celerp_token=USER_C) reaches the
    local app as exactly USER_C. The invariant is then re-checked in reverse.

    Red at merge-base: the one shared client jar caches USER_A's Set-Cookie, so
    B's local request carries celerp_token=USER_A and the no-cookie assertion
    fails.
    """
    seen: list[dict] = []
    _install_asgi_client(monkeypatch, _make_local_app(seen))

    # A signs in: the local app returns Set-Cookie celerp_token=USER_A.
    await _proxy(gw, corr="A", path="/x", query="set=USER_A", cookie="celerp_token=USER_A")
    # B is anonymous: supplies no Cookie header at all.
    await _proxy(gw, corr="B", path="/x")
    # C supplies exactly its own cookie.
    await _proxy(gw, corr="C", path="/x", cookie="celerp_token=USER_C")
    # Reverse order re-check: anonymous D after C must again be clean.
    await _proxy(gw, corr="D", path="/x")

    assert _for(seen, "A")["celerp_token"] == "USER_A"
    b = _for(seen, "B")
    assert b["celerp_token"] is None, (
        f"anonymous B must reach the local app with NO celerp cookie, saw "
        f"{b['cookie_header']!r}")
    c = _for(seen, "C")
    assert c["celerp_token"] == "USER_C", (
        f"C must reach the local app as exactly USER_C, saw {c['cookie_header']!r}")
    d = _for(seen, "D")
    assert d["celerp_token"] is None, (
        f"anonymous D must reach the local app with NO celerp cookie, saw "
        f"{d['cookie_header']!r}")


@pytest.mark.asyncio
async def test_proxied_auth_derived_exclusively__concurrent(gw, monkeypatch):
    """Concurrent multi-user isolation: authenticated and anonymous visitors
    launched with a true in-flight overlap (asyncio.gather, matching _spawn_proxy's
    independent tasks). Each authenticated visitor supplies its own sentinel cookie
    AND rotates cookies via Set-Cookie (poisoning any shared jar); each anonymous
    visitor supplies no Cookie at all and must reach the local app clean. Every
    local request must see exactly its own external cookie state and nothing else.

    Red at merge-base: the one shared jar caches the rotated sentinels, so at least
    one concurrent anonymous request has a foreign token injected into it.
    """
    seen: list[dict] = []
    _install_asgi_client(monkeypatch, _make_local_app(seen))

    n = 40
    tasks = []
    for i in range(n):
        corr = f"u{i}"
        token = f"token-{i}"
        if i % 2 == 0:
            # Authenticated visitor: presents its own cookie and rotates it via
            # Set-Cookie, feeding the sentinel into whatever jar carries it.
            tasks.append(_proxy(gw, corr=corr, path="/x", query=f"set={token}",
                                cookie=f"celerp_token={token}"))
        else:
            # Anonymous visitor: no Cookie header at all.
            tasks.append(_proxy(gw, corr=corr, path="/x"))
    await asyncio.gather(*tasks)

    for i in range(n):
        rec = _for(seen, f"u{i}")
        if i % 2 == 0:
            assert rec["celerp_token"] == f"token-{i}", (
                f"concurrent authenticated request u{i} must present only its own "
                f"token, saw {rec['cookie_header']!r}")
        else:
            assert rec["celerp_token"] is None, (
                f"concurrent anonymous request u{i} must present NO token, saw "
                f"{rec['cookie_header']!r}")


@pytest.mark.asyncio
async def test_proxied_auth_derived_exclusively__anonymous_after_authenticated(gw, monkeypatch):
    """Anon-after-auth through the gateway path: after an authenticated request
    sets access + refresh cookies, a fresh anonymous request to a protected route
    reaches the local app with no auth cookie, and the local app returns 302
    ->/login.

    Red at merge-base: the poisoned shared jar injects the leaked celerp_token
    into the anonymous /dashboard request, so the local app serves it (200)
    instead of redirecting to /login.
    """
    seen: list[dict] = []
    _install_asgi_client(monkeypatch, _make_local_app(seen))

    # Authenticated visitor: sets both access and refresh cookies.
    await _proxy(gw, corr="auth", path="/x", query="set=USER_A", cookie="celerp_token=USER_A")
    # Anonymous visitor hits a protected route with no Cookie header.
    await _proxy(gw, corr="anon", path="/dashboard")

    anon = _for(seen, "anon")
    assert anon["celerp_token"] is None, (
        f"the anonymous /dashboard request must carry no auth cookie, saw "
        f"{anon['cookie_header']!r}")
    assert anon["celerp_refresh"] is None, (
        f"the anonymous request must carry no refresh cookie, saw "
        f"{anon['cookie_header']!r}")


@pytest.mark.asyncio
async def test_proxied_auth_derived_exclusively__cross_port(gw, monkeypatch):
    """Cross-port isolation: an authenticated UI-port request must not leak its
    cookie into a later anonymous /share (API-port) request, nor into a later
    anonymous UI request. Both local targets share host 127.0.0.1 and differ only
    in port.

    Red at merge-base: httpx's default cookie jar keys by domain/path, not port,
    so the resident UI cookie is injected into the subsequent anonymous API-port
    /share request (and the anonymous UI request).
    """
    seen: list[dict] = []
    _install_asgi_client(monkeypatch, _make_local_app(seen))

    # Authenticated UI-port request sets a cookie.
    await _proxy(gw, corr="ui-auth", path="/x", query="set=USER_A", cookie="celerp_token=USER_A")
    # Anonymous public-share request routes to the API port (/share).
    await _proxy(gw, corr="share-anon", path="/share/tok123")
    # Anonymous UI-port request.
    await _proxy(gw, corr="ui-anon", path="/y")

    share = _for(seen, "share-anon")
    assert share["celerp_token"] is None, (
        f"the anonymous /share request must carry no UI auth cookie, saw "
        f"{share['cookie_header']!r}")
    ui_anon = _for(seen, "ui-anon")
    assert ui_anon["celerp_token"] is None, (
        f"the anonymous UI request must carry no auth cookie, saw "
        f"{ui_anon['cookie_header']!r}")


@pytest.mark.asyncio
async def test_proxied_auth_derived_exclusively__response_ownership(gw, monkeypatch):
    """Green-at-head guard (not red-first): two users signing in simultaneously
    each get back their own Set-Cookie and nothing else. Response headers are
    already per-request objects, so this locks response-ownership against a future
    request-id/concurrency regression.
    """
    sent: list = []

    async def _capture_send(ws, msg):
        sent.append(msg)
    monkeypatch.setattr(gw.__class__, "_send", staticmethod(_capture_send))

    seen: list[dict] = []
    _install_asgi_client(monkeypatch, _make_local_app(seen))

    await asyncio.gather(
        _proxy(gw, corr="A", path="/x", query="set=USER_A", cookie="celerp_token=USER_A"),
        _proxy(gw, corr="B", path="/x", query="set=USER_B", cookie="celerp_token=USER_B"),
    )

    def _set_cookies_for(corr):
        msgs = [m for m in sent if m["payload"]["id"] == corr]
        assert len(msgs) == 1, f"expected one response for {corr}, got {msgs}"
        return [v for k, v in msgs[0]["payload"]["headers"] if k.lower() == "set-cookie"]

    a_cookies = _set_cookies_for("A")
    b_cookies = _set_cookies_for("B")
    assert any("celerp_token=USER_A" in c for c in a_cookies), a_cookies
    assert not any("USER_B" in c for c in a_cookies), a_cookies
    assert any("celerp_token=USER_B" in c for c in b_cookies), b_cookies
    assert not any("USER_A" in c for c in b_cookies), b_cookies


@pytest.mark.asyncio
async def test_proxied_response_body_survives_client_close(gw, monkeypatch):
    """Green-at-head guard (not red-first): a proxied response with a multi-KB
    body serializes fully even though the generation check and multi_items()
    header serialization run after the per-request client context exits. A
    non-streaming client.request() buffers the whole body before returning, so the
    post-context reads never touch a closed client.
    """
    import base64

    big = b"Z" * 8192

    async def _big_body(request):
        return PlainTextResponse(big.decode())

    app = Starlette(routes=[Route("/{path:path}", _big_body, methods=["GET", "POST"])])

    sent: list = []

    async def _capture_send(ws, msg):
        sent.append(msg)
    monkeypatch.setattr(gw.__class__, "_send", staticmethod(_capture_send))
    _install_asgi_client(monkeypatch, app)

    await _proxy(gw, corr="big", path="/x")

    payload = sent[-1]["payload"]
    body = base64.b64decode(payload["body_b64"])
    assert body == big, "the full response body must survive serialization after the client closes"
