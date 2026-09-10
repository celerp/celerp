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
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp_inventory.routes import resolve_item_by_code


async def _company_id(session, entity_id: str):
    """The company that owns a created item, read back from its projection."""
    return (await session.execute(
        select(Projection.company_id).where(Projection.entity_id == entity_id)
    )).scalars().first()


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


# --- B5: internal barcode allocator must honour the shared barcode/EPC namespace ---
#
# The internal sequential allocator mints the next numeric code from SKUs and barcodes
# only. An existing item's numeric rfid_epc occupies the SAME physical-code namespace
# (barcode and rfid_epc share one space), so minting a barcode equal to a live EPC puts
# one value on two physical items - which the two separate DB unique indexes cannot
# catch. Every internal-mint path (split, transform, merge, ...) must skip a candidate
# already held as a barcode OR an rfid_epc.


@pytest.mark.asyncio
async def test_split_minted_barcode_skips_value_held_as_epc(client, session):
    """RED at merge base: the split allocator computes the next code from integer SKUs
    and barcodes only, so with parent barcode 000122 the next code is 123 and the child
    is minted barcode '000123' - the exact value an existing item already holds as its
    rfid_epc. After the fix the allocator skips any candidate in use as a barcode or an
    EPC, so the child gets the next free code and the EPC still resolves to one item."""
    h = _h(await _token(client))
    # An item holding the numeric value "000123" as its physical EPC tag (no barcode).
    epc_holder = (await client.post(
        "/items",
        json={"status": "available", "sku": "EPC-HOLDER", "name": "EpcHolder",
              "quantity": 1.0, "sell_by": "piece", "rfid_epc": "000123"},
        headers=h,
    )).json()["id"]
    # Parent whose numeric barcode 000122 makes the next internal sequence 123.
    parent = (await client.post(
        "/items",
        json={"status": "available", "sku": "PARENT-SEQ", "name": "Parent",
              "quantity": 10.0, "sell_by": "piece", "barcode": "000122"},
        headers=h,
    )).json()["id"]

    r = await client.post(
        f"/items/{parent}/split",
        json={"children": [{"sku": "CH.1", "quantity": 3.0}]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    child = await _item(client, h, r.json()["children"][0]["id"])

    assert child.get("barcode") != "000123", \
        f"split minted a barcode equal to an existing EPC: {child.get('barcode')!r}"
    cid = await _company_id(session, parent)
    res = await resolve_item_by_code(session, cid, "000123")
    assert res.duplicate_physical is False, "EPC 000123 now spans two physical items"
    assert res.one is not None and res.one.entity_id == epc_holder, \
        f"EPC 000123 must resolve to its original holder, got {res.one}"


@pytest.mark.asyncio
async def test_transform_minted_barcode_skips_value_held_as_epc(client, session):
    """RED at merge base: transform mints through allocate_internal_codes, which returns
    the raw next sequential code with no availability check, so it produces '000123' even
    though an existing item holds it as an rfid_epc. After the fix the hardened allocator
    skips it. This targets the allocator itself (merge mints the same way)."""
    h = _h(await _token(client))
    epc_holder = (await client.post(
        "/items",
        json={"status": "available", "sku": "EPC-HOLDER-T", "name": "EpcHolderT",
              "quantity": 1.0, "sell_by": "piece", "rfid_epc": "000123"},
        headers=h,
    )).json()["id"]
    parent = (await client.post(
        "/items",
        json={"status": "available", "sku": "TR-SEQ", "name": "TransformParent",
              "quantity": 10.0, "sell_by": "piece", "category": "Raw",
              "cost_price": 100.0, "barcode": "000122"},
        headers=h,
    )).json()["id"]

    r = await client.post(
        f"/items/{parent}/transform",
        json={"child_sku": "TR-CHILD", "child_category": "Processed",
              "child_sell_by": "gram", "child_quantity": 8.0, "child_cost_total": 100.0},
        headers=h,
    )
    assert r.status_code == 200, r.text
    child = await _item(client, h, r.json()["child_id"])

    assert child.get("barcode") != "000123", \
        f"transform minted a barcode equal to an existing EPC: {child.get('barcode')!r}"
    cid = await _company_id(session, parent)
    res = await resolve_item_by_code(session, cid, "000123")
    assert res.duplicate_physical is False, "EPC 000123 now spans two physical items"
    assert res.one is not None and res.one.entity_id == epc_holder


@pytest.mark.asyncio
async def test_split_manual_child_barcode_colliding_with_epc_rejected_409(client, session):
    """RED at merge base: a user-supplied split-child barcode is inserted verbatim with
    no namespace check, so a barcode equal to an existing item's rfid_epc is accepted and
    the value ends up on two physical items. After the fix it surfaces the clean 409
    conflict path and nothing is created."""
    h = _h(await _token(client))
    epc_holder = (await client.post(
        "/items",
        json={"status": "available", "sku": "EPC-HOLDER-M", "name": "EpcHolderM",
              "quantity": 1.0, "sell_by": "piece", "rfid_epc": "000555"},
        headers=h,
    )).json()["id"]
    parent = (await client.post(
        "/items",
        json={"status": "available", "sku": "PARENT-M", "name": "ParentM",
              "quantity": 10.0, "sell_by": "piece", "barcode": "000100"},
        headers=h,
    )).json()["id"]

    r = await client.post(
        f"/items/{parent}/split",
        json={"children": [{"sku": "CH.M", "quantity": 3.0, "barcode": "000555"}]},
        headers=h,
    )
    assert r.status_code == 409, \
        f"manual child barcode equal to an existing EPC must 409, got {r.status_code}: {r.text}"
    cid = await _company_id(session, parent)
    res = await resolve_item_by_code(session, cid, "000555")
    assert res.duplicate_physical is False, "EPC 000555 now spans two physical items"
    assert res.one is not None and res.one.entity_id == epc_holder


# --- B6: GTIN must survive the two same-product physical-lot creation paths that build
# a minimal child dict (partial fulfillment, customer returns). Both are new lots of the
# SAME product, so the product GTIN carries; the barcode is freshly minted and the
# physical rfid_epc tag never carries.


@pytest.mark.asyncio
async def test_partial_fulfillment_child_inherits_gtin_fresh_barcode_no_epc(client, session):
    """A partial draw on an invoice line carves a child lot of the same product. The
    child must inherit the product GTIN, mint a fresh barcode, and carry no physical EPC,
    and the GTIN must resolve to the child among the product's lots."""
    h = _h(await _token(client))
    parent = (await client.post(
        "/items",
        json={"status": "available", "sku": "FUL-GTIN", "name": "Fulfillable",
              "quantity": 10.0, "sell_by": "piece", "gtin": "12345670",
              "rfid_epc": "FULTAG", "barcode": "910100"},
        headers=h,
    )).json()["id"]

    doc = (await client.post(
        "/docs",
        json={"doc_type": "invoice", "line_items": [
            {"entity_id": parent, "sku": "FUL-GTIN", "name": "Fulfillable",
             "quantity": 3.0, "unit_price": 5, "sell_by": "piece"}]},
        headers=h,
    )).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=h)).status_code == 200

    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=h,
                          json={"line_entity_ids": [parent]})
    assert r.status_code == 200, r.text
    child_eid = r.json()["fulfilled"][0]
    assert child_eid != parent, f"partial fulfillment must carve a child, got {child_eid}"
    child = await _item(client, h, child_eid)

    assert child.get("gtin") == "12345670", \
        f"fulfillment split child must inherit the product gtin, got {child.get('gtin')!r}"
    assert child.get("barcode") and child.get("barcode") != "910100", \
        f"fulfillment split child must get a fresh barcode, got {child.get('barcode')!r}"
    assert _absent(child.get("rfid_epc")), \
        f"fulfillment split child must not inherit the parent physical tag, got {child.get('rfid_epc')!r}"
    cid = await _company_id(session, parent)
    res = await resolve_item_by_code(session, cid, "12345670")
    assert child_eid in {m.entity_id for m in res.matches}, \
        "the product gtin must resolve to the fulfillment split child"


