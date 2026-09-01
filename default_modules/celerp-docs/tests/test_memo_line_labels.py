# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Per-line shipped-state labels on a memo, derived from the event log plus the
item's live status.

get_doc attaches a `shipped_label` to each line: "Returned" (shipped then
reversed), "On Memo" (shipped, still out at the customer = item memo_out),
"Sold" (shipped then invoiced/finalized = item sold), "Not shipped" (never
fulfilled for this memo). A read error in the derivation degrades to no label
(the raw status badge stands), never a 500.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@memolabel.test"
    r = await client.post("/auth/register", json={"company_name": "MemoLabel Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _item(client, h, sku) -> str:
    r = await client.post("/items", headers=h, json={"status": "available", "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _memo(client, h, item_ids: list[str]) -> str:
    line_items = [{"entity_id": i, "sku": f"L-{n}", "name": f"L-{n}", "quantity": 1, "unit_price": 10, "sell_by": "piece"}
                  for n, i in enumerate(item_ids)]
    r = await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": line_items})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _label_for(doc: dict, eid: str):
    for li in doc.get("line_items") or []:
        if (li.get("entity_id") or li.get("item_id")) == eid:
            return li.get("shipped_label")
    raise AssertionError(f"line {eid} not found")


@pytest.mark.asyncio
async def test_line_label_on_memo(client):
    """A shipped line still out at the customer (item memo_out) reads "On Memo",
    a reversed line "Returned", and a never-shipped line "Not shipped"."""
    token = await _register(client)
    h = _h(token)
    on_memo = await _item(client, h, "LB-ONMEMO")
    returned = await _item(client, h, "LB-RET")
    never = await _item(client, h, "LB-NONE")
    memo = await _memo(client, h, [on_memo, returned, never])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200

    # Ship two lines; leave the third unshipped.
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [on_memo, returned]})).status_code == 200
    # Return one of the shipped lines.
    assert (await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_entity_ids": [returned]})).status_code == 200

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert _label_for(doc, on_memo) == "On Memo"
    assert _label_for(doc, returned) == "Returned"
    assert _label_for(doc, never) == "Not shipped"


@pytest.mark.asyncio
async def test_line_label_sold(client):
    """A line shipped and then sold (item status sold) reads "Sold", distinct
    from a still-out line which reads "On Memo"."""
    token = await _register(client)
    h = _h(token)
    sold = await _item(client, h, "LB-SOLD")
    memo = await _memo(client, h, [sold])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [sold]})).status_code == 200

    # The shipped item is sold (memo->invoice finalize promotes memo_out to sold).
    r = await client.post("/items/bulk/status", headers=h, json={"entity_ids": [sold], "status": "sold"})
    assert r.status_code == 200, r.text

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert _label_for(doc, sold) == "Sold"


async def _item_sku(client, h, sku, qty=1) -> str:
    """Create an available, splittable lot for a given SKU (several lots may share a SKU)."""
    r = await client.post("/items", headers=h, json={
        "status": "available", "sku": sku, "name": sku, "quantity": qty,
        "sell_by": "piece", "allow_splitting": True})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_cross_lot_close_and_label(client):
    """A memo line fulfilled ACROSS two lots of one SKU: the bound lot A plus a
    cross-lot sibling B that fulfill drew in but that never appears in the memo's
    line_items. When A is returned but B is still out at the customer, Close must
    refuse (B is unresolved) and the line must read "On Memo" (B is still out).

    At merge-base close_doc loops only state[line_items] so B is invisible and Close
    wrongly succeeds, and _derive_shipped_labels is scoped to line_items so the line
    reads "Returned" (only A, which was reverted). The fix reads the live allocation
    set (status_doc_id==memo AND status==memo_out) for close, and broadens the label
    candidate set to every item fulfilled from this memo, rolled up per SKU."""
    token = await _register(client)
    h = _h(token)
    lot_a = await _item_sku(client, h, "XL-SPAN", qty=1)
    lot_b = await _item_sku(client, h, "XL-SPAN", qty=1)

    # One line referencing lot A, quantity 2 -> fulfill draws A (1) then spans to B (1).
    line_items = [{"entity_id": lot_a, "sku": "XL-SPAN", "name": "XL-SPAN", "quantity": 2,
                   "unit_price": 10, "sell_by": "piece"}]
    memo = (await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": line_items})).json()["id"]
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [lot_a]})).status_code == 200

    # Both lots must now be out on this memo (A in line_items, B the cross-lot sibling).
    assert (await client.get(f"/items/{lot_a}", headers=h)).json().get("status") == "memo_out"
    assert (await client.get(f"/items/{lot_b}", headers=h)).json().get("status") == "memo_out"

    # Return the bound lot A only; the sibling B stays out at the customer.
    assert (await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_entity_ids": [lot_a]})).status_code == 200
    assert (await client.get(f"/items/{lot_b}", headers=h)).json().get("status") == "memo_out"

    # Close must refuse: B is an allocation of this memo still awaiting resolution.
    r = await client.post(f"/docs/{memo}/close", headers=h, json={})
    assert r.status_code == 409, f"close must refuse while a cross-lot sibling is still out; got {r.status_code}: {r.text}"

    # The line still reads "On Memo": an allocation of this SKU is still out.
    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") != "closed", f"memo must not be closed; got {doc.get('status')}"
    assert _label_for(doc, lot_a) == "On Memo", f"line must read On Memo while B is out; got {_label_for(doc, lot_a)!r}"


@pytest.mark.asyncio
async def test_line_label_sold_survives_archive(client):
    """A line shipped and then sold reads "Sold", and STAYS "Sold" after the item is
    later archived. At merge-base _label reads the item's LIVE projection status, so
    archiving (status -> "archived") reverts the label to "On Memo". The fix reads the
    durable item.status.set sold event from history, which archiving does not erase."""
    token = await _register(client)
    h = _h(token)
    sold = await _item(client, h, "LB-ARCH")
    memo = await _memo(client, h, [sold])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [sold]})).status_code == 200
    assert (await client.post("/items/bulk/status", headers=h, json={"entity_ids": [sold], "status": "sold"})).status_code == 200

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert _label_for(doc, sold) == "Sold", f"expected Sold before archive; got {_label_for(doc, sold)!r}"

    # Archive the sold item; its live status becomes "archived".
    assert (await client.post("/items/bulk/status", headers=h, json={"entity_ids": [sold], "status": "archived"})).status_code == 200
    assert (await client.get(f"/items/{sold}", headers=h)).json().get("status") == "archived"

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert _label_for(doc, sold) == "Sold", f"label must stay Sold after archive; got {_label_for(doc, sold)!r}"


@pytest.mark.asyncio
async def test_line_label_derivation_degrades(client, monkeypatch):
    """A read error inside the label derivation leaves lines without a
    shipped_label rather than 500ing the whole doc payload."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, "LB-DEG")
    memo = await _memo(client, h, [a])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200

    from celerp_docs import routes as _routes

    async def _boom(*args, **kwargs):
        raise RuntimeError("ledger read failed")

    monkeypatch.setattr(_routes, "_derive_shipped_labels", _boom)

    r = await client.get(f"/docs/{memo}", headers=h)
    assert r.status_code == 200, r.text
    doc = r.json()
    for li in doc.get("line_items") or []:
        assert "shipped_label" not in li
