# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""List quantity integrity: the backend rejects malformed list-line quantities at every list writer
(create, patch, line-page) before any mutation, and a scanned non-audit list line carries the real
Inventory quantity rather than a hardcoded 1. The own type/finiteness gate closes the holes that
validate_line_quantity leaves open (absent sell_by short-circuit; NaN and bool passing validate_positive).
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@listqty.test"
    r = await client.post("/auth/register", json={"company_name": "List Qty Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _location(client, t, name="Warehouse A") -> str:
    r = await client.post("/companies/me/locations", headers=_h(t), json={"name": name, "type": "warehouse"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _item(client, t, sku, *, loc, qty, barcode=None, sell_by="piece") -> str:
    body = {"status": "available", "sku": sku, "name": sku, "quantity": qty, "sell_by": sell_by,
            "location_id": loc, "inventory_type": "stocked"}
    if barcode:
        body["barcode"] = barcode
    r = await client.post("/items", headers=_h(t), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _quotation(client, t) -> str:
    r = await client.post("/lists", headers=_h(t), json={"list_type": "quotation"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _state(client, t, list_id) -> dict:
    return (await client.get(f"/lists/{list_id}", headers=_h(t))).json()


async def _list_count(client, t) -> int:
    """Number of lists currently stored for the company (the index endpoint's own total)."""
    r = await client.get("/lists", headers=_h(t))
    assert r.status_code == 200, r.text
    return r.json()["total"]


# --- create_list -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_qty", [None, "nan", "inf", "abc"])
async def test_list_create_rejects_bad_quantity(client, bad_qty):
    """create_list with a None/NaN/inf/unparsable quantity is rejected 422 naming the line, and no
    list is created. Red at merge-base: no list-qty validation exists, so the bad list is created."""
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "CQ-1", loc=loc, qty=5)
    before = await _list_count(client, t)

    r = await client.post("/lists", headers=_h(t), json={
        "list_type": "quotation",
        "line_items": [{"item_id": iid, "sku": "CQ-1", "name": "CQ-1", "quantity": bad_qty}],
    })
    assert r.status_code == 422, r.text
    assert "CQ-1" in r.text                      # the offending line/SKU is named
    assert await _list_count(client, t) == before  # nothing created


# --- patch_list ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_qty", [None, "nan", "inf", "abc"])
async def test_list_patch_rejects_bad_quantity(client, bad_qty):
    """patch_list replacing line_items with a bad quantity is rejected 422 and mutates nothing. Red at
    merge-base: the patch is accepted and the bad line persisted."""
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "PQ-1", loc=loc, qty=5)
    q = await _quotation(client, t)

    before = await _state(client, t, q)
    v0 = before["version"]
    line = {"item_id": iid, "sku": "PQ-1", "name": "PQ-1", "quantity": bad_qty}
    r = await client.patch(f"/lists/{q}", headers=_h(t), json={
        "expected_version": v0,
        "fields_changed": {"line_items": {"old": [], "new": [line]}},
    })
    assert r.status_code == 422, r.text
    assert "PQ-1" in r.text

    after = await _state(client, t, q)
    assert after["version"] == v0                 # version unchanged
    assert (after.get("line_items") or []) == (before.get("line_items") or [])  # no mutation


# --- patch_list_line_page --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_qty", [None, "nan", "inf", "abc"])
async def test_list_line_page_rejects_bad_quantity(client, bad_qty):
    """patch_list_line_page with a bad quantity is rejected 422, no splice. Red at merge-base: the page
    is spliced in and the bad line persisted."""
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "LP-1", loc=loc, qty=5)
    q = await _quotation(client, t)

    before = await _state(client, t, q)
    v0 = before["version"]
    page = [{"item_id": iid, "sku": "LP-1", "name": "LP-1", "quantity": bad_qty}]
    r = await client.patch(f"/lists/{q}/line-page", headers=_h(t), json={
        "line_items": page, "offset": 0, "original_count": 0, "expected_version": v0,
    })
    assert r.status_code == 422, r.text
    assert "LP-1" in r.text

    after = await _state(client, t, q)
    assert after["version"] == v0
    assert (after.get("line_items") or []) == (before.get("line_items") or [])


# --- the own type/finiteness gate (sell_by absent) -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_qty", ["nan", True])
async def test_list_rejects_quantity_nan_and_bool_without_sell_by(client, bad_qty):
    """A list line whose sell_by is absent and whose quantity is NaN or True (bool) is rejected 422, no
    mutation. This proves the OWN gate: validate_line_quantity short-circuits on absent sell_by and its
    validate_positive lets NaN/True pass, so a naive delegate would accept these. Red at merge-base:
    no list-qty validation at all, so the line persists."""
    t = await _register(client)
    loc = await _location(client, t)
    # Free-text line: no item_id, no sku -> no sell_by resolvable; validate_line_quantity would skip.
    q = await _quotation(client, t)

    before = await _state(client, t, q)
    v0 = before["version"]
    line = {"name": "Free text line", "description": "Free text line", "quantity": bad_qty}
    r = await client.patch(f"/lists/{q}", headers=_h(t), json={
        "expected_version": v0,
        "fields_changed": {"line_items": {"old": [], "new": [line]}},
    })
    assert r.status_code == 422, r.text

    after = await _state(client, t, q)
    assert after["version"] == v0
    assert (after.get("line_items") or []) == (before.get("line_items") or [])


# --- scan reads the real Inventory quantity --------------------------------


@pytest.mark.asyncio
async def test_scan_line_uses_real_inventory_quantity(client):
    """A non-audit list scan line carries the scanned item's Inventory quantity, not a hardcoded 1. Red
    at merge-base: _scan_line_from_item hardcodes line['quantity'] = 1 for non-audit lists."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "SC-1", loc=loc, qty=7, barcode="910001")
    q = await _quotation(client, t)

    r = await client.post(f"/lists/{q}/scan", headers=_h(t), json={"barcode": "910001"})
    assert r.status_code == 200, r.text
    assert r.json()["scanned"] == 1
    lines = (await _state(client, t, q))["line_items"]
    assert len(lines) == 1
    assert lines[0]["item_id"] is not None
    assert lines[0]["quantity"] == 7.0            # real Inventory quantity, not 1


@pytest.mark.asyncio
async def test_list_quantity_zero_preserved(client):
    """A scanned non-audit list line for an item with zero on-hand keeps quantity 0, not coerced to 1.
    Red at merge-base: _scan_line_from_item hardcodes 1 for non-audit lists, so 0 becomes 1."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "Z0-1", loc=loc, qty=0, barcode="920001")
    q = await _quotation(client, t)

    r = await client.post(f"/lists/{q}/scan", headers=_h(t), json={"barcode": "920001"})
    assert r.status_code == 200, r.text
    assert r.json()["scanned"] == 1
    lines = (await _state(client, t, q))["line_items"]
    assert len(lines) == 1
    assert lines[0]["quantity"] == 0.0            # preserved, not coerced to 1
