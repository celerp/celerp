# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""List quantity integrity.

Three behaviours, one shared per-line validator across every list writer (create,
patch, line-page) and the scan path:

B1 - list lines are quantity-validated and resolved by their EXACT item id, not by a
     SKU map. A line linked to a piece item (decimals 0) is held to that unit's rules;
     an unlinked free-text row that merely shares a real SKU gets only the finite/type
     gate and never borrows another item's unit rules; two distinct lots sharing one
     SKU are each resolved by their own id.

B2 - a non-audit stocked line at quantity 0 is rejected (manual save) or reported in
     ``failed`` (scan); a draft audit line at 0 is allowed. One validator, writer-
     appropriate aggregation.

B3 - a scanned line carries its unit (stamped from item state), so the quantity renders
     with its unit and the real Inventory quantity is preserved.
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


async def _item(client, t, sku, *, loc, qty, barcode=None, sell_by="piece", name=None) -> str:
    body = {"status": "available", "sku": sku, "name": name or sku, "quantity": qty, "sell_by": sell_by,
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


async def _audit(client, t, loc) -> str:
    r = await client.post("/lists/audit", headers=_h(t), json={"location_id": loc})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _state(client, t, list_id) -> dict:
    return (await client.get(f"/lists/{list_id}", headers=_h(t))).json()


async def _patch_lines(client, t, list_id, new_lines, expected_version):
    return await client.patch(f"/lists/{list_id}", headers=_h(t), json={
        "expected_version": expected_version,
        "fields_changed": {"line_items": {"old": [], "new": new_lines}},
    })


# --- B1: validated and resolved by exact item id ---------------------------


@pytest.mark.asyncio
async def test_list_quantity_validated_and_resolved_by_item_id(client):
    """B1: a list line linked to a piece item is held to that item's unit rules resolved
    BY ITS OWN ID (not by a SKU map), while an unlinked row sharing the same SKU gets only
    the finite/type gate, and two distinct lots sharing one SKU are each resolved by id.

    RED-CARRYING (fails at merge-base 2e48505, where _validate_list_line_quantities does
    not exist and lists perform no quantity validation - G1): a line LINKED to a piece item
    (decimals 0) with quantity 2.5 returns 422 "exceeds allowed precision"; at merge-base it
    persists (200).
    COMPANIONS (identical at both, locking id-resolution): an unlinked free-text row sharing
    that SKU with quantity 2.5 persists (finite gate only, no borrowed unit rule); two distinct
    non-splittable item ids sharing one SKU both persist; a non-number/NaN/inf quantity on any
    line is rejected by the unconditional finite/type gate.
    """
    t = await _register(client)
    loc = await _location(client, t)
    # Two distinct lots (distinct ids) deliberately sharing one SKU, both "piece".
    iid_a = await _item(client, t, "SHARE-SKU", loc=loc, qty=5, name="Lot A")
    iid_b = await _item(client, t, "SHARE-SKU", loc=loc, qty=5, name="Lot B")
    assert iid_a != iid_b

    q = await _quotation(client, t)
    v0 = (await _state(client, t, q))["version"]

    # RED-CARRYING: a linked piece line at 2.5 is rejected by the id-resolved unit rule.
    # The line carries NO sell_by of its own, so the resolution must come from the item id.
    r = await _patch_lines(client, t, q, [
        {"item_id": iid_a, "sku": "SHARE-SKU", "name": "Lot A", "quantity": 2.5},
    ], v0)
    assert r.status_code == 422, r.text
    assert "precision" in r.text.lower()
    after = await _state(client, t, q)
    assert after["version"] == v0                        # nothing persisted
    assert (after.get("line_items") or []) == []

    # COMPANION: an UNLINKED free-text row sharing the SKU with 2.5 persists - only the
    # finite gate applies, no borrowed piece-unit rule.
    r = await _patch_lines(client, t, q, [
        {"sku": "SHARE-SKU", "name": "Free text", "description": "Free text", "quantity": 2.5},
    ], v0)
    assert r.status_code == 200, r.text
    v1 = (await _state(client, t, q))["version"]

    # COMPANION: two DISTINCT lot ids sharing one SKU both persist (each resolved by its own
    # id; neither borrows the other, and 3 is integer-valid for piece).
    r = await _patch_lines(client, t, q, [
        {"item_id": iid_a, "sku": "SHARE-SKU", "name": "Lot A", "quantity": 3},
        {"item_id": iid_b, "sku": "SHARE-SKU", "name": "Lot B", "quantity": 3},
    ], v1)
    assert r.status_code == 200, r.text
    lines = (await _state(client, t, q))["line_items"]
    assert {li.get("item_id") for li in lines} == {iid_a, iid_b}

    # COMPANION: the unconditional finite/type gate rejects a non-number / NaN / inf on ANY
    # line, including one with no resolvable sell_by (the gate runs before delegation).
    v2 = (await _state(client, t, q))["version"]
    for bad in ["abc", "nan", "inf", None]:
        r = await _patch_lines(client, t, q, [
            {"name": "Free text", "description": "Free text", "quantity": bad},
        ], v2)
        assert r.status_code == 422, f"{bad!r}: {r.text}"
        assert (await _state(client, t, q))["version"] == v2   # no mutation


# --- B2: non-audit stocked zero rejected; audit zero allowed ---------------


@pytest.mark.asyncio
async def test_nonaudit_stocked_zero_rejected_audit_zero_allowed(client):
    """B2: a non-audit stocked line at 0 is rejected (manual PATCH) and reported in ``failed``
    (scan), through the SAME shared validator; a draft audit line at 0 is allowed.

    RED-CARRYING (manual, fails at merge-base per G1 - no list validation): a PATCH of a
    non-audit stocked List line to quantity 0 returns 422; at merge-base it persists (200).
    RED-CARRYING (scan, fails at merge-base per G1 - scan writes without the validator): a scan
    resolving to a zero-on-hand non-audit stocked line reports scanned==0 and the line goes to
    ``failed``; at merge-base the scan persists it (scanned==1).
    COMPANION (identical at both): a draft audit zero-on-hand line saves and reloads at 0
    (require_positive False for audit).
    """
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "ZQ-1", loc=loc, qty=5)
    zero_bc = "930001"
    await _item(client, t, "ZS-1", loc=loc, qty=0, barcode=zero_bc)

    q = await _quotation(client, t)
    v0 = (await _state(client, t, q))["version"]

    # RED-CARRYING (manual): a non-audit stocked line at 0 is rejected.
    r = await _patch_lines(client, t, q, [
        {"item_id": iid, "sku": "ZQ-1", "name": "ZQ-1", "quantity": 0},
    ], v0)
    assert r.status_code == 422, r.text
    after = await _state(client, t, q)
    assert after["version"] == v0
    assert (after.get("line_items") or []) == []

    # RED-CARRYING (scan): a scan resolving to a zero-on-hand stocked line is reported in
    # `failed`, not silently persisted.
    r = await client.post(f"/lists/{q}/scan", headers=_h(t), json={"barcode": zero_bc})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scanned"] == 0, body
    assert any(f.get("code") == zero_bc for f in body.get("failed") or []), body
    lines = (await _state(client, t, q)).get("line_items") or []
    assert lines == [], lines                             # nothing appended

    # COMPANION (green at both): a draft audit line at 0 saves and reloads at 0.
    audit = await _audit(client, t, loc)
    astate = await _state(client, t, audit)
    av0 = astate["version"]
    # Take the seeded audit line and set its quantity to 0, save through patch_list.
    seeded = list(astate.get("line_items") or [])
    assert seeded, "audit should seed at least one on-hand line"
    seeded[0]["quantity"] = 0
    r = await client.patch(f"/lists/{audit}", headers=_h(t), json={
        "expected_version": av0,
        "fields_changed": {"line_items": {"old": astate.get("line_items"), "new": seeded}},
    })
    assert r.status_code == 200, r.text
    reloaded = (await _state(client, t, audit))["line_items"]
    assert float(reloaded[0]["quantity"]) == 0.0


# --- B3: scanned line carries its unit -------------------------------------


@pytest.mark.asyncio
async def test_scanned_line_carries_unit(client):
    """B3: a scanned non-audit list line carries its unit (from item state) and its real
    Inventory quantity.

    RED (fails at merge-base per G3 - _scan_line_from_item sets no "unit" key): a 650 gram scan
    reloads with no/empty unit; at head the reloaded line has unit=="gram" and quantity==650.
    """
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "GR-1", loc=loc, qty=650, barcode="940001", sell_by="gram")
    q = await _quotation(client, t)

    r = await client.post(f"/lists/{q}/scan", headers=_h(t), json={"barcode": "940001"})
    assert r.status_code == 200, r.text
    assert r.json()["scanned"] == 1
    lines = (await _state(client, t, q))["line_items"]
    assert len(lines) == 1
    assert lines[0].get("unit") == "gram"                 # RED at merge-base: no unit key
    assert float(lines[0]["quantity"]) == 650.0           # real Inventory quantity preserved
