"""What a credit note, a shipment and a customer return may take, and what each one books.

A lot that never recorded its inventory account still moves the books when its quantity
changes. Only an issued credit note reduces what the invoice is owed, and undoing it puts
that back; what it settled stays settled through later payments and is not spent again. An
invoice ships what it invoiced less what was credited and already shipped; a customer return
takes back no more than the invoice shipped less what came back already, at the cost the
goods shipped at. A credit note line releases the goods of the invoice line it credits, by
the line's item or its SKU. An invoice ships only the goods it holds itself or that no
invoice from before cost snapshots holds."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.accounting_roles import VALUED_FROM_KEY
from celerp.models.ledger import LedgerEntry
from celerp.services.lot_origin import LOT_ACCOUNT_FIELD
from stock_books import assert_settled
from test_cost_follows_goods import _doc_number, _invoice, _ship
from test_cost_restatement import _state
from test_invoice_unshipped_books import _lot
from test_quantity_cost_invariant import _po, _receive
from test_set_aside_older_paths import _held, _net, _strip_snapshot, _unrecorded

pytestmark = pytest.mark.asyncio


async def _cn(client, auth, invoice: str, lines: list[dict], *, finalize: bool = True) -> str:
    """A credit note on ``invoice`` for ``lines`` ({sku, qty, lot?}), at 40 a unit."""
    items = [{"name": "Lot", "quantity": li["qty"], "unit_price": 40.0, "line_total": 40.0 * li["qty"],
              "sell_by": "piece", **({"sku": li["sku"]} if li.get("sku") else {}),
              **({"entity_id": li["lot"]} if li.get("lot") else {})} for li in lines]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "credit_note", "original_doc_id": invoice, "ref_id": f"CN-{uuid.uuid4().hex[:6]}",
        "line_items": items, "total": sum(i["line_total"] for i in items)})
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    if finalize:
        f = await client.post(f"/docs/{cn}/finalize", headers=auth["headers"], json={})
        assert f.status_code == 200, f.text
    return cn


async def _outstanding(session, auth, doc: str) -> float:
    session.expire_all()
    return float((await _state(session, auth, doc))["amount_outstanding"])


async def _return(client, auth, cn: str, sku: str, qty: float, lot: str | None = None):
    return await client.post(f"/docs/{cn}/receive-return", headers=auth["headers"], json={
        "items": [{"sku": sku, "quantity": qty, **({"item_id": lot} if lot else {})}],
        "idempotency_key": f"ret-{uuid.uuid4()}"})


# A lot with no recorded inventory account


async def test_adjusting_a_lot_with_no_recorded_account_moves_its_value(client, session, auth):
    sku = f"UNR-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    await _unrecorded(session, auth, lot)
    assert await _net(session, auth, "1130-OB") == 30.0
    r = await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 1})
    assert r.status_code == 200, r.text
    assert await _net(session, auth, "1130-OB") == 10.0
    assert await _net(session, auth, "6970") == 20.0
    # The account it was booked to is recorded on the lot from then on.
    assert (await _state(session, auth, lot)).get(LOT_ACCOUNT_FIELD) == "1130-OB"
    r = await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 0})
    assert r.status_code == 200, r.text
    assert await _net(session, auth, "1130-OB") == 0.0
    await assert_settled(client, session, auth)


# Only an issued credit note reduces what the invoice is owed


async def test_a_draft_credit_note_leaves_the_invoice_owed_in_full(client, session, auth):
    sku = f"CNO-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    cn = await _cn(client, auth, inv, [{"sku": sku, "lot": lot, "qty": 1}], finalize=False)
    assert await _outstanding(session, auth, inv) == 80.0
    assert (await client.post(f"/docs/{cn}/finalize", headers=auth["headers"], json={})).status_code == 200
    assert await _outstanding(session, auth, inv) == 40.0
    assert (await client.post(f"/docs/{cn}/void", headers=auth["headers"], json={})).status_code == 200
    assert await _outstanding(session, auth, inv) == 80.0
    assert (await client.post(f"/docs/{cn}/unvoid", headers=auth["headers"], json={})).status_code == 200
    assert await _outstanding(session, auth, inv) == 40.0
    assert (await client.post(f"/docs/{cn}/revert-to-draft", headers=auth["headers"], json={})).status_code == 200
    assert await _outstanding(session, auth, inv) == 80.0
    assert (await client.post(f"/docs/{cn}/finalize", headers=auth["headers"], json={})).status_code == 200
    assert await _outstanding(session, auth, inv) == 40.0
    await assert_settled(client, session, auth)


async def test_a_credit_note_voided_as_a_draft_changes_nothing_owed(client, session, auth):
    sku = f"CND-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    cn = await _cn(client, auth, inv, [{"sku": sku, "lot": lot, "qty": 1}], finalize=False)
    assert (await client.post(f"/docs/{cn}/void", headers=auth["headers"], json={})).status_code == 200
    assert await _outstanding(session, auth, inv) == 80.0
    assert (await client.post(f"/docs/{cn}/unvoid", headers=auth["headers"], json={})).status_code == 200
    assert await _outstanding(session, auth, inv) == 80.0


async def _pay(client, auth, doc: str, amount: float):
    return await client.post(f"/docs/{doc}/payment", headers=auth["headers"], json={
        "amount": amount, "method": "cash", "payment_date": "2026-10-09", "bank_account": "1111"})


async def test_a_payment_after_a_credit_note_owes_what_the_books_owe(client, session, auth):
    sku = f"CNP-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    await _cn(client, auth, inv, [{"sku": sku, "lot": lot, "qty": 1}])
    assert await _net(session, auth, "1120") == 40.0
    r = await _pay(client, auth, inv, 40.0)
    assert r.status_code == 200, r.text
    assert await _net(session, auth, "1120") == 0.0
    assert await _outstanding(session, auth, inv) == 0.0
    assert (await _state(session, auth, inv))["status"] == "paid"
    await assert_settled(client, session, auth)


async def test_a_credit_note_spent_on_its_invoice_is_not_spent_again(client, session, auth):
    sku = f"CNS-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    other = await _invoice(client, auth, [(lot, sku, 1)])
    assert (await _pay(client, auth, inv, 60.0)).status_code == 200
    cn = await _cn(client, auth, inv, [{"sku": sku, "lot": lot, "qty": 1}])
    # 20 of the 40 clears what the invoice still owed; 20 is left as credit, as in the books.
    assert await _outstanding(session, auth, inv) == 0.0
    assert await _outstanding(session, auth, cn) == 20.0
    r = await client.post(f"/docs/{cn}/apply-to-invoice", headers=auth["headers"],
                          json={"target_doc_id": other, "amount": 40.0})
    assert r.status_code == 409, r.text
    await session.rollback()
    r = await client.post(f"/docs/{cn}/apply-to-invoice", headers=auth["headers"],
                          json={"target_doc_id": other, "amount": 20.0})
    assert r.status_code == 200, r.text
    assert await _outstanding(session, auth, other) == 20.0
    assert await _outstanding(session, auth, cn) == 0.0
    assert await _net(session, auth, "1120") == 20.0
    await assert_settled(client, session, auth)


# Shipping what was not credited


async def test_an_invoice_ships_only_what_was_not_credited(client, session, auth):
    sku = f"SHC-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    await _cn(client, auth, inv, [{"sku": sku, "lot": lot, "qty": 1}])
    out = await _ship(client, auth, inv, lot)
    shipped = out["fulfilled"]
    assert len(shipped) == 1 and float((await _state(session, auth, shipped[0]))["quantity"]) == 1.0
    assert float((await _state(session, auth, lot))["quantity"]) == 2.0
    assert await _net(session, auth, "5100") == 10.0
    await assert_settled(client, session, auth)


async def test_a_line_credited_in_full_cannot_ship(client, session, auth):
    sku = f"SHF-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    await _cn(client, auth, inv, [{"sku": sku, "lot": lot, "qty": 2}])
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 422, r.text
    await session.rollback()
    detail = str(r.json()["detail"])
    assert "invoiced 2" in detail and "credited 2" in detail and "shipped 0" in detail, detail
    assert float((await _state(session, auth, lot))["quantity"]) == 3.0
    assert await _net(session, auth, "5100") == 0.0


# Customer returns


async def test_goods_the_invoice_never_shipped_cannot_come_back(client, session, auth):
    sku = f"REN-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 1)])
    cn = await _cn(client, auth, inv, [{"sku": sku, "lot": lot, "qty": 1}])
    r = await _return(client, auth, cn, sku, 1, lot)
    assert r.status_code == 422, r.text
    await session.rollback()
    detail = str(r.json()["detail"])
    assert "shipped 0" in detail and "returned 0" in detail, detail
    assert float((await _state(session, auth, lot))["quantity"]) == 3.0
    await assert_settled(client, session, auth)


async def test_a_return_comes_back_at_what_shipped_and_no_more_than_shipped(client, session, auth):
    sku = f"RET-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    # Raised before shipping, so its line names the lot the goods were carved from.
    cn = await _cn(client, auth, inv, [{"sku": sku, "lot": lot, "qty": 2}], finalize=False)
    shipped = (await _ship(client, auth, inv, lot))["fulfilled"][0]
    # The lot left behind is valued differently since; the return comes back at what shipped.
    po = await _po(client, auth, [{"item_id": lot, "name": "Lot", "quantity": 1, "unit_price": 40.0}])
    r = await _receive(client, auth, po, lot, 1)
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, lot))["cost_total"] == 50.0
    assert (await client.post(f"/docs/{cn}/finalize", headers=auth["headers"], json={})).status_code == 200
    r = await _return(client, auth, cn, sku, 1, lot)
    assert r.status_code == 200, r.text
    back = r.json()["received_items"][0]
    assert back["cost_price"] == 10.0, back
    made = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == back["item_id"],
        LedgerEntry.event_type == "item.created"))).scalars().one()
    assert (made.metadata_ or {}).get(VALUED_FROM_KEY) == shipped, made.metadata_
    r = await _return(client, auth, cn, sku, 1)
    assert r.status_code == 200, r.text
    assert r.json()["received_items"][0]["cost_price"] == 10.0, r.text
    r = await _return(client, auth, cn, sku, 1)
    assert r.status_code == 422, r.text
    await session.rollback()
    detail = str(r.json()["detail"])
    assert "shipped 2" in detail and "returned 2" in detail, detail
    await assert_settled(client, session, auth)


# Credit note lines that name no lot


async def test_a_credit_note_line_without_a_lot_releases_the_goods_of_its_sku(client, session, auth):
    sku = f"CNS-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    number = await _doc_number(session, auth, inv)
    await _cn(client, auth, inv, [{"sku": sku, "qty": 1}])
    assert await _held(session, auth, lot) == {number: 1.0}
    await assert_settled(client, session, auth)


async def test_a_credit_note_line_for_a_service_releases_nothing(client, session, auth):
    sku = f"CNV-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    number = await _doc_number(session, auth, inv)
    await _cn(client, auth, inv, [{"qty": 1}])
    assert await _held(session, auth, lot) == {number: 2.0}


# A shipping invoice and goods an older invoice holds


async def test_an_invoice_cannot_take_goods_an_older_invoice_without_a_cost_record_holds(client, session, auth):
    """The older invoice holds all 3 with no cost record, so there is no cost to move with
    the goods: a newer invoice for them is refused at finalize, naming the older one, and
    nothing leaves stock."""
    sku = f"SNP-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 3, 30.0)
    older = await _invoice(client, auth, [(lot, sku, 3)])
    await _strip_snapshot(session, auth, older)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "total": 80.0, "line_items": [
            {"entity_id": lot, "sku": sku, "name": "Lot", "quantity": 2, "unit_price": 40.0, "line_total": 80.0}]})
    assert r.status_code == 200, r.text
    newer = r.json()["id"]
    f = await client.post(f"/docs/{newer}/finalize", headers=auth["headers"])
    assert f.status_code == 409, f.text
    assert f.json()["detail"]["message_key"] == "lines.lot_already_invoiced", f.text
    assert await _doc_number(session, auth, older) in f.json()["detail"]["message"]
    assert float((await _state(session, auth, lot))["quantity"]) == 3.0
    assert await _held(session, auth, lot) == {await _doc_number(session, auth, older): 3.0}


async def test_an_invoice_with_a_snapshot_ships_goods_no_older_invoice_holds(client, session, auth):
    sku = f"SNF-{uuid.uuid4().hex[:4]}"
    lot = await _lot(client, auth, sku, 5, 50.0)
    older = await _invoice(client, auth, [(lot, sku, 3)])
    await _strip_snapshot(session, auth, older)
    newer = await _invoice(client, auth, [(lot, sku, 2)])
    await _ship(client, auth, newer, lot)
    assert await _held(session, auth, lot) == {await _doc_number(session, auth, older): 3.0}
    assert await _net(session, auth, "5100", f"je:auto:{newer}:") == 20.0
