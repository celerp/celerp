# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Selling goods held on consignment.

Consigned goods are the consignor's until they are bought, so the company never carries
them as inventory. Selling them costs the sale at the lot's recorded consignment cost
against what is now owed to the consignor (Dr Cost of goods sold, Cr Consignor payable);
the revenue and the receivable are booked as for any sale. Taking the goods back, voiding
the sale or a customer return reverses exactly that, once, and the goods are held on
consignment again. Converting the consignment to a vendor bill afterwards settles what is
owed: the sold goods move from the consignor payable to accounts payable, any difference
between the bill and the recorded cost going to cost of goods sold, and the goods still
held become the company's inventory at the bill's cost.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from celerp.accounting_roles import (
    LOT_ACCOUNT_FIELD,
    POSTING_ROLES_SCHEMA,
    ROLES_KEY,
    SCHEMA_KEY,
    SCOPES_KEY,
)
from celerp.models.projections import Projection
from celerp.services.company_lock import locked_company
from celerp_accounting.models import Account
from stock_books import assert_settled
from test_cost_restatement import _state
from test_money_stock_and_contact_invariants import _account_net
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

PAYABLE = "2115"
COGS = "5100"
PURCHASED = "1130-P"
AP = "2110"
PAYABLE_ROLE = "consignor_payable"
PAYABLE_FIELD = "consignor_payable_code"


async def _consign(client, session, auth, *, qty: float = 2, unit_price: float = 5.0,
                   cost_price: float | None = None, finalize: bool = True, **doc) -> tuple[str, str]:
    """Receive ``qty`` units on a consignment; return (consignment, lot)."""
    sku = f"CS-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": "Lot", "quantity": 0, "sell_by": "piece", "status": "available"})
    assert r.status_code == 200, r.text
    template = r.json()["id"]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "consignment_in", "contact_id": "supplier:1",
        "line_items": [{"item_id": template, "sku": sku, "name": "Lot", "quantity": qty,
                        "unit_price": unit_price, "line_total": qty * unit_price}],
        "total": qty * unit_price, **doc})
    assert r.status_code == 200, r.text
    consignment = r.json()["id"]
    if finalize:
        r = await client.post(f"/docs/{consignment}/finalize", headers=auth["headers"])
        assert r.status_code == 200, r.text
    item = {"item_id": template, "sku": sku, "name": "Lot", "quantity_received": qty,
            "po_line_index": 0, "receive_as": "stock"}
    if cost_price is not None:
        item["cost_price"] = cost_price
    r = await client.post(f"/docs/{consignment}/receive", headers=auth["headers"],
                          json={"location_id": "", "received_items": [item]})
    assert r.status_code == 200, r.text
    lot = (await _state(session, auth, consignment))["received_item_ids"][0]
    return consignment, lot


async def _invoice(client, session, auth, lot: str, qty: float | None = None):
    """A finalize response for an invoice of ``qty`` (default all) of ``lot``, and its id."""
    state = await _state(session, auth, lot)
    qty = state["quantity"] if qty is None else qty
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}",
        "line_items": [{"entity_id": lot, "sku": state["sku"], "name": "Lot", "quantity": qty,
                        "unit_price": 40.0, "line_total": 40.0 * qty}],
        "total": 40.0 * qty})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    return await client.post(f"/docs/{doc}/finalize", headers=auth["headers"]), doc


async def _sell(client, session, auth, lot: str, qty: float | None = None, *, ship: bool = True) -> str:
    r, doc = await _invoice(client, session, auth, lot, qty)
    assert r.status_code == 200, r.text
    if ship:
        r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"],
                              json={"line_entity_ids": [lot]})
        assert r.status_code == 200, r.text
    return doc


async def _books(session, auth, *codes: str) -> dict[str, float]:
    return {code: round(await _account_net(session, auth["company_id"], code), 2) for code in codes}


