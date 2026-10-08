# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Landed cost leaves the clearing account the bill parked it in.

A bill's freight, insurance, duty or import VAT sits on the clearing account its
own entry recorded for that kind of cost. Receiving, returning and undoing the
goods draw on that account whatever the company's clearing account is set to by
then, and a charge the bill posted to an account of its own clears from there.
"""
from __future__ import annotations

import pytest

from celerp.models.projections import Projection
from celerp.services.account_roles import set_role
from test_cost_restatement import _state
from test_receipt_accounting import _books, _doc, _finalize, _receive, _return

_GOODS = {"po_line_index": 0, "sku": "GOODS", "name": "Goods", "quantity_received": 2}


async def _clearing_account(client, auth, code: str = "1130-FR2") -> str:
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": code, "name": f"Clearing {code}", "account_type": "asset", "parent_code": "1130"})
    assert r.status_code == 200, r.text
    return code


async def _remap_freight(session, auth, code: str) -> None:
    await set_role(session, auth["company_id"], "landed_freight", code)
    await session.commit()


async def _freight_bill(client, auth) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": "FRT", "name": "Freight", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "freight"})
    assert r.status_code == 200, r.text
    return await _doc(client, auth, "bill", [
        {"sku": "GOODS", "name": "Goods", "quantity": 2, "unit_price": 15.0},
        {"entity_id": r.json()["id"], "sku": "FRT", "name": "Freight", "quantity": 1, "unit_price": 10.0},
    ])


@pytest.mark.asyncio
async def test_goods_received_after_a_remap_capitalise_from_the_bills_clearing_account(session, client, auth):
    bill = await _freight_bill(client, auth)
    await _finalize(client, auth, bill)
    await _remap_freight(session, auth, await _clearing_account(client, auth))
    r = await _receive(client, auth, bill, _GOODS)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-P", "1130-FRT", "1130-FR2") == {
        "1130-P": 40.0, "1130-FRT": 0.0, "1130-FR2": 0.0}


@pytest.mark.asyncio
async def test_goods_sent_back_after_a_remap_return_landed_cost_to_the_bills_clearing_account(
        session, client, auth):
    bill = await _freight_bill(client, auth)
    await _finalize(client, auth, bill)
    r = await _receive(client, auth, bill, _GOODS)
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    await _remap_freight(session, auth, await _clearing_account(client, auth))
    r = await _return(client, auth, bill, parcel, 1)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-P", "1130-FRT", "1130-FR2") == {
        "1130-P": 20.0, "1130-FRT": 5.0, "1130-FR2": 0.0}

    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-P", "1130-FRT", "1130-FR2") == {
        "1130-P": 15.0, "1130-FRT": 10.0, "1130-FR2": 0.0}


async def _bill_posting_freight_to(session, client, auth, code: str) -> str:
    """A bill brought over from another system, naming the account its freight was posted to."""
    bill = await _freight_bill(client, auth)
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": bill})
    lines = [dict(li) for li in row.state["line_items"]]
    lines[1]["account_code"] = code
    row.state = {**row.state, "line_items": lines}
    await session.commit()
    await _finalize(client, auth, bill)
    return bill


@pytest.mark.asyncio
async def test_a_charge_the_bill_posted_to_an_earlier_clearing_account_clears_from_that_account(
        session, client, auth):
    earlier = await _clearing_account(client, auth, "1130-FR3")
    await _remap_freight(session, auth, earlier)
    await _remap_freight(session, auth, "1130-FRT")
    bill = await _bill_posting_freight_to(session, client, auth, earlier)
    assert await _books(session, auth, earlier, "1130-FRT") == {earlier: 10.0, "1130-FRT": 0.0}
    r = await _receive(client, auth, bill, _GOODS)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-P", earlier, "1130-FRT") == {
        "1130-P": 40.0, earlier: 0.0, "1130-FRT": 0.0}


@pytest.mark.asyncio
async def test_a_charge_the_bill_posted_to_an_expense_account_stays_there(session, client, auth):
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": "5150", "name": "Freight in", "account_type": "expense"})
    assert r.status_code == 200, r.text
    bill = await _bill_posting_freight_to(session, client, auth, "5150")
    r = await _receive(client, auth, bill, _GOODS)
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    assert (await _state(session, auth, parcel))["cost_total"] == 30.0
    assert await _books(session, auth, "1130-P", "5150", "1130-FRT") == {
        "1130-P": 30.0, "5150": 10.0, "1130-FRT": 0.0}


@pytest.mark.asyncio
async def test_a_bill_received_before_it_is_finalized_books_its_charge_where_the_receipt_drew_it(
        session, client, auth, monkeypatch):
    """An earlier release took goods in on a bill still a draft."""
    import celerp_docs.routes as docs_routes

    monkeypatch.setattr(docs_routes, "_refuse_receipt_when_not_open", lambda state: None)
    bill = await _freight_bill(client, auth)
    r = await _receive(client, auth, bill, _GOODS)
    assert r.status_code == 200, r.text
    await _remap_freight(session, auth, await _clearing_account(client, auth))
    await _finalize(client, auth, bill)
    assert await _books(session, auth, "1130-P", "1130-FRT", "1130-FR2", "2110") == {
        "1130-P": 40.0, "1130-FRT": 0.0, "1130-FR2": 0.0, "2110": -40.0}
