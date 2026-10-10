# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Changing what a lot is worth changes the books with it.

A lot on hand carries its value on the inventory account it recorded when its stock
was booked. An edit that changes that value (a corrected cost, a re-counted quantity,
a cost carried into a merge result) books the difference to exactly that account in
the same transaction: an increase against stock gains, a decrease against stock
shrinkage. Nothing waits for a report to notice the gap. An edit that would change
whether the lot is the company's own stock at all, while its value is booked, is
refused with the reason.

Every step is checked against the books (assert_books_carry_stock), and reading the
balance sheet afterwards must find nothing to book as opening inventory.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.accounting_roles import LOT_ACCOUNT_FIELD, ROLES_KEY, AccountRole
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.company_lock import locked_company
from stock_books import assert_books_carry_stock, assert_settled
from test_cost_restatement import _item, _merge, _set_cost, _state
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _receive

pytestmark = pytest.mark.asyncio

GAIN, SHRINKAGE = AccountRole.STOCK_GAIN.value, AccountRole.STOCK_SHRINKAGE.value


async def _role(session, auth, role: str) -> str:
    session.expire_all()
    return ((await session.get(Company, auth["company_id"])).settings or {})[ROLES_KEY][role]


async def _patch(client, auth, lot: str, **fields):
    return await client.patch(f"/items/{lot}", headers=auth["headers"], json={
        "fields_changed": {k: {"old": None, "new": v} for k, v in fields.items()}})


@pytest.mark.parametrize("new_cost, gain, shrinkage", [(160.0, -60.0, 0.0), (40.0, 0.0, 60.0)], ids=["up", "down"])
async def test_correcting_an_opening_lots_cost_books_the_difference(client, session, auth, new_cost, gain, shrinkage):
    lot = await _item(client, auth, 100.0)
    await assert_settled(client, session, auth)

    r = await _set_cost(client, auth, lot, new_cost)

    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    assert await _account_net(session, auth["company_id"], await _role(session, auth, GAIN)) == gain
    assert await _account_net(session, auth["company_id"], await _role(session, auth, SHRINKAGE)) == shrinkage


@pytest.mark.parametrize("new_cost", [80.0, 20.0], ids=["up", "down"])
async def test_correcting_a_purchased_lots_cost_books_the_difference_on_its_account(client, session, auth, new_cost):
    sku = f"PUR-{uuid.uuid4().hex[:6]}"
    po = await _doc(client, auth, "purchase_order", [{"sku": sku, "name": "Beads", "quantity": 4, "unit_price": 12.5}])
    r = await _receive(client, auth, po, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 4})
    assert r.status_code == 200, r.text
    [lot] = (await _state(session, auth, po))["received_item_ids"]
    purchased = await _role(session, auth, AccountRole.INVENTORY_PURCHASED.value)
    assert (await _state(session, auth, lot))[LOT_ACCOUNT_FIELD] == purchased
    await assert_settled(client, session, auth)
    before = await _account_net(session, auth["company_id"], purchased)

    r = await _set_cost(client, auth, lot, new_cost)

    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    assert await _account_net(session, auth["company_id"], purchased) == before + new_cost - 50.0


@pytest.mark.parametrize("new_a", [130.0, 70.0], ids=["up", "down"])
async def test_correcting_a_merged_source_books_the_change_to_the_merge_result(client, session, auth, new_a):
    a, b = await _item(client, auth, 100.0), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    await assert_settled(client, session, auth)

    r = await _set_cost(client, auth, a, new_a)

    assert r.status_code == 200, r.text
    assert (await _state(session, auth, c))["cost_total"] == new_a + 50.0
    await assert_settled(client, session, auth)


async def test_a_price_set_and_quantity_edits_on_a_unit_costed_lot_book_the_difference(client, session, auth):
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"UC-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 5, "sell_by": "piece",
        "status": "available", "cost_price": 10.0})
    assert r.status_code == 200, r.text
    lot = r.json()["id"]
    await assert_settled(client, session, auth)

    r = await client.post(f"/items/{lot}/price", headers=auth["headers"],
                          json={"price_type": "cost_price", "new_price": 12.0})
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    assert (await _patch(client, auth, lot, quantity=8)).status_code == 200
    await assert_settled(client, session, auth)
    r = await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 3})
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


async def test_turning_booked_stock_into_a_service_is_refused(client, session, auth):
    lot = await _item(client, auth, 100.0)
    before = await _state(session, auth, lot)

    r = await _patch(client, auth, lot, inventory_type="service")

    assert r.status_code == 422, r.text
    assert "inventory type" in r.json()["detail"]
    assert await _state(session, auth, lot) == before
    await assert_settled(client, session, auth)


