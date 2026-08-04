# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for GatewayClient session token handling and message dispatch.

Tests the session token write/refresh logic without a live WebSocket server
by calling _dispatch() directly on the client instance.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import httpx
import pytest

from celerp.gateway.client import GatewayClient
import celerp.gateway.state as gw_state


@pytest.fixture
def client():
    return GatewayClient(
        gateway_token="test-gateway-token",
        instance_id="test-instance-id",
        gateway_url="wss://relay.celerp.com/ws/connect",
    )


@pytest.fixture(autouse=True)
def reset_session_token():
    original = gw_state.get_session_token()
    original_sub = gw_state.get_subscription_state()
    gw_state.set_session_token("")
    yield
    gw_state.set_session_token(original)
    gw_state.set_subscription_state(*original_sub)


# ── C4: relay-state sentinel on stdout (consumed by the Electron host) ─────────

def test_set_status_emits_sentinel_on_change(client, capsys):
    """_set_status prints one CELERP_RELAY_STATE line per transition and updates status."""
    client._set_status("active")
    out = capsys.readouterr().out
    assert "CELERP_RELAY_STATE=active" in out
    assert client.relay_status == "active"


def test_set_status_is_idempotent_no_repeat_emit(client, capsys):
    """Re-setting the same status emits nothing (Electron must not flap the blocker)."""
    client._set_status("active")
    capsys.readouterr()  # drain the first emit
    client._set_status("active")
    assert capsys.readouterr().out == ""
    assert client.relay_status == "active"


def test_set_status_emits_each_distinct_transition(client, capsys):
    """Distinct transitions each emit their own sentinel, in order."""
    for s in ("connecting", "active", "inactive"):
        client._set_status(s)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("CELERP_RELAY_STATE=")]
    assert lines == [
        "CELERP_RELAY_STATE=connecting",
        "CELERP_RELAY_STATE=active",
        "CELERP_RELAY_STATE=inactive",
    ]


@pytest.mark.asyncio
async def test_hello_ack_emits_active_sentinel(client, capsys):
    """The real active transition (hello_ack dispatch) emits the active sentinel - the signal
    Electron holds the power-save assertion on (C4)."""
    await client._dispatch({"type": "hello_ack", "payload": {"instance_id": "test-instance-id"}})
    assert "CELERP_RELAY_STATE=active" in capsys.readouterr().out
    assert client.relay_status == "active"


# ── hello_ack ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hello_ack_writes_session_token(client):
    """hello_ack with session_token -> written to state."""
    await client._dispatch({
        "type": "hello_ack",
        "payload": {"session_token": "tok-abc-123"},
    })
    assert gw_state.get_session_token() == "tok-abc-123"


@pytest.mark.asyncio
async def test_hello_ack_without_session_token_clears_stale_token(client):
    """An ack without a session token is the relay's verdict: any stale
    stored token is cleared, never kept."""
    gw_state.set_session_token("stale-paid-token")
    await client._dispatch({
        "type": "hello_ack",
        "payload": {},
    })
    assert gw_state.get_session_token() == ""


@pytest.mark.asyncio
async def test_hello_ack_writes_subscription_tier(client):
    """hello_ack carries tier/status on every connection (unlike
    subscription_updated, which only fires on a Stripe billing event and so
    never reaches a plain free instance)."""
    await client._dispatch({
        "type": "hello_ack",
        "payload": {"tier": "free", "status": "trialing"},
    })
    assert gw_state.get_subscription_state() == ("free", "trialing")


@pytest.mark.asyncio
async def test_hello_ack_without_tier_leaves_subscription_state_unchanged(client):
    """hello_ack with no tier field -> subscription state left as-is."""
    gw_state.set_subscription_state("team", "active")
    await client._dispatch({
        "type": "hello_ack",
        "payload": {"session_token": "tok"},
    })
    assert gw_state.get_subscription_state() == ("team", "active")


# ── session.refresh ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_refresh_rotates_token(client):
    """session.refresh replaces the existing session token."""
    gw_state.set_session_token("old-token")
    await client._dispatch({
        "type": "session.refresh",
        "payload": {"session_token": "new-token-rotated"},
    })
    assert gw_state.get_session_token() == "new-token-rotated"


@pytest.mark.asyncio
async def test_session_refresh_empty_token_ignored(client):
    """session.refresh with empty token -> state not overwritten."""
    gw_state.set_session_token("existing-token")
    await client._dispatch({
        "type": "session.refresh",
        "payload": {"session_token": ""},
    })
    assert gw_state.get_session_token() == "existing-token"


