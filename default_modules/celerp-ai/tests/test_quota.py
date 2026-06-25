# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/quota.py — gateway quota status reads.

Covers:
  - _relay_http_url: derivation from wss://, ws://, gateway_http_url override
  - get_quota_status: no gateway (None), 200 (dict), bad status (None), network error (None)
  - get_subscription_tier: 200 (tier), no gateway / failure (None)
"""

from __future__ import annotations

import httpx
import pytest
import respx

from celerp.ai.quota import _relay_http_url, get_quota_status, get_subscription_tier
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
    with respx.mock:
        respx.get("https://relay.test/quota/ai/status").mock(
            return_value=httpx.Response(200, json={"tier": "ai", "allowed": True, "used": 5, "limit": 200})
        )
        status = await get_quota_status()
    assert status["tier"] == "ai"
    assert status["limit"] == 200


@pytest.mark.asyncio
async def test_quota_status_bad_status(monkeypatch):
    _configure(monkeypatch)
    with respx.mock:
        respx.get("https://relay.test/quota/ai/status").mock(return_value=httpx.Response(503, json={}))
        assert await get_quota_status() is None


@pytest.mark.asyncio
async def test_quota_status_network_error(monkeypatch):
    _configure(monkeypatch)
    with respx.mock:
        respx.get("https://relay.test/quota/ai/status").mock(side_effect=httpx.ConnectError("refused"))
        assert await get_quota_status() is None


# ── get_subscription_tier ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_subscription_tier_no_gateway(monkeypatch):
    monkeypatch.setattr(settings, "gateway_token", "")
    monkeypatch.setattr(gw_state, "_session_token", "")
    assert await get_subscription_tier() is None


@pytest.mark.asyncio
async def test_get_subscription_tier_returns_tier(monkeypatch):
    _configure(monkeypatch)
    with respx.mock:
        respx.get("https://relay.test/quota/ai/status").mock(
            return_value=httpx.Response(200, json={"tier": "cloud", "allowed": True, "used": 5, "limit": 100})
        )
        assert await get_subscription_tier() == "cloud"


@pytest.mark.asyncio
async def test_get_subscription_tier_network_error(monkeypatch):
    _configure(monkeypatch)
    with respx.mock:
        respx.get("https://relay.test/quota/ai/status").mock(side_effect=httpx.ConnectError("refused"))
        assert await get_subscription_tier() is None
