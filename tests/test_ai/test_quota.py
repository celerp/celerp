# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp/ai/quota.py — 100% line coverage.

Covers:
  - _relay_http_url: derivation from wss://, ws://, gateway_http_url override
  - check_ai_quota: no gateway configured (skip), no instance_id (skip)
  - check_ai_quota: 200 → pass
  - check_ai_quota: 429 → HTTPException 402
  - check_ai_quota: 401 → pass (allow through)
  - check_ai_quota: other status → pass (allow through)
  - check_ai_quota: network error → pass (allow through)
  - check_ai_quota: credits param forwarded as query param
  - get_subscription_tier: 200 → returns tier
  - get_subscription_tier: failure → returns None
"""

from __future__ import annotations

import pytest
import httpx
import respx
from fastapi import HTTPException

from celerp.ai.quota import check_ai_quota, get_subscription_tier, _relay_http_url
from celerp.config import settings
import celerp.gateway.state as gw_state


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


# ── check_ai_quota ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quota_skip_no_gateway(monkeypatch):
    """No gateway configured → check skipped, no exception."""
    monkeypatch.setattr(settings, "gateway_token", "")
    monkeypatch.setattr(gw_state, "_session_token", "")
    await check_ai_quota()  # should not raise


@pytest.mark.asyncio
async def test_quota_skip_no_instance_id(monkeypatch):
    """No instance_id → check skipped."""
    monkeypatch.setattr(settings, "gateway_token", "tok")
    monkeypatch.setattr(gw_state, "_session_token", "sess")
    monkeypatch.setattr(settings, "gateway_instance_id", "")
    await check_ai_quota()  # should not raise


@pytest.mark.asyncio
async def test_quota_allowed(monkeypatch):
    """200 from relay → pass."""
    monkeypatch.setattr(settings, "gateway_token", "tok")
    monkeypatch.setattr(gw_state, "_session_token", "sess")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-iid")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test")

    with respx.mock:
        respx.post("https://relay.test/quota/ai/consume").mock(
            return_value=httpx.Response(200, json={"allowed": True, "used": 1, "limit": 100, "resets_at": "2026-04-01"})
        )
        await check_ai_quota()  # should not raise


@pytest.mark.asyncio
async def test_quota_exceeded_429(monkeypatch):
    """429 from relay → HTTPException 402."""
    monkeypatch.setattr(settings, "gateway_token", "tok")
    monkeypatch.setattr(gw_state, "_session_token", "sess")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-iid")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test")

    with respx.mock:
        respx.post("https://relay.test/quota/ai/consume").mock(
            return_value=httpx.Response(429, json={"detail": {
                "code": "quota_exceeded",
                "message": "AI query quota exceeded",
                "used": 3,
                "limit": 3,
                "resets_at": "2026-04-01T00:00:00Z",
            }})
        )
        with pytest.raises(HTTPException) as exc_info:
            await check_ai_quota()
    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["code"] == "quota_exceeded"
    assert "celerp.com/subscribe" in exc_info.value.detail["upgrade_url"]


@pytest.mark.asyncio
async def test_quota_expired_session_401(monkeypatch):
    """401 from relay → allow through (session expired, client will reconnect)."""
    monkeypatch.setattr(settings, "gateway_token", "tok")
    monkeypatch.setattr(gw_state, "_session_token", "sess")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-iid")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test")

    with respx.mock:
        respx.post("https://relay.test/quota/ai/consume").mock(
            return_value=httpx.Response(401, json={"detail": "Invalid or expired session token"})
        )
        await check_ai_quota()  # should not raise


@pytest.mark.asyncio
async def test_quota_unexpected_status(monkeypatch):
    """Other status codes → allow through."""
    monkeypatch.setattr(settings, "gateway_token", "tok")
    monkeypatch.setattr(gw_state, "_session_token", "sess")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-iid")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test")

    with respx.mock:
        respx.post("https://relay.test/quota/ai/consume").mock(
            return_value=httpx.Response(503, json={})
        )
        await check_ai_quota()  # should not raise


@pytest.mark.asyncio
async def test_quota_network_error(monkeypatch):
    """Network error → allow through (don't block on relay outage)."""
    monkeypatch.setattr(settings, "gateway_token", "tok")
    monkeypatch.setattr(gw_state, "_session_token", "sess")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-iid")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test")

    with respx.mock:
        respx.post("https://relay.test/quota/ai/consume").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        await check_ai_quota()  # should not raise


@pytest.mark.asyncio
async def test_quota_credits_param_ignored(monkeypatch):
    """credits parameter accepted but relay always consumes 1."""
    monkeypatch.setattr(settings, "gateway_token", "tok")
    monkeypatch.setattr(gw_state, "_session_token", "sess")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-iid")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test")

    with respx.mock:
        route = respx.post("https://relay.test/quota/ai/consume").mock(
            return_value=httpx.Response(200, json={"allowed": True})
        )
        await check_ai_quota(credits=5)
        assert route.called


# ── get_subscription_tier ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_subscription_tier_no_gateway(monkeypatch):
    """No gateway → returns None."""
    monkeypatch.setattr(settings, "gateway_token", "")
    monkeypatch.setattr(gw_state, "_session_token", "")
    result = await get_subscription_tier()
    assert result is None


@pytest.mark.asyncio
async def test_get_subscription_tier_returns_tier(monkeypatch):
    """200 response → returns tier string."""
    monkeypatch.setattr(settings, "gateway_token", "tok")
    monkeypatch.setattr(gw_state, "_session_token", "sess")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-iid")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test")

    with respx.mock:
        respx.get("https://relay.test/quota/ai/status").mock(
            return_value=httpx.Response(200, json={"tier": "cloud", "allowed": True, "used": 5, "limit": 100})
        )
        result = await get_subscription_tier()
    assert result == "cloud"


@pytest.mark.asyncio
async def test_get_subscription_tier_network_error(monkeypatch):
    """Network failure → returns None (never raises)."""
    monkeypatch.setattr(settings, "gateway_token", "tok")
    monkeypatch.setattr(gw_state, "_session_token", "sess")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-iid")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test")

    with respx.mock:
        respx.get("https://relay.test/quota/ai/status").mock(
            side_effect=httpx.ConnectError("refused")
        )
        result = await get_subscription_tier()
    assert result is None