# ── Other message types ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_error_message_logged(client, caplog):
    """A generic error message -> logged at ERROR level; an auth_failed frame is
    NOT generically logged (its first strikes are quiet retries)."""
    import logging
    with caplog.at_level(logging.ERROR, logger="celerp.gateway.client"):
        await client._dispatch({
            "type": "error",
            "payload": {"code": "quota_exceeded", "message": "Monthly quota reached"},
        })
        await client._dispatch(_auth_failed_frame())
    assert "quota_exceeded" in caplog.text
    assert "auth_failed" not in caplog.text
    assert len(caplog.records) == 1


@pytest.mark.asyncio
async def test_unknown_message_type_ignored(client):
    """Unknown message type -> no exception, no side effects."""
    await client._dispatch({"type": "unknown.future.type", "payload": {}})
    assert gw_state.get_session_token() == ""


@pytest.mark.asyncio
async def test_invoice_payment_dispatched_to_handler(client, monkeypatch):
    """A Cloud invoice.payment push -> routed to the backup-confirm handler."""
    import asyncio
    seen = {}

    async def _handler(payload):
        seen.update(payload)

    monkeypatch.setattr(client, "_handle_invoice_payment", _handler)
    await client._dispatch({"type": "invoice.payment", "payload": {
        "company_id": "c1", "entity_id": "doc:e1", "reference": "pi_9", "amount_minor": 500, "currency": "usd"}})
    await asyncio.sleep(0)  # let the spawned task run
    assert seen.get("reference") == "pi_9"


# ── ping ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_sends_pong(client, monkeypatch):
    """ping message -> pong sent back on the websocket."""
    sent = []

    async def fake_send(ws, msg):
        sent.append(msg)

    monkeypatch.setattr(client.__class__, "_send", staticmethod(fake_send))
    client._ws = object()  # non-None sentinel

    await client._dispatch({"type": "ping", "id": "ping-123", "payload": {}})
    assert len(sent) == 1
    assert sent[0]["type"] == "pong"
    assert sent[0]["id"] == "ping-123"


@pytest.mark.asyncio
async def test_ping_without_ws_no_crash(client):
    """ping when _ws is None -> no crash."""
    client._ws = None
    await client._dispatch({"type": "ping", "id": "ping-abc", "payload": {}})


# ── subscription_updated ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscription_updated_sets_state(client):
    """subscription_updated -> updates local subscription state."""
    await client._dispatch({
        "type": "subscription_updated",
        "payload": {"tier": "cloud", "status": "active", "feature_flags": {}},
    })
    tier, status = gw_state.get_subscription_state()
    assert tier == "cloud"
    assert status == "active"


# ── stop() ────────────────────────────────────────────────────────────────────

def test_relay_routes_share_paths_to_api_server(client):
    """Public share links are API-app routes; everything else browser-facing is
    UI. Routed to the UI server, /share/<token> 404s and every shared link is
    dead for external visitors."""
    assert client._local_port_for("/share/AbCdEfGhIjKl") == client._api_port
    assert client._local_port_for("/share/AbCdEfGhIjKl/bundle") == client._api_port
    assert client._local_port_for("/share") == client._api_port
    # UI stays UI - including lookalike prefixes
    assert client._local_port_for("/") == client._ui_port
    assert client._local_port_for("/docs/doc:123") == client._ui_port
    assert client._local_port_for("/api/barcode-preview") == client._ui_port
    assert client._local_port_for("/shared-thing") == client._ui_port


def test_stop_sets_running_false(client):
    """stop() sets _running=False."""
    client._running = True
    client.stop()
    assert client._running is False


# ── relay_status property ─────────────────────────────────────────────────────

def test_relay_status_property(client):
    """relay_status returns _relay_status."""
    client._relay_status = "active"
    assert client.relay_status == "active"


# ── is_serving ────────────────────────────────────────────────────────────────

def test_is_serving_true_for_matching_token_when_healthy(client):
    """A live client on the current token is serving it, so a reconnect that
    persisted the same token must treat it as the tunnel and no-op."""
    client._relay_status = "active"
    assert client.is_serving("test-gateway-token") is True


def test_is_serving_false_on_token_mismatch(client):
    """A token that rotated out from under this client is not the one it serves."""
    client._relay_status = "active"
    assert client.is_serving("a-different-token") is False


def test_is_serving_false_when_auth_rejected(client):
    """A relay-rejected client idles dead even though its token still matches, so
    callers must rebuild rather than trust the stale singleton."""
    client._auth_rejected = True
    assert client.is_serving("test-gateway-token") is False


