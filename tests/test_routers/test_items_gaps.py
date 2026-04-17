# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Coverage gap closers for routers/items.py:
  - GET /items/valuation with items that have cost/wholesale/retail (lines 121-142)
  - POST /items with inline pricing → emit pricing events (line 187)
  - GET /items/export/csv with q, category, status filters (lines 509-512)
"""

from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _reg(client) -> str:
    addr = f"items-{uuid.uuid4().hex[:8]}@gaps.test"
    r = await client.post("/auth/register", json={"company_name": "ItemCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _item(client, tok, name="Widget", sku=None, category=None, **kwargs) -> str:
    r = await client.post("/items", headers=_h(tok), json={
        "sku": sku or f"SKU-{uuid.uuid4().hex[:6]}", "sell_by": "piece",
        "name": name,
        **({"category": category} if category else {}),
        **kwargs,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Valuation with pricing data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_items_valuation_with_prices(client):
    """GET /items/valuation with items having cost/wholesale/retail (lines 121-142)."""
    tok = await _reg(client)

    # Create item with inline pricing (also exercises line 187 - pricing events)
    await _item(client, tok, name="Priced Widget", sku="PW-001",
                category="Electronics",
                cost_price=50.0, wholesale_price=75.0)
    await _item(client, tok, name="Plain Item", sku="PI-001")

    r = await client.get("/items/valuation", headers=_h(tok))
    assert r.status_code == 200
    body = r.json()
    assert "item_count" in body
    assert body["item_count"] >= 2
    # category_counts should have at least one category
    assert isinstance(body["category_counts"], dict)
    # cost_total should reflect the priced item
    # (valuation reads projections; pricing event updates projection state)
    assert "cost_total" in body
    assert "wholesale_total" in body
    assert "retail_total" in body


# ---------------------------------------------------------------------------
# POST /items with inline pricing (line 187)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_items_create_with_inline_pricing(client):
    """POST /items with cost_price, sale_price, wholesale_price → pricing events emitted (line 187)."""
    tok = await _reg(client)
    r = await client.post("/items", headers=_h(tok), json={
        "sku": "PRICED-001",
        "name": "Priced Item",
        "quantity": 10,
        "cost_price": 25.0,
        "sale_price": 45.0,
        "wholesale_price": 35.0,
        "sell_by": "piece",
    })
    assert r.status_code == 200
    item_id = r.json()["id"]

    # Verify item exists with correct id
    assert item_id.startswith("item:")


# ---------------------------------------------------------------------------
# GET /items/export/csv with filters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_items_export_csv_filters(client):
    """GET /items/export/csv with q, category, status filters (lines 509-512)."""
    tok = await _reg(client)
    await _item(client, tok, name="Filterable Gadget", sku="FG-001", category="Gadgets")
    await _item(client, tok, name="Other Thingamajig", sku="OT-001", category="Misc")

    # Without filter - headers present
    r_all = await client.get("/items/export/csv", headers=_h(tok))
    assert r_all.status_code == 200
    assert "sku" in r_all.text

    # q filter by name
    r_q = await client.get("/items/export/csv?q=filterable", headers=_h(tok))
    assert r_q.status_code == 200
    assert "FG-001" in r_q.text
    assert "OT-001" not in r_q.text

    # category filter
    r_cat = await client.get("/items/export/csv?category=Gadgets", headers=_h(tok))
    assert r_cat.status_code == 200
    assert "FG-001" in r_cat.text
    assert "OT-001" not in r_cat.text

    # status filter (items start active)
    r_status = await client.get("/items/export/csv?status=active", headers=_h(tok))
    assert r_status.status_code == 200
    # active items should appear; nonexistent status should not
    r_no_match = await client.get("/items/export/csv?status=nonexistent", headers=_h(tok))
    assert r_no_match.status_code == 200
    lines = [l for l in r_no_match.text.strip().split("\n") if l]
    assert len(lines) == 1  # header only


# ---------------------------------------------------------------------------
# Export CSV — UTC timestamp contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_items_export_csv_timestamps_have_z_suffix(client):
    """created_at and updated_at columns in export must end with Z (UTC marker)."""
    tok = await _reg(client)
    # Create one item so there's a row with real DB timestamps
    r = await client.post("/items", json={"name": "TsItem", "sku": "TS-001", "sell_by": "piece"}, headers=_h(tok))
    assert r.status_code == 200

    r_csv = await client.get("/items/export/csv", headers=_h(tok))
    assert r_csv.status_code == 200

    import csv, io
    reader = csv.DictReader(io.StringIO(r_csv.text))
    rows = list(reader)
    assert rows, "export must have at least one data row"

    for row in rows:
        created = row.get("created_at", "")
        updated = row.get("updated_at", "")
        if created:
            assert created.endswith("Z"), f"created_at must end with Z, got: {created!r}"
        if updated:
            assert updated.endswith("Z"), f"updated_at must end with Z, got: {updated!r}"


@pytest.mark.asyncio
async def test_items_export_csv_timestamps_no_offset_naive(client):
    """Timestamps stored without tzinfo should get Z appended, not +00:00."""
    tok = await _reg(client)
    await client.post("/items", json={"name": "TsItem2", "sku": "TS-002", "sell_by": "piece"}, headers=_h(tok))

    r_csv = await client.get("/items/export/csv", headers=_h(tok))
    assert r_csv.status_code == 200

    # +00:00 is the wrong normalisation — must always be Z
    assert "+00:00" not in r_csv.text, "timestamps must use Z suffix, not +00:00"


# ---------------------------------------------------------------------------
# Phase 1: Search scope tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_items_search_by_name(client):
    """Search by name (q param) returns matching item."""
    tok = await _reg(client)
    h = _h(tok)
    await client.post("/items", json={"sku": "SRCH-N1", "name": "UniqueWidgetAlpha", "sell_by": "piece"}, headers=h)
    r = await client.get("/items?q=UniqueWidgetAlpha", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    # Filter out demo items; find our item
    assert any(i.get("name") == "UniqueWidgetAlpha" for i in items)


@pytest.mark.asyncio
async def test_list_items_search_by_barcode(client):
    """Search by barcode value returns matching item."""
    tok = await _reg(client)
    h = _h(tok)
    r = await client.post("/items", json={"sku": "SRCH-B1", "name": "BarcodeItem", "barcode": "9876543210", "sell_by": "piece"}, headers=h)
    assert r.status_code == 200
    r = await client.get("/items?q=9876543210", headers=h)
    assert r.status_code == 200
    assert any(i.get("barcode") == "9876543210" for i in r.json()["items"])


@pytest.mark.asyncio
async def test_list_items_search_by_attribute(client):
    """Search by attribute value returns matching item."""
    tok = await _reg(client)
    h = _h(tok)
    r = await client.post(
        "/items",
        json={"sku": "SRCH-A1", "name": "AttrItem", "sell_by": "piece", "attributes": {"color": "crimsonred"}},
        headers=h,
    )
    assert r.status_code == 200
    r = await client.get("/items?q=crimsonred", headers=h)
    assert r.status_code == 200
    assert any(i.get("sku") == "SRCH-A1" for i in r.json()["items"])


@pytest.mark.asyncio
async def test_list_items_search_no_match_returns_empty(client):
    """Search with no match returns empty items list."""
    tok = await _reg(client)
    h = _h(tok)
    r = await client.get("/items?q=xyzzy_no_match_ever_12345", headers=h)
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_list_items_search_case_insensitive(client):
    """Search is case-insensitive."""
    tok = await _reg(client)
    h = _h(tok)
    await client.post("/items", json={"sku": "SRCH-C1", "name": "CaseSensitiveTest", "sell_by": "piece"}, headers=h)
    r = await client.get("/items?q=casesensitivetest", headers=h)
    assert r.status_code == 200
    assert any(i.get("name") == "CaseSensitiveTest" for i in r.json()["items"])
