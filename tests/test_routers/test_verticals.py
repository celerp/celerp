# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for celerp-verticals: /companies/verticals/* and /companies/me/apply-* endpoints."""

from __future__ import annotations

import uuid

import pytest


async def _reg(client) -> str:
    email = f"vert-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post("/auth/register", json={
        "company_name": "VertCo", "email": email, "name": "Admin", "password": "pw"
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.asyncio
async def test_apply_preset_gemstones(client):
    """POST /companies/me/apply-preset?vertical=gemstones applies 9 categories."""
    tok = await _reg(client)
    r = await client.post("/companies/me/apply-preset", params={"vertical": "gemstones"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == "gemstones"
    assert body["categories"] == 9


@pytest.mark.asyncio
async def test_apply_preset_schemas_stored(client):
    """After applying gemstones preset, category schemas are persisted."""
    tok = await _reg(client)
    await client.post("/companies/me/apply-preset", params={"vertical": "gemstones"}, headers=_h(tok))
    r = await client.get("/companies/me/category-schemas", headers=_h(tok))
    assert r.status_code == 200, r.text
    schemas = r.json()
    assert "Diamond" in schemas
    assert "Ruby" in schemas
    assert "Sapphire" in schemas
    assert "Emerald" in schemas
    assert "Jewelry" in schemas


@pytest.mark.asyncio
async def test_apply_preset_idempotent(client):
    """Applying the same preset twice overwrites without duplicating fields."""
    tok = await _reg(client)
    r1 = await client.post("/companies/me/apply-preset", params={"vertical": "gemstones"}, headers=_h(tok))
    r2 = await client.post("/companies/me/apply-preset", params={"vertical": "gemstones"}, headers=_h(tok))
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Category count must be stable across applications
    assert r1.json()["categories"] == r2.json()["categories"]

    r = await client.get("/companies/me/category-schemas", headers=_h(tok))
    schemas = r.json()
    # Diamond fields should not be duplicated
    diamond_keys = [f["key"] for f in schemas["Diamond"]]
    assert len(diamond_keys) == len(set(diamond_keys))


@pytest.mark.asyncio
async def test_apply_preset_not_found(client):
    """POST /companies/me/apply-preset?vertical=nonexistent returns 404."""
    tok = await _reg(client)
    r = await client.post("/companies/me/apply-preset", params={"vertical": "nonexistent"}, headers=_h(tok))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_apply_preset_diamond_fields(client):
    """Diamond category from gemstones preset has core grading fields."""
    tok = await _reg(client)
    await client.post("/companies/me/apply-preset", params={"vertical": "gemstones"}, headers=_h(tok))
    r = await client.get("/companies/me/category-schemas", headers=_h(tok))
    schemas = r.json()
    diamond_keys = {f["key"] for f in schemas["Diamond"]}
    # Core grading fields must be present
    assert "grade" in diamond_keys
    assert "carat" in diamond_keys
    assert "cut" in diamond_keys
    assert "clarity" in diamond_keys


@pytest.mark.asyncio
async def test_apply_preset_jewelry_metal_options(client):
    """Jewelry category has metal field with Gold 18K option."""
    tok = await _reg(client)
    await client.post("/companies/me/apply-preset", params={"vertical": "gemstones"}, headers=_h(tok))
    r = await client.get("/companies/me/category-schemas", headers=_h(tok))
    schemas = r.json()
    jewelry_fields = {f["key"]: f for f in schemas["Jewelry"]}
    assert "metal" in jewelry_fields
    assert "Gold 18K" in jewelry_fields["metal"]["options"]