def test_is_serving_false_in_error_state(client):
    """An error-latched client is not serving; the settings page must not read its
    stale error as the live tunnel state."""
    client._relay_status = "error"
    assert client.is_serving("test-gateway-token") is False


# ── _send ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_serializes_json():
    """_send sends JSON-serialized message to ws."""
    sent = []

    class FakeWs:
        async def send(self, data):
            sent.append(data)

    await GatewayClient._send(FakeWs(), {"type": "hello", "id": "1"})
    import json
    assert json.loads(sent[0]) == {"type": "hello", "id": "1"}


# ── set_client / get_client ───────────────────────────────────────────────────

def test_set_and_get_client():
    """set_client/get_client round-trip."""
    from celerp.gateway.client import get_client, set_client
    original = get_client()
    try:
        c = GatewayClient("tok", "inst", "wss://x")
        set_client(c)
        assert get_client() is c
        set_client(None)
        assert get_client() is None
    finally:
        set_client(original)


# ── run() ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_stops_when_not_running(client, monkeypatch):
    """run() exits when stop() is called before the loop starts."""
    connect_calls = []

    async def fake_connect():
        connect_calls.append(1)
        client._running = False

    monkeypatch.setattr(client, "_connect_and_serve", fake_connect)
    await client.run()
    assert connect_calls == [1]


@pytest.mark.asyncio
async def test_run_retries_on_exception(client, monkeypatch):
    """run() retries connection when _connect_and_serve raises."""
    calls = []
    import asyncio

    async def fake_sleep(delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def fake_connect():
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("refused")
        client._running = False

    monkeypatch.setattr(client, "_connect_and_serve", fake_connect)
    await client.run()
    assert len(calls) == 2


# ── tos_required error handling ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tos_required_error_sets_status(client):
    """error with code=tos_required -> relay_status='tos_required'."""
    await client._dispatch({
        "type": "error",
        "payload": {"code": "tos_required", "message": "Accept TOS", "required_version": "2026-04-12"},
    })
    assert client.relay_status == "tos_required"
    assert client.required_tos_version == "2026-04-12"


@pytest.mark.asyncio
async def test_tos_required_blocks_reconnect(client, monkeypatch):
    """run() does not reconnect when relay_status is tos_required."""
    connect_calls = []
    wait_calls = []

    async def fake_connect():
        connect_calls.append(1)
        # Simulate: first connect gets tos_required, then we stop
        client._relay_status = "tos_required"

    original_stop_event_wait = client._stop_event.wait

    async def counting_wait():
        wait_calls.append(1)
        if len(wait_calls) >= 3:
            client._running = False
        # Raise TimeoutError to simulate wait_for timeout expiring
        raise asyncio.TimeoutError

    import asyncio
    monkeypatch.setattr(client._stop_event, "wait", counting_wait)
    monkeypatch.setattr(client, "_connect_and_serve", fake_connect)
    await client.run()
    # Should have connected once, then looped in tos_required wait
    assert len(connect_calls) == 1
    assert len(wait_calls) >= 1


@pytest.mark.asyncio
async def test_connect_and_serve_uses_keepalive(client, monkeypatch):
    """The relay WS must open with keepalive (ping_interval/ping_timeout) so a
    silently-dead connection (e.g. the Mac sleeping) raises and the backoff loop
    reconnects. With ping_interval=None a half-open socket never raises, so the
    client never reconnects and the relay returns 502 until a manual restart.
    """
    import celerp.gateway.client as gwc
    captured = {}

    class _CM:
        async def __aenter__(self):
            raise ConnectionError("stop after capturing connect kwargs")
        async def __aexit__(self, *a):
            return False

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _CM()

    monkeypatch.setattr(gwc.websockets, "connect", fake_connect)
    with pytest.raises(ConnectionError):
        await client._connect_and_serve()

    assert captured.get("ping_interval") == 20, captured
    assert captured.get("ping_timeout") == 20, captured


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/labels/preview/barcode",
    "/api/items/bulk/split-preview",
    "/inventory/x",
])
async def test_proxy_routes_everything_to_ui_server(client, monkeypatch, path):
    """Relay-proxied requests (including /api/*) must go to the UI server (8080).

    The browser only ever talks to the UI server; the /api/* routes it calls are
    UI-server endpoints (HTMX fragments / previews), not the internal data API. The
    API server (8000) is an internal backend the UI calls server-side, so sending
    /api/* there over the relay 404s those UI routes.
    """
    client._ui_port = 8080
    client._api_port = 8000

    captured = {}

    class FakeResp:
        status_code = 200
        content = b"ok"
        headers = httpx.Headers()

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def request(self, method, url, headers=None, content=None):
            captured["url"] = url
            return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    async def fake_send(ws, msg):
        pass
    monkeypatch.setattr(client.__class__, "_send", staticmethod(fake_send))
    client._ws = object()

    await client._handle_proxy_request({
        "id": "r1", "method": "GET", "path": path,
        "query": "", "headers": {}, "body_b64": "",
    })
    assert "127.0.0.1:8080" in captured["url"], captured
    assert "127.0.0.1:8000" not in captured["url"], captured


