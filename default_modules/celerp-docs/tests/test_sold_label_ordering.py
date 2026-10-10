# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The "Sold" label must follow the APPLICABLE fulfillment, by ledger-id ordering.

_derive_shipped_labels reads a durable item.status.set(sold) event to promote a
line from "On Memo" to "Sold". A sold event that predates this memo's own
item.fulfilled event belongs to an EARLIER cycle (the stone was sold, returned to
stock, and re-consigned on this memo): it must NOT mark the current memo's line
"Sold". Only a sold event that FOLLOWS this memo's fulfilled event is a genuine
post-consignment sale.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@soldlabel.test"
    r = await client.post("/auth/register", json={"company_name": "SoldLabel Co", "email": addr,
                                                   "name": "A", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _item(client, h, sku) -> str:
    r = await client.post("/items", headers=h, json={
        "status": "available", "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _memo(client, h, item_ids: list[str]) -> str:
    line_items = [{"entity_id": i, "sku": f"S-{n}", "name": f"S-{n}", "quantity": 1,
                   "unit_price": 10, "sell_by": "piece"}
                  for n, i in enumerate(item_ids)]
    r = await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": line_items})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _sell_from_memo(client, h, memo: str, item_ids: list[str]) -> str:
    """The customer keeps the consigned items: the memo converts to an invoice, and finalizing
    that invoice sells what is out on the memo."""
    r = await client.post(f"/docs/{memo}/convert", headers=h)
    assert r.status_code == 200, r.text
    invoice = r.json()["target_doc_id"]
    r = await client.post(f"/docs/{invoice}/finalize", headers=h)
    assert r.status_code == 200, r.text
    for item_id in item_ids:
        assert (await client.get(f"/items/{item_id}", headers=h)).json()["status"] == "sold"
    return invoice


def _label_for(doc: dict, eid: str):
    for li in doc.get("line_items") or []:
        if (li.get("entity_id") or li.get("item_id")) == eid:
            return li.get("shipped_label")
    raise AssertionError(f"line {eid} not found")


@pytest.mark.asyncio
async def test_sold_label_requires_post_fulfillment_sale(client):
    """A sold event that PRECEDES this memo's fulfillment reads "On Memo"; a genuine
    sold event AFTER this memo's fulfillment reads "Sold".

    `stale` is sold (an earlier invoice), returned to stock, then fulfilled on
    THIS memo: its sold ledger id is lower than this memo's fulfilled id, so the line is
    still out on this memo and must read "On Memo". Converting this memo then sells
    `fresh` after its fulfillment, so it must read "Sold".

    At merge-base the sold query has no ledger-id ordering vs the applicable fulfilled
    event, so ANY sold event on the item marks the line "Sold" -> `stale` wrongly reads
    "Sold" (RED). Post-fix the sold id must exceed the applicable fulfilled id."""
    token = await _register(client)
    h = _h(token)
    stale = await _item(client, h, "SL-STALE")
    fresh = await _item(client, h, "SL-FRESH")

    # `stale`: sold on an earlier invoice, then returned to stock (available again)
    # BEFORE this memo exists. The sold ledger event now predates this memo's fulfilled event.
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"entity_id": stale, "sku": "S", "name": "S", "quantity": 1, "unit_price": 10, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    sale = r.json()["id"]
    assert (await client.post(f"/docs/{sale}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{sale}/fulfill-lines", headers=h,
                              json={"line_entity_ids": [stale]})).status_code == 200
    assert (await client.get(f"/items/{stale}", headers=h)).json()["status"] == "sold"
    # The sale is taken back on the invoice that made it, not on the memo.
    r = await client.post(f"/docs/{sale}/revert-lines", headers=h, json={"line_entity_ids": [stale]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{sale}/void", headers=h, json={"reason": "returned"})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/items/{stale}", headers=h)).json()["status"] == "available"

    memo = await _memo(client, h, [stale, fresh])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    # Fulfill both lines onto this memo (both go memo_out). This fulfilled event for
    # `stale` comes AFTER its old sold event.
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h,
                              json={"line_entity_ids": [stale, fresh]})).status_code == 200

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert _label_for(doc, stale) == "On Memo", (
        f"a sale predating this memo's fulfillment must read On Memo; got "
        f"{_label_for(doc, stale)!r}")

    # A genuine post-consignment sale (its sold id > its fulfilled id).
    await _sell_from_memo(client, h, memo, [stale, fresh])
    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert _label_for(doc, fresh) == "Sold", (
        f"a genuine post-fulfillment sale must read Sold; got {_label_for(doc, fresh)!r}")


@pytest.mark.asyncio
async def test_sale_taken_back_on_the_converted_invoice_reads_returned(client):
    """A memo line sold through its converted invoice, then taken back on that invoice, is
    back in stock: the memo row reads "Returned", not "Sold" beside an In Stock status."""
    token = await _register(client)
    h = _h(token)
    kept = await _item(client, h, "SL-KEPT")
    back = await _item(client, h, "SL-BACK")
    memo = await _memo(client, h, [kept, back])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h,
                              json={"line_entity_ids": [kept, back]})).status_code == 200
    invoice = await _sell_from_memo(client, h, memo, [kept, back])
    r = await client.post(f"/docs/{invoice}/revert-lines", headers=h, json={"line_entity_ids": [back]})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/items/{back}", headers=h)).json()["status"] == "available"

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert _label_for(doc, back) == "Returned"
    assert _label_for(doc, kept) == "Sold"
