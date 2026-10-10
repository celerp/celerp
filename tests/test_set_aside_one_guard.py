# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods a finalized invoice has costed and not shipped are guarded where every item change
is applied, so no route, import or connector can take them. Each way out of stock is
refused naming the invoice and leaves the lot as it was: an edited quantity, a CSV or batch
update, a store re-import, expiring or archiving the lot (alone or in bulk), reverting it to
draft, sending it out on memo, merging it into another lot, undoing the production receipt
that made it, or a write-off after a transfer. The hold follows the goods, not the SKU: a split part with its
own SKU, a lot renamed after the invoice, and an invoice line whose SKU differs from the lot
it took all keep it. Shipping or voiding the invoice releases the goods."""
from __future__ import annotations

import uuid

import pytest

from mfg_runs import complete, refusal, undo_receipt
from stock_books import assert_settled
from test_cost_follows_goods import _doc_number, _invoice, _ship
from test_cost_restatement import _state
from test_invoice_unshipped_books import _lot
from test_mfg_output_lineage import open_output
from test_set_aside_goods_every_exit import _qty, _refused, _write_off

pytestmark = pytest.mark.asyncio


async def _held_lot(client, auth, tag: str) -> tuple[str, str, str]:
    """A lot of 3 at 10.00 each; a finalized invoice sets aside 2 of it."""
    sku = f"G1-{tag}-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    return sku, lot, await _invoice(client, auth, [(lot, sku, 2)])


async def _unchanged(client, session, auth, lot: str, qty: float = 3, status: str = "available") -> None:
    state = await _state(session, auth, lot)
    assert (float(state["quantity"]), state["status"]) == (qty, status), state
    await assert_settled(client, session, auth)


async def _adjust(client, auth, lot: str, qty: float):
    return await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": qty})


def _child(body: dict) -> str:
    child = (body.get("child_ids") or body.get("children"))[0]
    return child if isinstance(child, str) else child.get("id") or child.get("entity_id")


# ---- every writer ------------------------------------------------------------------------

async def test_editing_the_quantity_cannot_take_set_aside_goods(client, session, auth):
    _sku, lot, inv = await _held_lot(client, auth, "PATCH")
    r = await client.patch(f"/items/{lot}", headers=auth["headers"],
                           json={"fields_changed": {"quantity": {"old": 3, "new": 0}}})
    await _refused(session, auth, r, inv, 2)
    await _unchanged(client, session, auth, lot)


async def test_a_batch_update_cannot_take_set_aside_goods(client, session, auth):
    _sku, lot, inv = await _held_lot(client, auth, "CSV")
    rec = {"entity_id": lot, "event_type": "item.patched", "data": {"quantity": 0},
           "source": "csv", "idempotency_key": f"g1-{uuid.uuid4().hex}"}
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [rec], "upsert": True})
    assert r.status_code == 200, r.text  # a batch reports each refused row and goes on
    [error] = r.json()["errors"]
    assert await _doc_number(session, auth, inv) in error["message"], error
    assert "cannot go below 2" in error["message"], error
    await _unchanged(client, session, auth, lot)


async def test_a_store_re_import_cannot_take_set_aside_goods(client, session, auth):
    from fastapi import HTTPException

    from celerp_inventory.services import update_item_from_connector
    _sku, lot, inv = await _held_lot(client, auth, "CONN")
    with pytest.raises(HTTPException) as refused:
        await update_item_from_connector(session, lot, {"quantity": 0.0}, f"conn-{uuid.uuid4().hex}",
                                         company_id=auth["company_id"])
    await session.rollback()
    assert refused.value.status_code == 409
    assert await _doc_number(session, auth, inv) in refused.value.detail, refused.value.detail
    await _unchanged(client, session, auth, lot)


async def test_expiring_a_partly_held_lot_is_refused_naming_the_invoice(client, session, auth):
    _sku, lot, inv = await _held_lot(client, auth, "EXPIRE")
    await _refused(session, auth, await client.post(f"/items/{lot}/expire", headers=auth["headers"]), inv, None)
    await _unchanged(client, session, auth, lot)


async def test_archiving_a_partly_held_lot_in_bulk_is_refused_naming_the_invoice(client, session, auth):
    _sku, lot, inv = await _held_lot(client, auth, "BULK")
    r = await client.post("/items/bulk/status", headers=auth["headers"], json={"entity_ids": [lot], "status": "archived"})
    await _refused(session, auth, r, inv, None)
    await _unchanged(client, session, auth, lot)


@pytest.mark.parametrize("status", ["expired", "sold"])
async def test_bulk_status_cannot_expire_or_sell_a_held_lot(client, session, auth, status):
    """Expiring and selling have their own actions, so a bulk status edit refuses them first."""
    _sku, lot, _inv = await _held_lot(client, auth, f"BULK-{status}")
    r = await client.post("/items/bulk/status", headers=auth["headers"], json={"entity_ids": [lot], "status": status})
    assert r.status_code == 422, r.text
    await _unchanged(client, session, auth, lot)


async def test_reverting_a_held_lot_to_draft_is_refused(client, session, auth):
    _sku, lot, _inv = await _held_lot(client, auth, "DRAFT")
    r = await client.post("/items/bulk/revert-to-draft", headers=auth["headers"], json={"entity_ids": [lot]})
    assert r.status_code >= 400, r.text
    await session.rollback()
    await _unchanged(client, session, auth, lot)


async def test_a_memo_cannot_send_out_set_aside_goods(client, session, auth):
    sku, lot, inv = await _held_lot(client, auth, "MEMO")
    h = auth["headers"]
    r = await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": [
        {"entity_id": lot, "sku": sku, "name": sku, "quantity": 3, "unit_price": 50.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [lot]})
    await _refused(session, auth, r, inv, None)
    await _unchanged(client, session, auth, lot)


async def test_undoing_the_production_receipt_of_invoiced_output_is_refused(client, session, auth):
    _made, order, lot = await open_output(client, session, auth)
    sku = (await _state(session, auth, lot))["sku"]
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    refusal(await undo_receipt(client, auth, order, lot, key="undo"), 409, "mfg.output_changed")
    await session.rollback()
    assert await _qty(session, auth, lot) == 2
    r = await complete(client, auth, order, key="done", waste_quantity=2, waste_reason="scrap")
    assert r.status_code == 200, r.text  # completion re-costs the held output, it takes nothing
    await assert_settled(client, session, auth)
    await _ship(client, auth, inv, lot)
    await assert_settled(client, session, auth)


async def test_a_held_lot_cannot_be_merged_until_the_invoice_lets_it_go(client, session, auth):
    """The invoice's line names the lot, so merging it into another lot would strand the
    goods the invoice holds: refused like expiring or archiving it. Once the invoice is
    voided the merge goes through and can be undone."""
    sku = f"G1-MRG-{uuid.uuid4().hex[:4]}"
    a, b = await _lot(client, auth, sku, 2, 20.0), await _lot(client, auth, sku, 2, 20.0)
    inv = await _invoice(client, auth, [(a, sku, 2)])
    h = auth["headers"]
    body = {"source_entity_ids": [a, b], "target_sku_from": a}

    async def merge():
        p = await client.post("/items/merge/preview", headers=h, json=body)
        assert p.status_code == 200, p.text
        return await client.post("/items/merge", headers=h, json={**body, "plan_fingerprint": p.json().get("plan_fingerprint")})

    await _refused(session, auth, await merge(), inv, None)
    assert (await client.post(f"/docs/{inv}/void", headers=h, json={})).status_code == 200
    m = await merge()
    assert m.status_code == 200, m.text
    r = await client.post(f"/items/{m.json()['id']}/undo-merge", headers=h, json={})
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


async def test_a_transferred_lot_keeps_the_hold(client, session, auth):
    _sku, lot, inv = await _held_lot(client, auth, "XFER")
    h = auth["headers"]
    r = await client.post("/companies/me/locations", headers=h, json={"name": f"Other-{uuid.uuid4().hex[:4]}", "type": "warehouse"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/items/{lot}/transfer", headers=h, json={"to_location_id": r.json()["id"]})
    assert r.status_code == 200, r.text
    await _refused(session, auth, await _adjust(client, auth, lot, 0), inv, 2)
    assert (await _adjust(client, auth, lot, 2)).status_code == 200
    await assert_settled(client, session, auth)


# ---- the hold follows the goods ---------------------------------------------------------

async def test_two_invoices_hold_until_each_ships_or_is_voided(client, session, auth):
    sku = f"G1-TWO-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 5, 50.0)
    a = await _invoice(client, auth, [(lot, sku, 2)])
    b = await _invoice(client, auth, [(lot, sku, 2)])
    r = await _adjust(client, auth, lot, 3)
    await _refused(session, auth, r, b, 4)
    assert await _doc_number(session, auth, a) in r.json()["detail"]
    assert (await _adjust(client, auth, lot, 4)).status_code == 200  # the free unit leaves
    assert (await client.post(f"/docs/{a}/void", headers=auth["headers"], json={"reason": "cancelled"})).status_code == 200
    assert (await _adjust(client, auth, lot, 2)).status_code == 200
    await _refused(session, auth, await _adjust(client, auth, lot, 1), b, 2)
    await _ship(client, auth, b, lot)
    assert (await _adjust(client, auth, lot, 0)).status_code == 200
    await assert_settled(client, session, auth)


async def test_a_split_part_with_its_own_sku_keeps_the_hold(client, session, auth):
    sku, lot, inv = await _held_lot(client, auth, "SPLIT")
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"],
                          json={"children": [{"sku": f"{sku}-C", "quantity": 3}]})
    assert r.status_code == 200, r.text
    child = _child(r.json())
    await _refused(session, auth, await _adjust(client, auth, child, 0), inv, 2)
    assert (await _adjust(client, auth, child, 2)).status_code == 200
    await assert_settled(client, session, auth)


async def test_renaming_the_lot_keeps_the_hold(client, session, auth):
    sku, lot, inv = await _held_lot(client, auth, "RENAME")
    r = await client.patch(f"/items/{lot}", headers=auth["headers"],
                           json={"fields_changed": {"sku": {"old": sku, "new": f"{sku}-NEW"}}})
    assert r.status_code == 200, r.text
    await _refused(session, auth, await _adjust(client, auth, lot, 0), inv, 2)
    await assert_settled(client, session, auth)


async def test_an_invoice_line_naming_another_sku_keeps_the_hold(client, session, auth):
    sku = f"G1-TYPO-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, f"{sku}-X", 2)])
    await _refused(session, auth, await _adjust(client, auth, lot, 0), inv, 2)
    await assert_settled(client, session, auth)


async def test_free_units_of_a_split_family_can_leave(client, session, auth):
    sku, lot, _inv = await _held_lot(client, auth, "FREE")
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 2}]})
    assert r.status_code == 200, r.text
    child = _child(r.json())
    assert (await _adjust(client, auth, child, 1)).status_code == 200
    await assert_settled(client, session, auth)