@pytest.mark.asyncio
async def test_proxy_response_preserves_multiple_set_cookie(client, monkeypatch):
    """A proxied response must serialize headers as an ordered list of pairs so multiple Set-Cookie
    headers (login/refresh emit access_token + refresh_token) survive. A dict would collapse them
    into one comma-joined value the browser can't parse, dropping the refresh cookie over the relay."""
    sent = []

    class FakeResp:
        status_code = 200
        content = b"<html></html>"
        headers = httpx.Headers([
            ("content-type", "text/html"),
            ("content-length", "13"),
            ("set-cookie", "access_token=AAA; Path=/; HttpOnly"),
            ("set-cookie", "refresh_token=RRR; Path=/; HttpOnly"),
        ])

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def request(self, method, url, headers=None, content=None):
            return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    async def fake_send(ws, msg):
        sent.append(msg)
    monkeypatch.setattr(client.__class__, "_send", staticmethod(fake_send))
    client._ws = object()

    await client._handle_proxy_request({
        "id": "r1", "method": "GET", "path": "/x",
        "query": "", "headers": {}, "body_b64": "",
    })

    headers = sent[-1]["payload"]["headers"]
    assert isinstance(headers, list), f"headers must be a list of pairs, got {type(headers)}"
    cookies = [v for k, v in headers if k.lower() == "set-cookie"]
    assert len(cookies) == 2, f"both Set-Cookie headers must survive, got: {cookies}"
    assert any("access_token=AAA" in c for c in cookies)
    assert any("refresh_token=RRR" in c for c in cookies)
    # content-length is dropped so the relay recomputes it from the (decoded) body.
    assert not any(k.lower() == "content-length" for k, _ in headers)


# ── H1: proxy path denylist (client-side relay-trust hardening) ────────────────

@pytest.mark.asyncio
async def test_proxy_blocks_destructive_local_only_route(client, monkeypatch):
    """A remote-proxied request to a destructive local-only route (factory reset) is
    refused with 403 and nothing is forwarded to the UI server - a compromised broker
    (even replaying a captured session) cannot trigger a wipe."""
    import base64 as _b64
    sent = []

    async def fake_send(ws, msg):
        sent.append(msg)

    monkeypatch.setattr(client.__class__, "_send", staticmethod(fake_send))
    client._ws = object()  # non-None sentinel; the forward path is never reached

    for path in ("/settings/factory-reset", "/settings/factory-reset/confirm"):
        sent.clear()
        await client._handle_proxy_request(
            {"id": "r1", "method": "POST", "path": path, "query": "", "headers": {}, "body_b64": ""}
        )
        assert len(sent) == 1, path
        payload = sent[0]["payload"]
        assert payload["status"] == 403
        assert b"local machine" in _b64.b64decode(payload["body_b64"])


# ── reconnect loop console noise ──────────────────────────────────────────────
# The relay being down must not spam the console: one line when the connection
# is lost, silence during retries, one line when it comes back.

import asyncio as _asyncio
import logging as _logging

_real_wait_for = _asyncio.wait_for


def _fast_loop(client, monkeypatch):
    """Make run()'s backoff waits near-instant and disable the reaper."""
    async def fast_wait_for(awaitable, timeout=None):
        return await _real_wait_for(awaitable, timeout=0.005)

    monkeypatch.setattr(_asyncio, "wait_for", fast_wait_for)

    async def _noop():
        return None

    monkeypatch.setattr(client, "_reaper_loop", _noop)


@pytest.mark.asyncio
async def test_connection_loss_warns_once_not_per_retry(client, caplog, monkeypatch):
    """Repeated connect failures produce exactly one visible warning; retries are
    silent at INFO and above, with the exception detail kept at DEBUG."""
    attempts = 0

    async def failing_connect():
        nonlocal attempts
        attempts += 1
        if attempts >= 4:
            client.stop()
        raise ConnectionError("boom")

    monkeypatch.setattr(client, "_connect_and_serve", failing_connect)
    _fast_loop(client, monkeypatch)

    with caplog.at_level(_logging.DEBUG, logger="celerp.gateway.client"):
        await client.run()

    assert attempts >= 4
    visible = [r for r in caplog.records if r.levelno >= _logging.WARNING]
    assert len(visible) == 1
    # The visible line is calm user copy; the raw exception stays at DEBUG.
    assert "boom" not in visible[0].getMessage()
    debug = [r for r in caplog.records if r.levelno == _logging.DEBUG]
    assert any("boom" in r.getMessage() for r in debug)