async def _posted(session, auth) -> list[dict]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    return [r.state for r in rows if (r.state or {}).get("status") == "posted"]


async def _settled(client, session, auth) -> None:
    """Every posted entry balances, and the books carry exactly the stock lots hold."""
    for je in await _posted(session, auth):
        debit = round(sum(float(e.get("debit") or 0) for e in je["entries"]), 2)
        credit = round(sum(float(e.get("credit") or 0) for e in je["entries"]), 2)
        assert debit == credit, je
    await assert_settled(client, session, auth)


async def test_selling_consigned_goods_costs_the_sale_against_the_consignor_payable(client, session, auth):
    consignment, lot = await _consign(client, session, auth)
    assert (await _state(session, auth, lot))["cost_total"] == 10.0
    await _settled(client, session, auth)

    doc = await _sell(client, session, auth, lot, ship=False)
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -10.0, COGS: 10.0}
    fin = (await _state(session, auth, f"je:auto:{doc}:fin"))["entries"]
    assert {(e["account"], e["debit"], e["credit"]) for e in fin if e["account"] in (PAYABLE, COGS)} == {
        (COGS, 10.0, 0.0), (PAYABLE, 0.0, 10.0)}
    assert sum(e["debit"] for e in fin if e["account"] not in (PAYABLE, COGS)) == 40.0 * 2
    state = await _state(session, auth, lot)
    assert (state["consignment_flag"], state[PAYABLE_FIELD], state.get(LOT_ACCOUNT_FIELD)) == ("in", PAYABLE, None)
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -10.0, COGS: 10.0}
    assert (await _state(session, auth, lot))["status"] == "sold"
    await _settled(client, session, auth)


async def test_a_foreign_consignment_is_costed_at_its_rate(client, session, auth):
    _, lot = await _consign(client, session, auth, currency="EUR", conversion_rate=1.1)
    assert (await _state(session, auth, lot))["cost_total"] == 11.0
    await _sell(client, session, auth, lot)
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -11.0, COGS: 11.0}
    await _settled(client, session, auth)


async def test_consigned_goods_with_no_known_cost_are_never_sold_at_a_guessed_cost(client, session, auth):
    # A foreign consignment may hold goods before it has a rate, so they carry no cost.
    _, lot = await _consign(client, session, auth, currency="EUR", finalize=False)
    assert (await _state(session, auth, lot)).get("cost_total") is None
    before = len(await _posted(session, auth))
    r, doc = await _invoice(client, session, auth, lot)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "consignment.no_cost", r.text
    await session.rollback()  # the refused request's work, as the app's session drops it
    assert len(await _posted(session, auth)) == before
    assert (await _state(session, auth, doc))["status"] == "draft"
    assert PAYABLE_FIELD not in await _state(session, auth, lot)


async def test_taking_back_a_consigned_sale_reverses_it_once(client, session, auth):
    _, lot = await _consign(client, session, auth)
    doc = await _sell(client, session, auth, lot)
    r = await client.post(f"/docs/{doc}/revert-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: 0.0, COGS: 0.0}
    state = await _state(session, auth, lot)
    assert (state["status"], state["consignment_flag"]) == ("available", "in")
    entries = len(await _posted(session, auth))

    r = await client.post(f"/docs/{doc}/revert-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code >= 400
    assert len(await _posted(session, auth)) == entries
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: 0.0, COGS: 0.0}
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -10.0, COGS: 10.0}
    await _settled(client, session, auth)