async def test_a_draft_can_still_change_its_inventory_type(client, session, auth):
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"DR-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 1, "sell_by": "piece",
        "status": "draft", "cost_total": 100.0})
    assert r.status_code == 200, r.text

    assert (await _patch(client, auth, r.json()["id"], inventory_type="service")).status_code == 200
    await assert_settled(client, session, auth)


async def test_a_value_change_whose_gain_account_cannot_take_it_is_refused(client, session, auth):
    lot = await _item(client, auth, 100.0)
    company = await locked_company(session, auth["company_id"])
    roles = dict(company.settings[ROLES_KEY])
    roles.pop(GAIN)
    company.settings = {**company.settings, ROLES_KEY: roles}
    await session.commit()

    r = await _set_cost(client, auth, lot, 150.0)

    assert r.status_code == 409, r.text
    assert (await _state(session, auth, lot))["cost_total"] == 100.0
    await assert_books_carry_stock(session, auth["company_id"])
    entries = (await session.execute(select(Projection.entity_id).where(
        Projection.company_id == auth["company_id"], Projection.entity_id.like("je:auto:%value%")))).scalars().all()
    assert entries == []


async def test_a_corrected_cost_edited_back_or_returned_to_draft_leaves_the_books_matching(client, session, auth):
    """There is no Undo for a cost edit: editing it back books the reverse, and
    returning the lot to draft takes off the value it holds after the edit."""
    lot = await _item(client, auth, 100.0)
    for cost in (160.0, 100.0, 75.0):
        assert (await _set_cost(client, auth, lot, cost)).status_code == 200
        await assert_settled(client, session, auth)

    r = await client.post("/items/bulk/revert-to-draft", headers=auth["headers"], json={"entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, lot))["status"] == "draft"
    await assert_settled(client, session, auth)
    r = await client.post("/items/bulk/make-available", headers=auth["headers"], json={"entity_ids": [lot]})
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


async def test_a_production_run_whose_output_was_merged_before_completion_books_its_cost_once(
        client, session, auth):
    """The books carry the stock after every step of a run. Completing it with waste
    re-costs its lots; a lot merged meanwhile carries that re-cost into the merge
    result, booked by the completion entry and never a second time as a stock gain."""
    h = auth["headers"]
    raw = await _item(client, auth, 40.0, qty=20, sku=f"RAW-{uuid.uuid4().hex[:6]}")
    product = await _item(client, auth, 0.0, qty=0, sku=f"FG-{uuid.uuid4().hex[:6]}")
    r = await client.put(f"/manufacturing/items/{product}/recipe", headers=h, json={
        "output_qty": 2, "components": [{"item_id": raw, "quantity": 5}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    order = (await client.post(f"/manufacturing/items/{product}/build", headers=h, json={"quantity": 2})).json()["id"]
    assert (await client.post(f"/manufacturing/{order}/issue", headers=h)).status_code == 200  # 5 at 2 each
    await assert_settled(client, session, auth)
    r = await client.post(f"/manufacturing/{order}/receive", headers=h, json={"quantity": 1})
    assert r.status_code == 200, r.text
    out = r.json()["lot_item_id"]
    assert (await _state(session, auth, out))["cost_total"] == 5.0
    await assert_settled(client, session, auth)
    other = await _item(client, auth, 7.0)
    merged = await _merge(client, auth, [out, other])
    await assert_settled(client, session, auth)

    # One of the five components was wasted: 2 to cost of goods sold, each unit of output 4.
    r = await client.post(f"/manufacturing/{order}/complete", headers=h, json={"waste_quantity": 1})

    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    assert await _account_net(session, auth["company_id"], await _role(session, auth, GAIN)) == 0
    assert (await _state(session, auth, merged))["status"] != "merged"
    assert (await _state(session, auth, merged))["cost_total"] == 11.0


async def test_stock_consumed_outside_a_production_run_books_the_value_it_gave_up(client, session, auth):
    """Only a production run books its own consumption (onto work in progress); stock
    consumed by anything else leaves the books as shrinkage, never silently."""
    from celerp.events.engine import emit_event

    lot = await _item(client, auth, 100.0, qty=4)
    await assert_settled(client, session, auth)

    await emit_event(session, company_id=auth["company_id"], entity_id=lot, entity_type="item",
                     event_type="item.consumed", data={"quantity_consumed": 1}, actor_id=auth["user_id"],
                     location_id=None, source="api", idempotency_key=f"consume-{uuid.uuid4().hex}", metadata_={})
    await session.commit()

    assert (await _state(session, auth, lot))["quantity"] == 3
    await assert_settled(client, session, auth)
    assert await _account_net(session, auth["company_id"], await _role(session, auth, SHRINKAGE)) == 25.0
