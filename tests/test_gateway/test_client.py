# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for GatewayClient session token handling and message dispatch.

Tests the session token write/refresh logic without a live WebSocket server
by calling _dispatch() directly on the client instance.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

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
    gw_state.set_session_token("")
    yield
    gw_state.set_session_token(original)


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
async def test_hello_ack_without_session_token_leaves_state_empty(client):
    """hello_ack with no session_token field -> state unchanged."""
    await client._dispatch({
        "type": "hello_ack",
        "payload": {},
    })
    assert gw_state.get_session_token() == ""


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
    """error message -> logged at ERROR level, no exception raised."""
    import logging
    with caplog.at_level(logging.ERROR, logger="celerp.gateway.client"):
        await client._dispatch({
            "type": "error",
            "payload": {"code": "AUTH_FAILED", "message": "Invalid token"},
        })
    assert "AUTH_FAILED" in caplog.text


@pytest.mark.asyncio
async def test_unknown_message_type_ignored(client):
    """Unknown message type -> no exception, no side effects."""
    await client._dispatch({"type": "unknown.future.type", "payload": {}})
    assert gw_state.get_session_token() == ""


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
    sleep_calls = []

    async def fake_connect():
        connect_calls.append(1)
        # Simulate: first connect gets tos_required, then we stop
        client._relay_status = "tos_required"

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        if len(sleep_calls) >= 3:
            client._running = False

    import asyncio
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(client, "_connect_and_serve", fake_connect)
    await client.run()
    # Should have connected once, then slept in tos_required loop
    assert len(connect_calls) == 1
    assert len(sleep_calls) >= 1
