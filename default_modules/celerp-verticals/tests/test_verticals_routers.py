# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Tests for celerp-verticals: /companies/verticals/* and /companies/me/apply-* endpoints."""

from __future__ import annotations

import uuid

import pytest


async def _reg(client) -> str:
    email = f"vert-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post("/auth/register", json={
        "company_name": "VertCo", "email": email, "name": "Admin", "password": "validpass1"
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
    # Schemas are keyed by the stable category slug (matches an item's `category`
    # value and the demo rows), not the display_name.
    assert "diamond" in schemas
    assert "ruby" in schemas
    assert "sapphire" in schemas
    assert "emerald" in schemas
    assert "jewelry" in schemas


@pytest.mark.asyncio
async def test_apply_preset_idempotent(client):
    """Applying the same preset twice adds nothing the first application did not."""
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
    diamond_keys = [f["key"] for f in schemas["diamond"]]
    assert len(diamond_keys) == len(set(diamond_keys))


@pytest.mark.asyncio
async def test_apply_preset_keeps_existing_company_settings(client):
    """A preset only adds company settings the company does not have yet."""
    tok = await _reg(client)
    r = await client.patch("/companies/me", json={"settings": {"inventory_method": "lifo"}}, headers=_h(tok))
    assert r.status_code == 200, r.text
    r = await client.post("/companies/me/apply-preset", params={"vertical": "agricultural"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["company_settings"] == {}
    settings = (await client.get("/companies/me", headers=_h(tok))).json()["settings"]
    assert settings["inventory_method"] == "lifo"


@pytest.mark.asyncio
async def test_apply_preset_adds_missing_company_settings(client):
    tok = await _reg(client)
    r = await client.post("/companies/me/apply-preset", params={"vertical": "agricultural"}, headers=_h(tok))
    assert r.json()["company_settings"] == {"inventory_method": "fefo"}
    settings = (await client.get("/companies/me", headers=_h(tok))).json()["settings"]
    assert settings["inventory_method"] == "fefo"


@pytest.mark.asyncio
async def test_apply_preset_not_found(client):
    """POST /companies/me/apply-preset?vertical=nonexistent returns 404."""
    tok = await _reg(client)
    r = await client.post("/companies/me/apply-preset", params={"vertical": "nonexistent"}, headers=_h(tok))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_apply_preset_diamond_fields(client):
    """Diamond category from gemstones preset has core grading fields.

    Note: carat weight is tracked via sell_by=carat + quantity, not a custom
    attribute field. The custom schema covers gem quality attributes only.
    """
    tok = await _reg(client)
    await client.post("/companies/me/apply-preset", params={"vertical": "gemstones"}, headers=_h(tok))
    r = await client.get("/companies/me/category-schemas", headers=_h(tok))
    schemas = r.json()
    diamond_keys = {f["key"] for f in schemas["diamond"]}
    # Core grading fields must be present
    assert "grade" in diamond_keys
    assert "cut" in diamond_keys
    assert "clarity" in diamond_keys


@pytest.mark.asyncio
async def test_apply_preset_jewelry_metal_options(client):
    """Jewelry category has metal field with Gold 18K option."""
    tok = await _reg(client)
    await client.post("/companies/me/apply-preset", params={"vertical": "gemstones"}, headers=_h(tok))
    r = await client.get("/companies/me/category-schemas", headers=_h(tok))
    schemas = r.json()
    jewelry_fields = {f["key"]: f for f in schemas["jewelry"]}
    assert "metal" in jewelry_fields
    assert "Gold 18K" in jewelry_fields["metal"]["options"]
