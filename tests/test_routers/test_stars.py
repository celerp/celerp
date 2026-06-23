# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for the /stars CTA endpoints. The relay client is mocked."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from celerp.config import settings


async def _register(client, suffix: str = "") -> str:
    addr = f"stars-{suffix or uuid.uuid4().hex[:8]}@test.local"
    r = await client.post(
        "/auth/register",
        json={"company_name": "StarCo", "email": addr, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_cta_returns_relay_payload(client):
    token = await _register(client, "cta")
    cta = {"mode": "founding", "cta_label": "Star on GitHub",
           "url": "https://celerp.com/github?utm_source=app&utm_medium=footer"}
    with patch("celerp.routers.stars.get_star_cta", new=AsyncMock(return_value=cta)):
        r = await client.get("/stars/cta", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "founding"
    assert data["dismissed"] is False


@pytest.mark.asyncio
async def test_cta_neutral_when_relay_unavailable(client):
    token = await _register(client, "neutral")
    with patch("celerp.routers.stars.get_star_cta", new=AsyncMock(return_value=None)):
        r = await client.get("/stars/cta", headers=_h(token), params={"medium": "footer"})
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "neutral"
    assert "/github" in data["url"]


@pytest.mark.asyncio
async def test_cta_empty_when_disabled(client, monkeypatch):
    token = await _register(client, "disabled")
    monkeypatch.setattr(settings, "star_cta_enabled", False)
    r = await client.get("/stars/cta", headers=_h(token))
    assert r.status_code == 200
    assert r.json() == {}


@pytest.mark.asyncio
async def test_dismiss_then_cta_shows_dismissed(client):
    token = await _register(client, "dismiss")
    rd = await client.post("/stars/dismiss", headers=_h(token))
    assert rd.status_code == 200
    assert rd.json()["dismissed"] is True
    with patch("celerp.routers.stars.get_star_cta", new=AsyncMock(return_value=None)):
        r = await client.get("/stars/cta", headers=_h(token))
    assert r.json()["dismissed"] is True


@pytest.mark.asyncio
async def test_cta_requires_auth(client):
    r = await client.get("/stars/cta")
    assert r.status_code == 401