@pytest.mark.asyncio
async def test_customer_return_parcel_inherits_gtin_fresh_barcode_no_epc(client, session):
    """RED at merge base: the returned-parcel dict is built minimally and omits gtin, so
    the returned lot loses the product identifier. A returned parcel is a new lot of the
    same product: it must carry the original item's GTIN, mint a fresh barcode, and carry
    no physical EPC."""
    h = _h(await _token(client))
    sold = (await client.post(
        "/items",
        json={"status": "available", "sku": "W-RET-G", "name": "Returnable",
              "barcode": "556001", "quantity": 1.0, "cost_price": 40.0,
              "unit_price": 50.0, "sell_by": "piece", "gtin": "12345670",
              "rfid_epc": "SOLDTAG"},
        headers=h,
    )).json()["id"]
    await client.post(f"/items/{sold}/status", headers=h, json={"new_status": "sold"})

    inv = (await client.post(
        "/docs",
        json={"doc_type": "invoice", "line_items": [
            {"name": "Returnable", "sku": "W-RET-G", "quantity": 1, "unit_price": 50, "sell_by": "unit"}],
         "subtotal": 50, "tax": 0, "total": 50},
        headers=h,
    )).json()["id"]
    await client.post(f"/docs/{inv}/finalize", headers=h)

    cn = (await client.post(
        "/docs",
        json={"doc_type": "credit_note", "original_doc_id": inv, "line_items": [
            {"name": "Returnable", "sku": "W-RET-G", "quantity": 1, "unit_price": 50, "sell_by": "unit"}],
         "subtotal": 50, "tax": 0, "total": 50},
        headers=h,
    )).json()["id"]
    await client.post(f"/docs/{cn}/finalize", headers=h)

    r = await client.post(f"/docs/{cn}/receive-return", headers=h,
                          json={"items": [{"sku": "W-RET-G", "quantity": 1}]})
    assert r.status_code == 200, r.text
    returned_id = r.json()["received_items"][0]["item_id"]
    returned = await _item(client, h, returned_id)

    assert returned.get("gtin") == "12345670", \
        f"returned parcel must inherit the product gtin, got {returned.get('gtin')!r}"
    assert returned.get("barcode") and returned.get("barcode") != "556001", \
        f"returned parcel must get a fresh barcode, got {returned.get('barcode')!r}"
    assert _absent(returned.get("rfid_epc")), \
        f"returned parcel is a new physical unit; rfid_epc must not carry, got {returned.get('rfid_epc')!r}"
    cid = await _company_id(session, sold)
    res = await resolve_item_by_code(session, cid, "12345670")
    assert returned_id in {m.entity_id for m in res.matches}, \
        "the product gtin must resolve to the returned parcel"
