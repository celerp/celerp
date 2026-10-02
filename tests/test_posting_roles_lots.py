# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every inventory lot keeps the inventory account its value was booked into.

A lot entered by hand takes the company's current opening inventory account, and
stock received or produced takes the current purchased inventory account; selling,
fulfilling, returning or splitting a lot moves its value on that same account whatever the
account is set to by then. A lot from before lots recorded their account
refuses to move its cost until its account is proven or chosen, rather than guess.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.account_roles import set_role
from celerp.services.company_lock import locked_company
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net

_FIELD = "inventory_account_code"


async def _new_inventory_account(client, auth, code: str = "1131") -> str:
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": code, "name": f"Stock {code}", "account_type": "asset", "parent_code": "1130"})
    assert r.status_code == 200, r.text
    return code


async def _remap(session, auth, code: str, role: str = "inventory_opening") -> None:
    await set_role(session, auth["company_id"], role, code)
    await session.commit()


async def _lot(client, auth, cost: float, qty: float = 1, sku: str | None = None) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku or f"LOT-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": qty,
        "sell_by": "piece", "status": "available", "cost_total": cost})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _sell(client, auth, *lots: tuple[str, float]) -> str:
    lines = []
    for item_id, qty in lots:
        lines.append({"entity_id": item_id, "name": "Lot", "quantity": qty, "unit_price": 50.0,
                      "sell_by": "piece"})
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": lines, "total": 50.0 * sum(q for _, q in lots)})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc


async def _forget_origin(session, auth, item_id: str) -> None:
    """Make a lot look like one created before lots recorded their account."""
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": item_id},
                            populate_existing=True)
    row.state = {k: v for k, v in row.state.items() if k != _FIELD}
    await session.commit()


def _credits(je: dict) -> dict[str, float]:
    """The inventory credits of an entry, by account."""
    return {e["account"]: e["credit"] for e in je["entries"]
            if e.get("credit") and e["account"].startswith("113")}


@pytest.mark.asyncio
async def test_a_new_lot_records_the_inventory_account_it_is_booked_into(session, client, auth):
    first = await _lot(client, auth, 10.0)
    await _remap(session, auth, await _new_inventory_account(client, auth))
    second = await _lot(client, auth, 10.0)
    assert (await _state(session, auth, first))[_FIELD] == "1130-OB"
    assert (await _state(session, auth, second))[_FIELD] == "1131"


@pytest.mark.asyncio
async def test_a_sale_relieves_each_lot_on_the_account_it_was_booked_into(session, client, auth):
    old = await _lot(client, auth, 30.0)
    await _remap(session, auth, await _new_inventory_account(client, auth))
    new = await _lot(client, auth, 12.0)
    inv = await _sell(client, auth, (old, 1), (new, 1))
    je = await _state(session, auth, f"je:auto:{inv}:fin")
    assert _credits(je)["1130-OB"] == 30.0 and _credits(je)["1131"] == 12.0
    roles = {e["account"]: e.get("account_roles") for e in je["entries"]}
    assert roles["1130-OB"] == ["inventory_opening"] and roles["1131"] == ["inventory_opening"]
    assert {e["account"]: e["debit"] for e in je["entries"] if e.get("debit")}["5100"] == 42.0


@pytest.mark.asyncio
async def test_fulfilling_after_a_remap_moves_nothing_onto_the_new_account(session, client, auth):
    lot = await _lot(client, auth, 30.0)
    inv = await _sell(client, auth, (lot, 1))
    await _remap(session, auth, await _new_inventory_account(client, auth))
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    cid = auth["company_id"]
    assert await _account_net(session, cid, "1131") == 0.0
    assert await _account_net(session, cid, "5100") == 30.0


@pytest.mark.asyncio
async def test_a_split_lot_keeps_its_account_in_every_part(session, client, auth):
    lot = await _lot(client, auth, 20.0, qty=2)
    await _remap(session, auth, await _new_inventory_account(client, auth))
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).scalars().all()
    parts = [p.state for p in rows if p.entity_id == lot or p.state.get("split_from") == lot
             or p.state.get("parent_id") == lot]
    assert len(parts) >= 2
    assert {s.get(_FIELD) for s in parts} == {"1130-OB"}