@pytest.mark.parametrize("undo", ["void", "revert-to-draft"])
async def test_voiding_a_consigned_sale_reverses_it_once(client, session, auth, undo):
    _, lot = await _consign(client, session, auth)
    doc = await _sell(client, session, auth, lot, ship=False)
    r = await client.post(f"/docs/{doc}/{undo}", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: 0.0, COGS: 0.0}
    state = await _state(session, auth, lot)
    assert (state["status"], state["consignment_flag"]) == ("available", "in")
    entries = len(await _posted(session, auth))

    await client.post(f"/docs/{doc}/{undo}", headers=auth["headers"], json={})
    assert len(await _posted(session, auth)) == entries
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: 0.0, COGS: 0.0}
    await _settled(client, session, auth)

    if undo == "void":
        r = await client.post(f"/docs/{doc}/unvoid", headers=auth["headers"], json={})
        assert r.status_code == 200, r.text
        assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -10.0, COGS: 10.0}
        await _settled(client, session, auth)


async def test_a_customer_return_puts_the_goods_back_on_consignment(client, session, auth):
    consignment, lot = await _consign(client, session, auth)
    doc = await _sell(client, session, auth, lot)
    sku = (await _state(session, auth, lot))["sku"]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "credit_note", "original_doc_id": doc, "ref_id": f"CN-{uuid.uuid4().hex[:6]}",
        "line_items": [{"sku": sku, "name": "Lot", "quantity": 2, "unit_price": 40.0}], "total": 80.0})
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    assert (await client.post(f"/docs/{cn}/finalize", headers=auth["headers"])).status_code == 200
    body = {"items": [{"sku": sku, "item_id": lot, "quantity": 2}], "idempotency_key": f"ret-{uuid.uuid4()}"}
    r = await client.post(f"/docs/{cn}/receive-return", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    returned = r.json()["received_items"][0]["item_id"]
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: 0.0, COGS: 0.0}
    state = await _state(session, auth, returned)
    assert (state["consignment_flag"], state[PAYABLE_FIELD], state.get(LOT_ACCOUNT_FIELD)) == ("in", PAYABLE, None)
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{cn}/receive-return", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: 0.0, COGS: 0.0}

    # Bought afterwards, the returned goods are the company's own at the bill's cost.
    r = await client.post(f"/docs/{consignment}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    state = await _state(session, auth, returned)
    assert (state.get("consignment_flag"), state[LOT_ACCOUNT_FIELD], state["cost_total"]) == (None, PURCHASED, 10.0)
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 0.0, PURCHASED: 10.0, AP: -10.0}
    await _settled(client, session, auth)


async def test_undoing_a_customer_return_after_buying_takes_the_goods_off_at_what_they_cost(client, session, auth):
    """Recorded at 4 a unit, bought at 5: undoing the return takes the lot off inventory at
    the 10 it carries, not the 8 it came back at, so no inventory is left that no lot holds."""
    consignment, lot = await _consign(client, session, auth, cost_price=4.0)
    doc = await _sell(client, session, auth, lot)
    sku = (await _state(session, auth, lot))["sku"]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "credit_note", "original_doc_id": doc, "ref_id": f"CN-{uuid.uuid4().hex[:6]}",
        "line_items": [{"sku": sku, "name": "Lot", "quantity": 2, "unit_price": 40.0}], "total": 80.0})
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    assert (await client.post(f"/docs/{cn}/finalize", headers=auth["headers"])).status_code == 200
    r = await client.post(f"/docs/{cn}/receive-return", headers=auth["headers"], json={
        "items": [{"sku": sku, "item_id": lot, "quantity": 2}], "idempotency_key": f"ret-{uuid.uuid4()}"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{consignment}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 0.0, PURCHASED: 10.0, AP: -10.0}

    r = await client.delete(f"/docs/{cn}/receive-return", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 10.0, PURCHASED: 0.0, AP: -10.0}
    await _settled(client, session, auth)


@pytest.mark.parametrize("recorded_unit", [4.0, 5.0, 6.0])
async def test_converting_after_a_partial_sale_settles_the_sold_and_buys_the_held(
        client, session, auth, recorded_unit):
    """The bill prices each unit at 5: the sold unit's payable moves to accounts payable with
    the bill's difference from its recorded cost taken to cost of goods sold, and the unit
    still held becomes inventory at the bill's cost and sells like any other stock."""
    consignment, lot = await _consign(client, session, auth, cost_price=recorded_unit)
    assert (await _state(session, auth, lot))["cost_total"] == 2 * recorded_unit
    doc = await _sell(client, session, auth, lot, 1)
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -recorded_unit, COGS: recorded_unit}
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{consignment}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    bill = r.json()["target_doc_id"]
    assert all(not (li.get("item_id") and li.get("entity_id") and li["item_id"] != li["entity_id"])
               for li in (await _state(session, auth, bill))["line_items"])
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 5.0, PURCHASED: 5.0, AP: -10.0}
    adjustments = [je for je in await _posted(session, auth)
                   if str(je.get("memo") or "").startswith("COGS adjustment")]
    assert round(sum(float(e.get("debit") or 0) - float(e.get("credit") or 0)
                     for je in adjustments for e in je["entries"] if e["account"] == COGS), 2) == 5.0 - recorded_unit
    held = await _state(session, auth, lot)
    assert (held.get("consignment_flag"), held[LOT_ACCOUNT_FIELD], held["cost_total"], held["quantity"]) == (
        None, PURCHASED, 5.0, 1)
    await _settled(client, session, auth)

    # Converting twice is refused and books nothing more.
    entries = len(await _posted(session, auth))
    r = await client.post(f"/docs/{consignment}/convert", headers=auth["headers"])
    assert r.status_code >= 400
    assert len(await _posted(session, auth)) == entries

    await _sell(client, session, auth, lot)
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 10.0, PURCHASED: 0.0, AP: -10.0}
    await _settled(client, session, auth)
    assert doc


