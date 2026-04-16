# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import pytest


async def _headers(client) -> dict:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Acme", "email": "admin@acme.com", "name": "Admin", "password": "pw"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_companies_me_patch_and_locations(client):
    headers = await _headers(client)

    r = await client.get("/companies/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["name"] == "Acme"

    r = await client.patch("/companies/me", json={"settings": {"a": 1}}, headers=headers)
    assert r.status_code == 200

    r = await client.post(
        "/companies/me/locations",
        json={"name": "Main", "type": "warehouse", "address": {"x": 1}, "is_default": True},
        headers=headers,
    )
    assert r.status_code == 200

    r = await client.get("/companies/me/locations", headers=headers)
    assert r.status_code == 200
    # Registration seeds "Head Office" + we added "Main" = 2 locations
    assert len(r.json()["items"]) >= 1
    names = [loc["name"] for loc in r.json()["items"]]
    assert "Main" in names


@pytest.mark.asyncio
async def test_companies_requires_auth(client):
    r = await client.get("/companies/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_patch_user_last_owner_guard(client):
    """Cannot demote the last active owner."""
    headers = await _headers(client)

    # Get own user id
    me = await client.get("/companies/me/users", headers=headers)
    assert me.status_code == 200
    users = me.json()["items"]
    owner = next(u for u in users if u["role"] == "owner")
    owner_id = owner["id"]

    # Demoting the only owner must fail
    r = await client.patch(f"/companies/me/users/{owner_id}", json={"role": "admin"}, headers=headers)
    assert r.status_code == 400
    assert "last owner" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_user_demote_admin_allowed(client):
    """Can demote an admin when they are not the last owner."""
    headers = await _headers(client)

    # Invite a second user and make them admin
    import uuid as _uuid
    email2 = f"admin2-{_uuid.uuid4().hex[:8]}@test.com"
    r = await client.post("/companies/me/users", json={"email": email2, "name": "Admin2", "password": "pw", "role": "admin"}, headers=headers)
    assert r.status_code == 200
    user2_id = r.json()["id"]

    # Now demoting the second admin should succeed
    r = await client.patch(f"/companies/me/users/{user2_id}", json={"role": "operator"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_demo_reseed_vertical(client):
    """POST /companies/me/demo/reseed seeds vertical-aware items."""
    headers = await _headers(client)

    # Set vertical to gemstones
    await client.patch("/companies/me", json={"settings": {"vertical": "gemstones"}}, headers=headers)

    r = await client.post("/companies/me/demo/reseed", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["vertical"] == "gemstones"

    # Items should now include gemstone demo items
    items = (await client.get("/items", headers=headers)).json()["items"]
    skus = {i["sku"] for i in items}
    assert "DEMO-DIA-001" in skus
    assert "DEMO-RUB-001" in skus
    assert "DEMO-JWL-001" in skus


@pytest.mark.asyncio
async def test_demo_reseed_no_vertical(client):
    """POST /companies/me/demo/reseed with no vertical seeds generic items."""
    headers = await _headers(client)

    r = await client.post("/companies/me/demo/reseed", headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_demo_reseed_full_wizard_flow(client):
    """Simulate full wizard flow: register (seeds generic), set vertical, reseed.
    DEMO-001 must be gone and gemstone items must appear."""
    headers = await _headers(client)

    # Registration seeds DEMO-001
    items = (await client.get("/items", headers=headers)).json()["items"]
    skus = {i["sku"] for i in items}
    assert "DEMO-001" in skus, "Registration should seed DEMO-001"

    # Wizard step: save vertical to company settings
    company = (await client.get("/companies/me", headers=headers)).json()
    settings = dict(company.get("settings") or {})
    settings["vertical"] = "gemstones"
    await client.patch("/companies/me", json={"name": company.get("name", "Acme"), "settings": settings}, headers=headers)

    # Wizard step: reseed demo items
    r = await client.post("/companies/me/demo/reseed", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["vertical"] == "gemstones"
    assert body["wiped"] >= 1  # DEMO-001 was wiped

    # DEMO-001 must be gone, gemstone items must appear
    items = (await client.get("/items", headers=headers)).json()["items"]
    skus = {i["sku"] for i in items}
    assert "DEMO-001" not in skus, "DEMO-001 should be wiped after reseed"
    assert "DEMO-DIA-001" in skus, "Diamond demo item should appear after gemstones reseed"
    assert "DEMO-JWL-001" in skus, "Jewelry demo item should appear after gemstones reseed"