@pytest.mark.asyncio
async def test_an_older_lot_with_no_provable_account_refuses_to_move_its_cost(session, client, auth):
    lot = await _lot(client, auth, 30.0)
    await _forget_origin(session, auth, lot)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "total": 50.0,
        "line_items": [{"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 50.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert "no recorded inventory account" in r.json()["detail"]
    assert r.headers["X-Celerp-Fix"] == "/settings/accounting?tab=posting-accounts"


@pytest.mark.asyncio
async def test_a_lot_account_is_not_editable(session, client, auth):
    lot = await _lot(client, auth, 30.0)
    r = await client.patch(f"/items/{lot}", headers=auth["headers"], json={
        "fields_changed": {_FIELD: {"old": "1130-OB", "new": "1131"}}})
    assert r.status_code == 422, r.text
    assert (await _state(session, auth, lot))[_FIELD] == "1130-OB"


@pytest.mark.asyncio
async def test_a_company_without_posting_accounts_records_no_lot_account(session, client, auth):
    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if not k.startswith("posting_")}
    await session.commit()
    lot = await _lot(client, auth, 0.0)
    assert _FIELD not in await _state(session, auth, lot)


# --- Receipts, returns, audits and manufacturing keep each lot's account ---------------

from test_receipt_accounting import _doc, _finalize, _receive, _return  # noqa: E402


def _debits(je: dict) -> dict[str, float]:
    return {e["account"]: e["debit"] for e in je["entries"] if e.get("debit")}


async def _books(session, auth, *codes: str) -> dict[str, float]:
    return {c: await _account_net(session, auth["company_id"], c) for c in codes}


@pytest.mark.asyncio
async def test_a_top_up_after_a_remap_adds_to_the_lot_on_its_own_account(session, client, auth):
    lot = await _lot(client, auth, 100.0, qty=10)
    await _remap(session, auth, await _new_inventory_account(client, auth))
    po = await _doc(client, auth, "purchase_order",
                    [{"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0}])
    r = await _receive(client, auth, po, {"po_line_index": 0, "item_id": lot, "quantity_received": 5})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-OB", "1131") == {"1130-OB": 70.0, "1131": 0.0}
    # Billing the order later moves nothing between inventory accounts.
    await _finalize(client, auth, po)
    assert await _books(session, auth, "1130-OB", "1131", "2110") == {"1130-OB": 70.0, "1131": 0.0, "2110": -70.0}


@pytest.mark.asyncio
async def test_goods_sent_back_after_a_remap_leave_the_account_they_came_in_on(session, client, auth):
    lot = await _lot(client, auth, 100.0, qty=10)
    po = await _doc(client, auth, "purchase_order",
                    [{"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0}])
    r = await _receive(client, auth, po, {"po_line_index": 0, "item_id": lot, "quantity_received": 5})
    assert r.status_code == 200, r.text
    await _remap(session, auth, await _new_inventory_account(client, auth))
    r = await _return(client, auth, po, lot, 5)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-OB", "1131") == {"1130-OB": 0.0, "1131": 0.0}


@pytest.mark.asyncio
async def test_landed_cost_sent_back_after_a_remap_leaves_the_lot_account(session, client, auth):
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": "FRT", "name": "Freight", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "freight"})
    assert r.status_code == 200, r.text
    bill = await _doc(client, auth, "bill", [
        {"sku": "GOODS", "name": "Goods", "quantity": 2, "unit_price": 15.0},
        {"entity_id": r.json()["id"], "sku": "FRT", "name": "Freight", "quantity": 1, "unit_price": 10.0},
    ])
    await _finalize(client, auth, bill)
    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": "GOODS", "name": "Goods", "quantity_received": 2})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    await _remap(session, auth, await _new_inventory_account(client, auth), "inventory_purchased")
    r = await _return(client, auth, bill, parcel, 1)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-P", "1131", "1130-FRT") == {
        "1130-P": 20.0, "1131": 0.0, "1130-FRT": 5.0}


@pytest.mark.asyncio
async def test_an_audit_after_a_remap_adjusts_each_lot_on_its_own_account(session, client, auth):
    h = auth["headers"]
    r = await client.post("/companies/me/locations", headers=h, json={"name": "Counted", "type": "warehouse"})
    assert r.status_code == 200, r.text
    loc = r.json()["id"]

    async def lot(cost: float, qty: float) -> str:
        r = await client.post("/items", headers=h, json={
            "sku": f"AUD-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": qty, "sell_by": "piece",
            "status": "available", "cost_total": cost, "location_id": loc})
        assert r.status_code == 200, r.text
        return r.json()["id"]

    old = await lot(100.0, 10)
    await _remap(session, auth, await _new_inventory_account(client, auth))
    new = await lot(50.0, 5)
    audit = (await client.post("/lists/audit", headers=h, json={"location_id": loc})).json()["id"]
    assert (await client.post(f"/lists/{audit}/finalize", headers=h)).status_code == 200
    for item_id, counted in ((old, 8), (new, 6)):
        r = await client.patch(f"/lists/{audit}/line/{item_id}", headers=h, json={"counted_qty": counted})
        assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{audit}/adjust", headers=h)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-OB", "1131") == {"1130-OB": -20.0, "1131": 10.0}


@pytest.mark.asyncio
async def test_manufacturing_relieves_inputs_on_their_account_and_books_output_where_the_lot_records(
        session, client, auth):
    raw = await _lot(client, auth, 40.0, qty=20, sku=f"RAW-{uuid.uuid4().hex[:6]}")
    product = await _lot(client, auth, 0.0, qty=0, sku=f"FG-{uuid.uuid4().hex[:6]}")
    h = auth["headers"]
    r = await client.put(f"/manufacturing/items/{product}/recipe", headers=h, json={
        "output_qty": 2, "components": [{"item_id": raw, "quantity": 5}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    await _remap(session, auth, await _new_inventory_account(client, auth), "inventory_purchased")
    order = (await client.post(f"/manufacturing/items/{product}/build", headers=h, json={"quantity": 2})).json()["id"]
    assert (await client.post(f"/manufacturing/{order}/issue", headers=h)).status_code == 200
    r = await client.post(f"/manufacturing/{order}/complete", headers=h, json={})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-OB", "1131") == {"1130-OB": -10.0, "1131": 10.0}
    run = await _state(session, auth, order)
    for out in run.get("received_lots") or []:
        assert (await _state(session, auth, out))[_FIELD] == "1131"


@pytest.mark.asyncio
async def test_an_older_lot_waits_for_its_account_and_only_one_that_holds_it_can_be_chosen(session, client, auth):
    lot = await _lot(client, auth, 30.0, sku="OLD-2")
    await _forget_origin(session, auth, lot)
    await _remap(session, auth, await _new_inventory_account(client, auth), "inventory_purchased")
    # Viewing the balance sheet posts the opening inventory entry, which carries it on 1130-OB.
    assert (await client.get("/accounting/balance-sheet", headers=auth["headers"])).status_code == 200
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "total": 50.0,
        "line_items": [{"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 50.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    r = await client.post(f"/docs/{inv}/finalize", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert "OLD-2 has no recorded inventory account" in r.json()["detail"]
    assert r.headers["X-Celerp-Fix"] == "/settings/accounting?tab=posting-accounts"
    await session.rollback()  # the refused request's work ends with it, as its own session would

    r = await client.post("/admin/doctor?checks=posting_origins", headers=auth["headers"])
    (finding,) = r.json()["results"][0]["details"]
    assert (finding["kind"], finding["entity_id"]) == ("lot_origin", lot)

    for code in ("1131", "1130-P"):  # today's purchased account, and one that held purchases, hold none of it
        r = await client.put(f"/accounting/posting-accounts/older-stock/{lot}", headers=auth["headers"],
                             json={"code": code})
        assert r.status_code == 422, r.text
        assert "does not hold" in r.json()["detail"]
    r = await client.put(f"/accounting/posting-accounts/older-stock/{lot}", headers=auth["headers"],
                         json={"code": "1130-OB"})
    assert r.status_code == 200, r.text
    assert r.json()["older_stock"]["lots"] == []
    r = await client.post(f"/docs/{inv}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert _credits(await _state(session, auth, f"je:auto:{inv}:fin")) == {"1130-OB": 30.0}
