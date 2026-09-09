# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""List quantity integrity: the backend rejects malformed list-line quantities at every list writer
(create, patch, line-page) before any mutation, resolves each line's unit by its exact linked item id
(never a shared SKU), issues one bounded item query rather than a full-inventory scan, and a scanned
non-audit list line carries the real Inventory quantity AND its unit. Audit snapshots may legitimately
be zero; normal stocked lines follow the positive rule. The own type/finiteness gate closes the holes
that validate_line_quantity leaves open (absent sell_by short-circuit; NaN and bool passing
validate_positive).
"""
from __future__ import annotations

import uuid

import pytest

import celerp_docs.routes as docs_routes


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


async def _item(client, t, sku, *, loc, qty, barcode=None, sell_by="piece", allow_splitting=False) -> str:
    body = {"status": "available", "sku": sku, "name": sku, "quantity": qty, "sell_by": sell_by,
            "location_id": loc, "inventory_type": "stocked", "allow_splitting": allow_splitting}
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


# --- B1: bounded query, exact-id identity ----------------------------------


@pytest.mark.asyncio
async def test_list_validation_does_not_scan_full_inventory(client, monkeypatch):
    """List-line validation resolves each line's unit by a bounded query on the submitted item ids,
    never a full-inventory SKU scan. Guard: the full-inventory helper _get_item_sell_by_map is made to
    raise; a create_list with a valid linked line must still succeed, proving the validator never calls
    it. Red at the PR head, where the validator loads all inventory via _get_item_sell_by_map and this
    raises 500; after the fix the bounded id query is used and the list is created."""
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "BND-1", loc=loc, qty=5)

    async def _boom(session, company_id):
        raise RuntimeError("full-inventory sell_by map must not be loaded during list validation")

    monkeypatch.setattr(docs_routes, "_get_item_sell_by_map", _boom)

    r = await client.post("/lists", headers=_h(t), json={
        "list_type": "quotation",
        "line_items": [{"item_id": iid, "sku": "BND-1", "name": "BND-1", "quantity": 3}],
    })
    assert r.status_code == 200, r.text
    lines = (await _state(client, t, r.json()["id"]))["line_items"]
    assert len(lines) == 1 and float(lines[0]["quantity"]) == 3.0


@pytest.mark.asyncio
async def test_free_text_line_ignores_matching_sku(client):
    """A free-text list line (no item_id) whose sku matches a real 0-decimal piece item is bound only by
    the own finiteness gate, never that item's per-unit rule: a fractional quantity persists. Red at the
    PR head, where the validator resolves sell_by by SKU (sell_by_map.get('DUP') -> piece, 0 decimals)
    and 422s the fractional value; after the fix sell_by is resolved by exact id only, so the unlinked
    line has no unit rule and 2.5 is accepted."""
    t = await _register(client)
    loc = await _location(client, t)
    # A real piece item (0 decimals) carrying SKU DUP.
    await _item(client, t, "DUP", loc=loc, qty=5, sell_by="piece")
    q = await _quotation(client, t)

    before = await _state(client, t, q)
    v0 = before["version"]
    # Free-text line: no item_id, SKU "DUP" matches the piece item, fractional qty.
    line = {"sku": "DUP", "name": "Free text DUP", "description": "Free text DUP", "quantity": 2.5}
    r = await client.patch(f"/lists/{q}", headers=_h(t), json={
        "expected_version": v0,
        "fields_changed": {"line_items": {"old": [], "new": [line]}},
    })
    assert r.status_code == 200, r.text
    lines = (await _state(client, t, q))["line_items"]
    assert len(lines) == 1 and float(lines[0]["quantity"]) == 2.5


@pytest.mark.asyncio
async def test_shared_sku_distinct_lots_both_allowed(client):
    """Two distinct non-splittable lots sharing one SKU are both allowed on a List with valid
    quantities. Green before and after: a list is entity_type 'list', never governed by the document
    uniqueness guard, and per-id resolution treats the two lots independently. Pins that identity is
    per-item-id, not per-SKU."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "LOT", loc=loc, qty=5, sell_by="piece")
    b = await _item(client, t, "LOT", loc=loc, qty=5, sell_by="piece")
    q = await _quotation(client, t)

    before = await _state(client, t, q)
    v0 = before["version"]
    r = await client.patch(f"/lists/{q}", headers=_h(t), json={
        "expected_version": v0,
        "fields_changed": {"line_items": {"old": [], "new": [
            {"item_id": a, "sku": "LOT", "name": "LOT", "quantity": 2},
            {"item_id": b, "sku": "LOT", "name": "LOT", "quantity": 3},
        ]}},
    })
    assert r.status_code == 200, r.text
    lines = (await _state(client, t, q))["line_items"]
    assert len(lines) == 2
    assert {a, b} == {li.get("item_id") for li in lines}


