# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for the /stars/badge endpoints (claim/get/delete + verify-url).

The relay credential is decoded unverified (local-display trust model), so any
HS256 key signs a valid test credential.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt


async def _register(client, suffix: str = "") -> str:
    addr = f"badge-{suffix or uuid.uuid4().hex[:8]}@test.local"
    r = await client.post(
        "/auth/register",
        json={"company_name": "BadgeCo", "email": addr, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _cred(uid: int = 42, login: str = "alice", tier: str = "founding", ordinal: int = 8) -> str:
    return jwt.encode(
        {"typ": "supporter_cred", "uid": uid, "login": login, "tier": tier, "ordinal": ordinal},
        "any-key", algorithm="HS256",
    )


@pytest.mark.asyncio
async def test_claim_then_get(client):
    token = await _register(client, "claim")
    r = await client.post("/stars/badge", headers=_h(token), json={"cred": _cred()})
    assert r.status_code == 200
    badge = r.json()["badge"]
    assert badge["tier"] == "founding" and badge["ordinal"] == 8
    assert badge["label"] == "Founding Supporter #8"
    assert badge["github_login"] == "alice"

    g = await client.get("/stars/badge", headers=_h(token))
    assert g.json()["badge"]["ordinal"] == 8


@pytest.mark.asyncio
async def test_get_badge_null_when_none(client):
    token = await _register(client, "none")
    assert (await client.get("/stars/badge", headers=_h(token))).json()["badge"] is None


@pytest.mark.asyncio
async def test_get_badge_includes_wall_url(client):
    """The badge GET carries the public founders-wall URL so the in-app card can
    link to it once claimed."""
    token = await _register(client, "wall")
    data = (await client.get("/stars/badge", headers=_h(token))).json()
    assert data["wall_url"].endswith("/github/founders")


@pytest.mark.asyncio
async def test_claim_invalid_cred(client):
    token = await _register(client, "bad")
    r = await client.post("/stars/badge", headers=_h(token), json={"cred": "garbage"})
    assert "error" in r.json()
    r2 = await client.post("/stars/badge", headers=_h(token), json={})
    assert "error" in r2.json()


@pytest.mark.asyncio
async def test_claim_wrong_token_type_rejected(client):
    token = await _register(client, "wrongtyp")
    not_a_cred = jwt.encode({"typ": "stars_state", "login": "x", "ordinal": 1}, "k", algorithm="HS256")
    assert "error" in (await client.post("/stars/badge", headers=_h(token), json={"cred": not_a_cred})).json()


@pytest.mark.asyncio
async def test_delete_badge_opts_out(client):
    token = await _register(client, "del")
    await client.post("/stars/badge", headers=_h(token), json={"cred": _cred()})
    # Mock the relay opt-out call so the delete stays offline.
    with patch("celerp.services.supporter_badge.httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.request = AsyncMock(return_value=MagicMock())
        d = await client.delete("/stars/badge", headers=_h(token))
    assert d.status_code == 200 and d.json()["removed"] is True
    assert (await client.get("/stars/badge", headers=_h(token))).json()["badge"] is None


@pytest.mark.asyncio
async def test_verify_url_includes_instance_and_return(client):
    token = await _register(client, "vurl")
    r = await client.get("/stars/badge/verify-url", headers=_h(token),
                         params={"return_url": "http://localhost:8080/stars/claim"})
    assert r.status_code == 200
    url = r.json()["url"]
    assert "/github/verify/start?" in url
    assert "instance_id=" in url
    assert "return_url=http" in url and "stars" in url


@pytest.mark.asyncio
async def test_verify_url_carries_opt_in(client):
    token = await _register(client, "optin")
    base = {"return_url": "http://localhost:8080/stars/claim"}
    # Default: public listing off (opt_in=0 carried through to the relay verify flow).
    d = (await client.get("/stars/badge/verify-url", headers=_h(token), params=base)).json()["url"]
    assert "opt_in=0" in d
    # Explicit opt-in passes through as opt_in=1.
    o = (await client.get("/stars/badge/verify-url", headers=_h(token),
                          params={**base, "opt_in": 1})).json()["url"]
    assert "opt_in=1" in o


@pytest.mark.asyncio
async def test_badge_requires_auth(client):
    assert (await client.get("/stars/badge")).status_code == 401
