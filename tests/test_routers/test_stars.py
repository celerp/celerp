# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for the /stars CTA endpoints. The relay client is mocked."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from celerp.config import settings


async def _register(client, suffix: str = "") -> str:
    addr = f"stars-{suffix or uuid.uuid4().hex[:8]}@test.example"
    r = await client.post(
        "/auth/register",
        json={"company_name": "StarCo", "email": addr, "name": "Admin", "password": "pwvalid1"},
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
    assert r.json() == {"mode": "neutral"}


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


@pytest.mark.asyncio
async def test_cta_asks_the_relay_in_the_ui_language(owner_ui, monkeypatch):
    """The star card asks for its copy in the language the page is shown in: the UI
    proxy forwards it and the API passes it on to the relay request."""
    from httpx import ASGITransport, AsyncClient

    from celerp.main import app

    def bridged(tok, timeout=10.0, follow_redirects=False):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {tok}"})

    monkeypatch.setattr("ui.api_client._local_client", bridged)
    asked = AsyncMock(return_value={"mode": "founding", "url": "https://celerp.com/github"})
    owner_ui.cookies.set("celerp_lang", "de")
    with patch("celerp.routers.stars.get_star_cta", new=asked):
        r = await owner_ui.get("/stars/cta", params={"medium": "dashboard"})
    assert r.status_code == 200, r.text
    asked.assert_awaited_once_with("dashboard", "de")


@pytest.mark.asyncio
async def test_cta_language_outside_the_catalogs_asks_in_english(client):
    token = await _register(client, "lang")
    asked = AsyncMock(return_value=None)
    with patch("celerp.routers.stars.get_star_cta", new=asked):
        r = await client.get("/stars/cta", headers=_h(token), params={"medium": "footer", "lang": "xx-evil"})
    assert r.status_code == 200
    asked.assert_awaited_once_with("footer", "en")
