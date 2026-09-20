# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/quota.py — gateway quota status reads.

Covers:
  - _relay_http_url: derivation from wss://, ws://, gateway_http_url override
  - get_quota_status: no gateway (None), 200 (dict), bad status (None), network error (None)
"""

from __future__ import annotations

import httpx
import pytest
import respx
from unittest.mock import AsyncMock, patch

from celerp.ai.quota import _relay_http_url, get_quota_status
from celerp.config import settings
import celerp.gateway.state as gw_state


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "gateway_token", "tok")
    monkeypatch.setattr(gw_state, "_session_token", "sess")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-iid")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test")


# ── _relay_http_url ───────────────────────────────────────────────────────────

def test_relay_http_url_wss(monkeypatch):
    monkeypatch.setattr(settings, "gateway_http_url", "")
    monkeypatch.setattr(settings, "gateway_url", "wss://relay.celerp.com/ws/connect")
    assert _relay_http_url() == "https://relay.celerp.com"


def test_relay_http_url_ws(monkeypatch):
    monkeypatch.setattr(settings, "gateway_http_url", "")
    monkeypatch.setattr(settings, "gateway_url", "ws://localhost:8000/ws/connect")
    assert _relay_http_url() == "http://localhost:8000"


def test_relay_http_url_override(monkeypatch):
    monkeypatch.setattr(settings, "gateway_http_url", "https://custom-relay.example.com/")
    assert _relay_http_url() == "https://custom-relay.example.com"


# ── get_quota_status ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quota_status_no_gateway(monkeypatch):
    monkeypatch.setattr(settings, "gateway_token", "")
    monkeypatch.setattr(gw_state, "_session_token", "")
    assert await get_quota_status() is None


@pytest.mark.asyncio
async def test_quota_status_returns_dict(monkeypatch):
    _configure(monkeypatch)
    response = httpx.Response(
        200, json={"tier": "ai", "allowed": True, "used": 5, "limit": 200})
    with patch(
        "celerp.services.cloud_entitlement.authenticated_request",
        new=AsyncMock(return_value=response),
    ):
        status = await get_quota_status()
    assert status["tier"] == "ai"
    assert status["limit"] == 200


@pytest.mark.asyncio
@respx.mock
async def test_paid_quota_survives_optional_sync_failure_without_websocket(monkeypatch):
    """A proven paid quota read must stay usable when local WS resync fails.

    This follows the live no-WebSocket path used by /ai: exchange the durable
    instance API key for a relay bearer, read paid quota over REST, then fail the
    optional local entitlement resync. The authoritative paid result must win.
    """
    monkeypatch.setattr(settings, "gateway_token", "durable-api-key")
    monkeypatch.setattr(settings, "gateway_instance_id", "issue-332-iid")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test")
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    monkeypatch.setattr(gw_state, "_session_token", "")

    token_route = respx.post("https://relay.test/auth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "short-lived-jwt"}))
    quota_route = respx.get("https://relay.test/quota/ai/status").mock(
        return_value=httpx.Response(200, json={
            "tier": "ai", "allowed": True, "used": 0,
            "base_limit": 200, "remaining": 200,
        }))

    with patch(
        "celerp.services.cloud_entitlement.sync_existing_entitlement",
        new=AsyncMock(side_effect=RuntimeError("local activation persistence failed")),
    ) as sync:
        status = await get_quota_status()

    assert status == {
        "tier": "ai", "allowed": True, "used": 0,
        "base_limit": 200, "remaining": 200,
    }
    assert token_route.call_count == 1
    assert quota_route.call_count == 1
    assert quota_route.calls[0].request.headers["Authorization"] == (
        "Bearer short-lived-jwt")
    sync.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_quota_status_bad_status(monkeypatch):
    _configure(monkeypatch)
    with patch(
        "celerp.services.cloud_entitlement.authenticated_request",
        new=AsyncMock(return_value=httpx.Response(503, json={})),
    ):
        assert await get_quota_status() == {"unknown": True}


@pytest.mark.asyncio
async def test_quota_status_network_error(monkeypatch):
    _configure(monkeypatch)
    with patch(
        "celerp.services.cloud_entitlement.authenticated_request",
        new=AsyncMock(side_effect=httpx.ConnectError("refused")),
    ):
        assert await get_quota_status() == {"unknown": True}
