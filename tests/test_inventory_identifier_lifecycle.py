# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Lifecycle correctness for the two physical-tag identifiers gtin and rfid_epc.

gtin is a product-type identifier (it travels with the SKU / product family, so a
split child and a received parcel of the same product share it, and a transform to a
different product must drop it). rfid_epc is a physical-tag identifier bound to one
physical unit, so it must NEVER survive an operation that creates a new physical unit:
split, transform, clone, receive, and merge each mint a new unit and so must clear it.

These tests pin the LIFECYCLE (carried vs cleared), never a bare value round-trip: the
item event model is extra=allow, so a raw value persists regardless. The wrong thing
at merge base is which identifier survives each operation.
"""

from __future__ import annotations

import uuid

import pytest


async def _token(client) -> str:
    email = f"ident-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Acme", "email": email, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _item(client, headers, entity_id: str) -> dict:
    r = await client.get(f"/items/{entity_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _absent(value) -> bool:
    """An identifier is cleared when it is missing, None, or empty."""
    return value in (None, "")


@pytest.mark.asyncio
async def test_split_inherits_gtin_fresh_barcode_no_epc(client):
    """RED at merge base: _CHILD_RESET_FIELDS omits rfid_epc, so a split child wrongly
    carries the parent's physical tag. gtin (product-type) is correctly inherited and
    the barcode is correctly reminted; only the rfid_epc carry-over is wrong."""
    h = _h(await _token(client))
    r = await client.post(
        "/items",
        json={"status": "available", "sku": "PARENT-GTIN", "name": "Parent",
              "quantity": 10.0, "sell_by": "piece",
              "gtin": "12345670", "rfid_epc": "TAGPARENT", "barcode": "900100"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    r = await client.post(
        f"/items/{parent_id}/split",
        json={"children": [{"sku": "CH.1", "quantity": 3.0}]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    child_id = r.json()["children"][0]["id"]

    child = await _item(client, h, child_id)
    assert child.get("gtin") == "12345670", f"child must inherit product gtin, got {child.get('gtin')!r}"
    assert child.get("barcode") and child.get("barcode") != "900100", \
        f"child must get a fresh barcode, got {child.get('barcode')!r}"
    assert _absent(child.get("rfid_epc")), \
        f"child must not inherit the parent physical tag, got {child.get('rfid_epc')!r}"


@pytest.mark.asyncio
async def test_transform_clears_gtin_no_epc(client):
    """RED at merge base: transform copy-all-then-override keeps both gtin and rfid_epc
    (neither is in _CHILD_RESET_FIELDS). A transform produces a DIFFERENT product on a
    new physical unit, so gtin must be cleared and rfid_epc must not carry over."""
    h = _h(await _token(client))
    r = await client.post(
        "/items",
        json={"status": "available", "sku": "TRANS-PARENT", "name": "Parent",
              "quantity": 10.0, "sell_by": "piece", "category": "Raw",
              "cost_price": 100.0, "gtin": "12345670", "rfid_epc": "TAGP"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    r = await client.post(
        f"/items/{parent_id}/transform",
        json={"child_sku": "CHILD", "child_category": "Processed",
              "child_sell_by": "gram", "child_quantity": 8.0, "child_cost_total": 100.0},
        headers=h,
    )
    assert r.status_code == 200, r.text
    child_id = r.json()["child_id"]

    child = await _item(client, h, child_id)
    assert _absent(child.get("gtin")), \
        f"transform child is a different product; gtin must be cleared, got {child.get('gtin')!r}"
    assert _absent(child.get("rfid_epc")), \
        f"transform child is a new physical unit; rfid_epc must be cleared, got {child.get('rfid_epc')!r}"


@pytest.mark.asyncio
async def test_clone_auto_barcode_resets_epc_keeps_gtin(client):
    """RED at merge base: auto_barcode mints a fresh barcode only; a payload rfid_epc is
    stored verbatim via extra=allow. A clone is a new physical unit, so its physical tag
    must be cleared while the product-type gtin is kept."""
    h = _h(await _token(client))
    r = await client.post(
        "/items",
        json={"status": "available", "sku": "CLONE-1", "name": "Clone",
              "quantity": 1.0, "sell_by": "piece", "auto_barcode": True,
              "gtin": "12345670", "rfid_epc": "CLONETAG"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    clone_id = r.json()["id"]

    clone = await _item(client, h, clone_id)
    assert clone.get("gtin") == "12345670", f"clone must keep the product gtin, got {clone.get('gtin')!r}"
    assert _absent(clone.get("rfid_epc")), \
        f"clone is a new physical unit; rfid_epc must be cleared, got {clone.get('rfid_epc')!r}"


@pytest.mark.asyncio
async def test_receive_inherits_gtin_not_epc(client):
    """RED at merge base: receive copies the template's attributes wholesale onto the new
    parcel, so a template physical tag rides along and the parcel wrongly carries the
    template's rfid_epc. A received parcel is a new physical unit: it must inherit the
    product gtin from its template but never a physical tag."""
    h = _h(await _token(client))
    loc = (await client.post("/companies/me/locations", headers=h,
                             json={"name": "WH", "type": "warehouse"})).json()["id"]

    # Catalog template carrying a product gtin AND a physical tag. The tag must not
    # propagate to a received parcel; only the product gtin should.
    goods = (await client.post(
        "/items",
        json={"status": "available", "sku": "WIDGET", "name": "WIDGET",
              "quantity": 0, "sell_by": "piece", "gtin": "12345670",
              "rfid_epc": "TEMPLATETAG"},
        headers=h,
    )).json()["id"]

    bill = (await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [{"item_id": goods, "sku": "WIDGET", "name": "WIDGET",
                        "quantity": 5, "unit_price": 10, "line_total": 50}],
        "total": 50,
    })).json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{bill}/receive", headers=h, json={
        "location_id": loc,
        "received_items": [{"item_id": goods, "sku": "WIDGET", "name": "WIDGET",
                            "quantity_received": 5, "cost_price": 10, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text

    items = (await client.get("/items?q=WIDGET", headers=h)).json()["items"]
    parcels = [i for i in items if i["id"] != goods and float(i.get("quantity") or 0) > 0]
    assert len(parcels) == 1, f"expected exactly one received parcel, got {len(parcels)}"
    parcel = parcels[0]
    assert parcel.get("gtin") == "12345670", \
        f"received parcel must inherit the product gtin, got {parcel.get('gtin')!r}"
    assert _absent(parcel.get("rfid_epc")), \
        f"received parcel is a new physical unit; it must not inherit the template " \
        f"physical tag, got {parcel.get('rfid_epc')!r}"


@pytest.mark.asyncio
async def test_merge_carries_gtin_not_epc(client):
    """RED at merge base: merge create_data omits gtin entirely, so the merged item lacks
    the product gtin. The merged item is a new physical unit of the target's product, so
    it must carry the target's gtin and must have no physical tag."""
    h = _h(await _token(client))

    src_a = (await client.post(
        "/items",
        json={"status": "available", "sku": "AAA", "name": "AAA", "quantity": 5.0,
              "sell_by": "piece", "category": "widgets",
              "gtin": "12345670", "rfid_epc": "SRCTAG"},
        headers=h,
    )).json()["id"]
    src_b = (await client.post(
        "/items",
        json={"status": "available", "sku": "BBB", "name": "BBB", "quantity": 3.0,
              "sell_by": "piece", "category": "widgets"},
        headers=h,
    )).json()["id"]

    r = await client.post(
        "/items/merge",
        json={"source_entity_ids": [src_a, src_b], "target_sku_from": src_a},
        headers=h,
    )
    assert r.status_code == 200, r.text
    merged_id = r.json()["id"]

    merged = await _item(client, h, merged_id)
    assert merged.get("gtin") == "12345670", \
        f"merged item must carry the target product gtin, got {merged.get('gtin')!r}"
    assert _absent(merged.get("rfid_epc")), \
        f"merged item is a new physical unit; rfid_epc must be cleared, got {merged.get('rfid_epc')!r}"