# --- B3: scanned line carries its unit -------------------------------------


@pytest.mark.asyncio
async def test_scanned_line_carries_unit_and_real_qty(client):
    """A scanned gram item with 650 on-hand adds a line carrying quantity 650 AND unit/sell_by 'gram'.
    Red at merge-base: _scan_line_from_item copies quantity but sets no unit, so the stored line has no
    unit; after the fix it carries unit == 'gram' and sell_by == 'gram'."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "GR-1", loc=loc, qty=650, barcode="930650", sell_by="gram")
    q = await _quotation(client, t)

    r = await client.post(f"/lists/{q}/scan", headers=_h(t), json={"barcode": "930650"})
    assert r.status_code == 200, r.text
    assert r.json()["scanned"] == 1
    lines = (await _state(client, t, q))["line_items"]
    assert len(lines) == 1
    assert float(lines[0]["quantity"]) == 650.0
    assert lines[0].get("unit") == "gram"
    assert lines[0].get("sell_by") == "gram"


# --- B2: scan shares validation; zero non-audit stocked is invalid ---------


@pytest.mark.asyncio
async def test_zero_on_hand_scanned_non_audit_reported_not_persisted(client):
    """Scanning a zero-on-hand stocked item onto a quotation reports it in `failed` and persists no
    line: scan validates through the same per-line rule as ordinary writers, and a non-audit stocked
    line must be positive. Red at merge-base: the scan persists an unvalidated qty-0 line; after the fix
    it is reported and skipped."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "Z0-1", loc=loc, qty=0, barcode="920001", sell_by="piece")
    q = await _quotation(client, t)

    r = await client.post(f"/lists/{q}/scan", headers=_h(t), json={"barcode": "920001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scanned"] == 0
    assert any(f.get("code") == "920001" for f in body.get("failed", []))
    lines = (await _state(client, t, q))["line_items"]
    assert lines == [] or all(li.get("sku") != "Z0-1" for li in lines)


@pytest.mark.asyncio
async def test_draft_audit_zero_on_hand_autosavable(client):
    """A draft audit seeded with a zero-on-hand item stays autosavable: its line-page PATCH accepts the
    zero snapshot because audit lists are exempt from the positive rule. Red at merge-base: the list
    validator applies the positive rule and 422s; after the fix the audit exemption accepts it."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "AUD-0", loc=loc, qty=0, barcode="940001", sell_by="piece")

    r = await client.post("/lists/audit", headers=_h(t), json={"location_id": loc})
    assert r.status_code == 200, r.text
    audit_id = r.json()["id"]

    st = await _state(client, t, audit_id)
    lines = st.get("line_items") or []
    assert lines, "audit should seed the zero-on-hand item"
    assert any(float(li.get("quantity")) == 0.0 for li in lines)

    # Re-save the seeded page verbatim (zero snapshot included) - must be accepted.
    r2 = await client.patch(f"/lists/{audit_id}/line-page", headers=_h(t), json={
        "line_items": lines, "offset": 0, "original_count": len(lines),
        "expected_version": st["version"],
    })
    assert r2.status_code == 200, r2.text


# --- B1: patch_doc twin - document line unit resolved by exact id ----------


@pytest.mark.asyncio
async def test_patch_doc_free_text_line_ignores_matching_sku(client):
    """A document (invoice) free-text line (no item_id) whose sku matches a 0-decimal piece item is
    bound only by the finiteness gate, never that item's per-unit rule, when patched. Red at the PR
    head, where patch_doc resolves sell_by by SKU and 422s the fractional value; after the fix patch_doc
    routes through the shared bounded validator, the unlinked line resolves no sell_by, and 2.5
    persists."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "PDUP", loc=loc, qty=5, sell_by="piece")

    doc = await client.post("/docs", headers=_h(t), json={"doc_type": "invoice", "status": "draft"})
    assert doc.status_code == 200, doc.text
    doc_id = doc.json()["id"]

    line = {"sku": "PDUP", "name": "Free text PDUP", "description": "Free text PDUP",
            "quantity": 2.5, "unit_price": 4.0}
    r = await client.patch(f"/docs/{doc_id}", headers=_h(t), json={
        "fields_changed": {"line_items": {"old": [], "new": [line]}},
    })
    assert r.status_code == 200, r.text
    got = (await client.get(f"/docs/{doc_id}", headers=_h(t))).json()
    lis = got.get("line_items") or []
    assert len(lis) == 1 and float(lis[0]["quantity"]) == 2.5
