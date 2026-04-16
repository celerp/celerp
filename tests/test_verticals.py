# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for celerp-verticals API endpoints."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient) -> str:
    """Register a fresh company and return the admin access token."""
    r = await client.post(
        "/auth/register",
        json={
            "company_name": "VerticalsCo",
            "name": "Admin User",
            "email": "admin@verticals.test",
            "password": "testpass123",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _register_manager(client: AsyncClient, admin_token: str) -> str:
    """Create a manager user in the same company and return its token."""
    r = await client.post(
        "/companies/me/users",
        json={"name": "Mgr", "email": "mgr@verticals.test", "password": "testpass123", "role": "manager"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    r2 = await client.post(
        "/auth/login",
        json={"email": "mgr@verticals.test", "password": "testpass123"},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# GET /companies/verticals/categories
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_categories_requires_auth(client: AsyncClient):
    r = await client.get("/companies/verticals/categories")
    assert r.status_code == 401


@pytest.mark.anyio
async def test_list_categories_returns_list(client: AsyncClient):
    token = await _register(client)
    r = await client.get("/companies/verticals/categories", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    for item in data:
        assert "name" in item
        assert "display_name" in item
        assert "vertical_tags" in item
        assert isinstance(item["vertical_tags"], list)


@pytest.mark.anyio
async def test_list_categories_not_empty(client: AsyncClient):
    token = await _register(client)
    r = await client.get("/companies/verticals/categories", headers=_auth(token))
    assert r.status_code == 200
    assert len(r.json()) > 0


# ---------------------------------------------------------------------------
# GET /companies/verticals/categories/{name}
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_category_requires_auth(client: AsyncClient):
    r = await client.get("/companies/verticals/categories/diamond")
    assert r.status_code == 401


@pytest.mark.anyio
async def test_get_category_diamond(client: AsyncClient):
    token = await _register(client)
    r = await client.get("/companies/verticals/categories/diamond", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "diamond"
    assert "fields" in data
    assert isinstance(data["fields"], list)
    assert len(data["fields"]) > 0


@pytest.mark.anyio
async def test_get_category_not_found(client: AsyncClient):
    token = await _register(client)
    r = await client.get("/companies/verticals/categories/nonexistent_xyz_abc", headers=_auth(token))
    assert r.status_code == 404


@pytest.mark.anyio
async def test_get_category_fields_have_required_keys(client: AsyncClient):
    token = await _register(client)
    r = await client.get("/companies/verticals/categories/diamond", headers=_auth(token))
    assert r.status_code == 200
    fields = r.json()["fields"]
    for f in fields:
        assert "key" in f
        assert "label" in f
        assert "type" in f


# ---------------------------------------------------------------------------
# GET /companies/verticals/presets
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_presets_requires_auth(client: AsyncClient):
    r = await client.get("/companies/verticals/presets")
    assert r.status_code == 401


@pytest.mark.anyio
async def test_list_presets_returns_list(client: AsyncClient):
    token = await _register(client)
    r = await client.get("/companies/verticals/presets", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    for p in data:
        assert "name" in p
        assert "display_name" in p
        assert "categories" in p
        assert isinstance(p["categories"], list)


@pytest.mark.anyio
async def test_list_presets_includes_gemstones(client: AsyncClient):
    token = await _register(client)
    r = await client.get("/companies/verticals/presets", headers=_auth(token))
    names = [p["name"] for p in r.json()]
    assert "gemstones" in names


@pytest.mark.anyio
async def test_list_presets_includes_all_verticals(client: AsyncClient):
    token = await _register(client)
    r = await client.get("/companies/verticals/presets", headers=_auth(token))
    names = set(p["name"] for p in r.json())
    expected = {
        "gemstones", "watches", "coins_precious_metals", "artwork",
        "fashion", "electronics", "hardware", "books_media",
        "automotive", "cosmetics", "furniture",
        "agricultural", "food_beverage", "wine_spirits",
    }
    assert expected.issubset(names)


# ---------------------------------------------------------------------------
# POST /companies/me/apply-preset
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_apply_preset_requires_auth(client: AsyncClient):
    r = await client.post("/companies/me/apply-preset", params={"vertical": "gemstones"})
    assert r.status_code == 401


@pytest.mark.anyio
async def test_apply_preset_gemstones(client: AsyncClient):
    token = await _register(client)
    r = await client.post(
        "/companies/me/apply-preset",
        params={"vertical": "gemstones"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["applied"] == "gemstones"
    assert data["categories"] > 0


@pytest.mark.anyio
async def test_apply_preset_seeds_category_schemas(client: AsyncClient):
    token = await _register(client)
    r = await client.post(
        "/companies/me/apply-preset",
        params={"vertical": "gemstones"},
        headers=_auth(token),
    )
    assert r.status_code == 200

    r2 = await client.get("/companies/me/category-schemas", headers=_auth(token))
    assert r2.status_code == 200
    schemas = r2.json()
    assert isinstance(schemas, dict)
    assert len(schemas) > 0


@pytest.mark.anyio
async def test_apply_preset_not_found(client: AsyncClient):
    token = await _register(client)
    r = await client.post(
        "/companies/me/apply-preset",
        params={"vertical": "nonexistent_xyz"},
        headers=_auth(token),
    )
    assert r.status_code == 404


@pytest.mark.anyio
async def test_apply_preset_fefo_company_settings(client: AsyncClient):
    """F&B preset should set inventory_method=fefo in company.settings."""
    token = await _register(client)
    r = await client.post(
        "/companies/me/apply-preset",
        params={"vertical": "food_beverage"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("company_settings", {}).get("inventory_method") == "fefo"


@pytest.mark.anyio
async def test_apply_preset_agricultural_fefo(client: AsyncClient):
    token = await _register(client)
    r = await client.post(
        "/companies/me/apply-preset",
        params={"vertical": "agricultural"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json().get("company_settings", {}).get("inventory_method") == "fefo"


@pytest.mark.anyio
async def test_apply_preset_idempotent(client: AsyncClient):
    token = await _register(client)
    r1 = await client.post(
        "/companies/me/apply-preset",
        params={"vertical": "fashion"},
        headers=_auth(token),
    )
    r2 = await client.post(
        "/companies/me/apply-preset",
        params={"vertical": "fashion"},
        headers=_auth(token),
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["categories"] == r2.json()["categories"]


# ---------------------------------------------------------------------------
# POST /companies/me/apply-category
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_apply_category_requires_auth(client: AsyncClient):
    r = await client.post("/companies/me/apply-category", params={"name": "diamond"})
    assert r.status_code == 401


@pytest.mark.anyio
async def test_apply_category_diamond(client: AsyncClient):
    token = await _register(client)
    r = await client.post(
        "/companies/me/apply-category",
        params={"name": "diamond"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["applied"] == "diamond"
    assert "display_name" in data


@pytest.mark.anyio
async def test_apply_category_seeds_schema(client: AsyncClient):
    token = await _register(client)
    r = await client.post(
        "/companies/me/apply-category",
        params={"name": "ruby"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    display = r.json()["display_name"]

    r2 = await client.get("/companies/me/category-schemas", headers=_auth(token))
    schemas = r2.json()
    assert display in schemas
    assert isinstance(schemas[display], list)
    assert len(schemas[display]) > 0


@pytest.mark.anyio
async def test_apply_category_not_found(client: AsyncClient):
    token = await _register(client)
    r = await client.post(
        "/companies/me/apply-category",
        params={"name": "nonexistent_xyz"},
        headers=_auth(token),
    )
    assert r.status_code == 404


@pytest.mark.anyio
async def test_apply_category_idempotent(client: AsyncClient):
    token = await _register(client)
    r1 = await client.post(
        "/companies/me/apply-category",
        params={"name": "jewelry"},
        headers=_auth(token),
    )
    r2 = await client.post(
        "/companies/me/apply-category",
        params={"name": "jewelry"},
        headers=_auth(token),
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["applied"] == r2.json()["applied"]


@pytest.mark.anyio
async def test_apply_category_multiple_verticals(client: AsyncClient):
    """Apply one category from each major vertical group."""
    token = await _register(client)
    categories = ["diamond", "watch", "gold_bullion", "painting", "laptop", "book", "wine"]
    for cat in categories:
        r = await client.post(
            "/companies/me/apply-category",
            params={"name": cat},
            headers=_auth(token),
        )
        assert r.status_code == 200, f"Failed for category '{cat}': {r.text}"


# ---------------------------------------------------------------------------
# Non-admin blocked
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_apply_preset_requires_admin(client: AsyncClient):
    admin_token = await _register(client)
    mgr_token = await _register_manager(client, admin_token)
    r = await client.post(
        "/companies/me/apply-preset",
        params={"vertical": "gemstones"},
        headers=_auth(mgr_token),
    )
    assert r.status_code == 403


@pytest.mark.anyio
async def test_apply_category_requires_admin(client: AsyncClient):
    admin_token = await _register(client)
    mgr_token = await _register_manager(client, admin_token)
    r = await client.post(
        "/companies/me/apply-category",
        params={"name": "diamond"},
        headers=_auth(mgr_token),
    )
    assert r.status_code == 403