async def test_converting_with_goods_invoiced_but_not_shipped_reprices_that_sale(client, session, auth):
    consignment, lot = await _consign(client, session, auth, cost_price=4.0)
    doc = await _sell(client, session, auth, lot, ship=False)
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -8.0, COGS: 8.0}
    r = await client.post(f"/docs/{consignment}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 10.0, PURCHASED: 0.0, AP: -10.0}
    held = await _state(session, auth, lot)
    assert (held.get("consignment_flag"), held[LOT_ACCOUNT_FIELD], held["cost_total"]) == (None, PURCHASED, 10.0)
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 10.0, PURCHASED: 0.0, AP: -10.0}
    await _settled(client, session, auth)


async def test_sold_consigned_goods_cannot_go_back_to_the_consignor(client, session, auth):
    consignment, lot = await _consign(client, session, auth)
    doc = await _sell(client, session, auth, lot, ship=False)
    r = await client.post(f"/docs/{consignment}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "consignment.return.sold", r.text
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{consignment}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 409, r.text
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -10.0, COGS: 10.0}
    assert (await _state(session, auth, lot))["consignment_flag"] == "in"


async def test_goods_still_held_go_back_to_the_consignor(client, session, auth):
    consignment, lot = await _consign(client, session, auth)
    r = await client.post(f"/docs/{consignment}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: 0.0, COGS: 0.0}
    await _settled(client, session, auth)


async def test_after_a_return_to_the_consignor_what_was_kept_is_still_bought(client, session, auth):
    """4 received at a recorded 4 a unit and billed at 5: 1 on a sale, 2 sent back, 1 held.
    The bill buys the 2 kept and the consignor payable clears."""
    consignment, lot = await _consign(client, session, auth, qty=4, cost_price=4.0)
    doc = await _sell(client, session, auth, lot, 1, ship=False)
    r = await client.post(f"/docs/{consignment}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -4.0, COGS: 4.0}

    r = await client.post(f"/docs/{consignment}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    bill = await _state(session, auth, r.json()["target_doc_id"])
    assert [li["quantity"] for li in bill["line_items"]] == [2]
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 5.0, PURCHASED: 5.0, AP: -10.0}
    await _settled(client, session, auth)


async def test_a_consignment_returned_whole_has_nothing_to_buy(client, session, auth):
    consignment, lot = await _consign(client, session, auth)
    r = await client.post(f"/docs/{consignment}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, consignment))["status"] == "returned"
    entries = len(await _posted(session, auth))
    r = await client.post(f"/docs/{consignment}/convert", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "consignment.buy.nothing_kept", r.text
    assert len(await _posted(session, auth)) == entries


async def test_only_real_older_stock_is_told_to_choose_an_inventory_account():
    from celerp.services.account_roles import lot_account

    with pytest.raises(HTTPException) as refused:
        lot_account({"sku": "C1", "consignment_flag": "in"})
    assert refused.value.detail["message_key"] == "consignment.not_owned"
    with pytest.raises(HTTPException) as refused:
        lot_account({"sku": "O1"})
    assert refused.value.detail["message_key"] == "posting.older_stock.no_account"


async def _older_release(session, cid) -> None:
    """The company as a release before the consignor payable left it: no role, no account."""
    company = await locked_company(session, cid)
    roles = {k: v for k, v in company.settings[ROLES_KEY].items() if k != PAYABLE_ROLE}
    scopes = {k: v for k, v in company.settings[SCOPES_KEY].items() if k != PAYABLE_ROLE}
    company.settings = {**company.settings, SCHEMA_KEY: 2, ROLES_KEY: roles, SCOPES_KEY: scopes}
    await session.execute(delete(Account).where(Account.company_id == cid, Account.code == PAYABLE))
    await session.commit()


async def test_a_new_company_has_a_consignor_payable_account(session, auth):
    session.expire_all()
    account = (await session.execute(select(Account).where(
        Account.company_id == auth["company_id"], Account.code == PAYABLE))).scalar_one()
    assert (account.name, account.account_type, account.parent_code) == ("Consignor Payable", "liability", "2100")
    assert (await locked_company(session, auth["company_id"])).settings[ROLES_KEY][PAYABLE_ROLE] == PAYABLE


async def test_an_existing_company_gets_the_consignor_payable_and_sells_consigned_goods(client, session, auth):
    cid = auth["company_id"]
    _, lot = await _consign(client, session, auth)
    await _older_release(session, cid)
    await _startup(session)
    await _startup(session)
    session.expire_all()
    accounts = (await session.execute(select(Account).where(
        Account.company_id == cid, Account.code == PAYABLE))).scalars().all()
    assert [(a.name, a.account_type, a.parent_code) for a in accounts] == [("Consignor Payable", "liability", "2100")]
    settings = (await locked_company(session, cid)).settings
    assert (settings[SCHEMA_KEY], settings[ROLES_KEY][PAYABLE_ROLE]) == (POSTING_ROLES_SCHEMA, PAYABLE)
    await session.commit()

    await _sell(client, session, auth, lot)
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -10.0, COGS: 10.0}
    await _settled(client, session, auth)


async def test_a_company_without_a_consignor_payable_is_told_to_choose_one(client, session, auth):
    """A chart that already uses the seeded number for something else is never given a
    guessed account; selling consigned goods asks for the account instead."""
    cid = auth["company_id"]
    _, lot = await _consign(client, session, auth)
    await _older_release(session, cid)
    session.add(Account(company_id=cid, code=PAYABLE, name="Customer deposits", account_type="liability",
                        parent_code="2100"))
    await session.commit()
    await _startup(session)
    session.expire_all()
    assert PAYABLE_ROLE not in (await locked_company(session, cid)).settings[ROLES_KEY]
    accounts = (await session.execute(select(Account).where(
        Account.company_id == cid, Account.code == PAYABLE))).scalars().all()
    assert [a.name for a in accounts] == ["Customer deposits"]
    await session.commit()

    r, _doc = await _invoice(client, session, auth, lot)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "posting.refused", r.text
    assert await _books(session, auth, COGS) == {COGS: 0.0}