@pytest.mark.asyncio
async def test_reconnect_announces_and_rearms_warning(client, caplog, monkeypatch):
    """A successful handshake after a loss announces the reconnect in plain
    language (no instance_id) and re-arms the single loss warning."""
    attempts = 0

    async def connect():
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            await client._dispatch({"type": "hello_ack", "payload": {
                "instance_id": "test-instance-id", "session_token": "tok"}})
        if attempts >= 5:
            client.stop()
        raise ConnectionError("boom")

    monkeypatch.setattr(client, "_connect_and_serve", connect)
    _fast_loop(client, monkeypatch)

    with caplog.at_level(_logging.INFO, logger="celerp.gateway.client"):
        await client.run()

    visible = [r for r in caplog.records if r.levelno >= _logging.WARNING]
    # One warning before the reconnect, one after it re-arms - never per retry.
    assert len(visible) == 2
    connected = [r for r in caplog.records
                 if r.levelno == _logging.INFO and "onnect" in r.getMessage()]
    assert connected
    assert all("test-instance-id" not in r.getMessage() for r in connected)


# ── stop()/close() reset transient announce state ─────────────────────────────

@pytest.mark.asyncio
async def test_stop_resets_loss_announced(client):
    """A stopped client must not carry the connection-loss announcement latch
    into a later start(); both shutdown paths clear it."""
    client._loss_announced = True
    client.stop()
    assert client._loss_announced is False

    client._loss_announced = True
    await client.close()
    assert client._loss_announced is False


@pytest.mark.asyncio
async def test_stop_resets_auth_rejection_state(client):
    """Disconnecting clears the auth-rejection latch and strike count on both
    shutdown paths, so a reconnect with fresh credentials starts clean."""
    client._auth_failures = 2
    client._auth_rejected = True
    client.stop()
    assert client._auth_failures == 0
    assert client._auth_rejected is False

    client._auth_failures = 2
    client._auth_rejected = True
    await client.close()
    assert client._auth_failures == 0
    assert client._auth_rejected is False


# ── auth_failed handling ──────────────────────────────────────────────────────

def _auth_failed_frame():
    return {"type": "error", "payload": {"code": "auth_failed", "message": "Invalid GATEWAY_TOKEN"}}


@pytest.mark.asyncio
async def test_auth_failed_becomes_terminal_after_bounded_retries(client, caplog):
    """Repeated auth_failed frames stop the client instead of retrying forever.

    The first rejections are quiet retries (a mid-deploy blip should not kill the
    connection); the third is terminal: status flips to error and exactly one
    ERROR line is emitted, not one per attempt."""
    import logging
    with caplog.at_level(logging.ERROR, logger="celerp.gateway.client"):
        await client._dispatch(_auth_failed_frame())
        await client._dispatch(_auth_failed_frame())
        assert client.relay_status != "error"
        await client._dispatch(_auth_failed_frame())
    assert client.relay_status == "error"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1


@pytest.mark.asyncio
async def test_auth_failed_halts_reconnect_loop(client, monkeypatch):
    """After the terminal rejection, run() idles without reconnecting."""
    import asyncio

    for _ in range(3):
        await client._dispatch(_auth_failed_frame())
    connect_calls = []

    async def fake_connect():
        connect_calls.append(1)

    monkeypatch.setattr(client, "_connect_and_serve", fake_connect)
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.1)
    client.stop()
    await asyncio.wait_for(task, timeout=5)
    assert connect_calls == []


@pytest.mark.asyncio
async def test_hello_ack_clears_auth_failure_strikes(client):
    """A successful handshake resets the rejection count: two old strikes plus
    two new ones must not trip the terminal state; only a fresh third does."""
    await client._dispatch(_auth_failed_frame())
    await client._dispatch(_auth_failed_frame())
    await client._dispatch({"type": "hello_ack", "payload": {}})
    await client._dispatch(_auth_failed_frame())
    await client._dispatch(_auth_failed_frame())
    assert client.relay_status != "error"
    await client._dispatch(_auth_failed_frame())
    assert client.relay_status == "error"
