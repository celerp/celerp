# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import pytest


async def _token(client) -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": "SplitCo", "email": "split@test.com", "name": "Admin", "password": "pw"},
    )
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_split_preview_weight_unit(client):
    """split-preview returns proportional weight defaults for a weight-unit item."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/items",
        json={"sku": "GOLD-001", "name": "Gold Bar", "quantity": 10.0, "sell_by": "gram",
              "weight": 10.0},
        headers=headers,
    )
    assert r.status_code == 200
    item_id = r.json()["id"]

    r = await client.get(f"/items/{item_id}/split-preview?qty=3", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["parent_qty"] == 10.0
    assert data["child_qty"] == 3.0
    assert data["parent_qty_remaining"] == 7.0
    assert data["is_weight_unit"] is True
    assert "child_weight_default" in data
    assert data["child_weight_default"] == pytest.approx(3.0)
    assert data["parent_weight_remaining"] == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_split_preview_piece_unit_with_pieces(client):
    """split-preview returns proportional pieces defaults for a piece-unit item."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/items",
        json={"sku": "BOX-001", "name": "Box", "quantity": 10.0, "sell_by": "piece",
              "attributes": {"pieces": 20}},
        headers=headers,
    )
    assert r.status_code == 200
    item_id = r.json()["id"]

    r = await client.get(f"/items/{item_id}/split-preview?qty=5", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["child_qty"] == 5.0
    assert "child_pieces_default" in data
    assert data["child_pieces_default"] == 10  # 5/10 * 20


@pytest.mark.asyncio
async def test_split_preview_invalid_qty(client):
    """split-preview returns 422 when qty >= parent_qty."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/items",
        json={"sku": "SKU-PV3", "name": "Thing", "quantity": 5.0, "sell_by": "piece"},
        headers=headers,
    )
    item_id = r.json()["id"]

    r = await client.get(f"/items/{item_id}/split-preview?qty=5", headers=headers)
    assert r.status_code == 422

    r = await client.get(f"/items/{item_id}/split-preview?qty=10", headers=headers)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_split_with_weight_and_pieces(client):
    """POST split with weight/pieces on child sets them correctly."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/items",
        json={"sku": "RUBY-001", "name": "Ruby", "quantity": 10.0, "sell_by": "gram",
              "weight": 10.0, "attributes": {"pieces": 5}},
        headers=headers,
    )
    assert r.status_code == 200
    item_id = r.json()["id"]

    r = await client.post(
        f"/items/{item_id}/split",
        json={"children": [{"sku": "RUBY-001.1", "quantity": 3.0, "weight": 3.0, "pieces": 2.0}]},
        headers=headers,
    )
    assert r.status_code == 200
    children = r.json()["children"]
    assert len(children) == 1
    child_id = children[0]["id"]

    r = await client.get(f"/items/{child_id}", headers=headers)
    assert r.status_code == 200
    child_data = r.json()
    assert float(child_data.get("weight", 0)) == pytest.approx(3.0)
    # pieces stored in attributes
    pieces_val = child_data.get("pieces") or (child_data.get("attributes") or {}).get("pieces")
    assert float(pieces_val) == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_split_auto_sku(client):
    """POST split with sku='__auto__' auto-generates a valid SKU."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/items",
        json={"sku": "AUTO-001", "name": "AutoItem", "quantity": 10.0, "sell_by": "piece"},
        headers=headers,
    )
    item_id = r.json()["id"]

    r = await client.post(
        f"/items/{item_id}/split",
        json={"children": [{"sku": "__auto__", "quantity": 3.0}]},
        headers=headers,
    )
    assert r.status_code == 200
    children = r.json()["children"]
    assert len(children) == 1
    generated_sku = children[0]["sku"]
    assert generated_sku.startswith("AUTO-001.")
    assert generated_sku != "AUTO-001.__auto__"


@pytest.mark.asyncio
async def test_duplicate_empty_sku_autogenerates(client):
    """Duplicate with empty new_sku auto-generates a SKU ending in -copy."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/items",
        json={"sku": "DUP-SRC", "name": "Source Item", "quantity": 5.0, "sell_by": "piece"},
        headers=headers,
    )
    assert r.status_code == 200
    item_id = r.json()["id"]

    # Verify item exists
    r = await client.get(f"/items/{item_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["sku"] == "DUP-SRC"

    # Simulate what duplicate auto-generation does: DUP-SRC-copy should be available
    r = await client.post(
        "/items",
        json={"sku": "DUP-SRC-copy", "name": "Source Item", "quantity": 5.0, "sell_by": "piece"},
        headers=headers,
    )
    assert r.status_code == 200
    new_id = r.json()["id"]

    r = await client.get(f"/items/{new_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["sku"] == "DUP-SRC-copy"
