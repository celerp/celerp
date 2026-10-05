# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A lot's recorded inventory account is the account that holds its value.

Stock entered with no purchase behind it is carried by the opening inventory
entry, so it records the opening inventory account. Stock a receipt brings in
records the account the receipt booked it to. A part split off a lot keeps the
lot's account, recorded or not. Each inventory account's balance therefore
equals the value of the lots that record it, whatever the posting accounts are
changed to along the way.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.accounting_roles import LOT_ACCOUNT_FIELD
from celerp.models.projections import Projection
from celerp.services.account_roles import set_role
from celerp.services.lot_origin import held_value
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net
from test_posting_roles_lots import _credits, _lot, _new_inventory_account, _sell
from test_posting_roles_merge import _merged
from test_receipt_accounting import _doc, _receive

pytestmark = pytest.mark.asyncio

_FIELD = "inventory_account_code"


async def _remap(session, auth, role: str, code: str) -> None:
    await set_role(session, auth["company_id"], role, code)
    await session.commit()


async def _books_match_lots(session, auth, *accounts: str) -> dict[str, float]:
    """Each account's balance next to the value of the lots on hand that record it."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).scalars().all()
    held = dict.fromkeys(accounts, 0.0)
    for row in rows:
        s, value = row.state, held_value(row)
        if value is None or s.get(_FIELD) not in held:
            continue
        held[s[_FIELD]] = round(held[s[_FIELD]] + float(value), 2)
    books = {a: await _account_net(session, auth["company_id"], a) for a in accounts}
    assert books == held
    return books


async def _fulfil(session, client, auth, doc_id: str) -> str:
    """Ship the invoice's one line the way a user does, and return the lot it shipped."""
    doc = await _state(session, auth, doc_id)
    [line] = doc["line_items"]
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [line["item_id"]]})
    assert r.status_code == 200, r.text
    [line] = (await _state(session, auth, doc_id))["line_items"]
    return line["item_id"]


async def _revert(client, auth, doc_id: str, lot_id: str) -> None:
    r = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"], json={"line_entity_ids": [lot_id]})
    assert r.status_code == 200, r.text


# --- A part split off for a fulfilment keeps its lot's account -------------------------


async def test_a_part_fulfilled_then_returned_after_a_remap_sells_from_its_lots_account(session, client, auth):
    lot = await _lot(client, auth, 50.0, qty=10, sku="FUL")
    origin = (await _state(session, auth, lot))[_FIELD]
    new = await _new_inventory_account(client, auth)
    for role in ("inventory_purchased", "inventory_opening"):
        await _remap(session, auth, role, new)

    order = await _sell(client, auth, (lot, 3))
    part = await _fulfil(session, client, auth, order)
    assert part != lot
    assert (await _state(session, auth, part))[_FIELD] == origin
    await _revert(client, auth, order, part)
    restored = await _state(session, auth, part)
    assert (restored["status"], restored[_FIELD], restored["cost_total"]) == ("available", origin, 15.0)

    inv = await _sell(client, auth, (part, 3))
    assert _credits(await _state(session, auth, f"je:auto:{inv}:fin")) == {origin: 15.0}
    assert await _account_net(session, auth["company_id"], new) == 0.0


# --- Opening stock records the opening account and keeps it -----------------------------


async def test_opening_stock_stays_on_its_own_inventory_account_through_remaps_sales_and_merges(
        session, client, auth):
    accounts = ("1130-OB", "1130-P", "1131", "1132")
    sold = await _lot(client, auth, 100.0, sku="OPEN-SOLD")
    kept = await _lot(client, auth, 30.0, sku="OPEN-KEPT")
    assert (await _state(session, auth, sold))[_FIELD] == "1130-OB"
    assert await _books_match_lots(session, auth, *accounts) == {
        "1130-OB": 130.0, "1130-P": 0.0, "1131": 0.0, "1132": 0.0}

    await _remap(session, auth, "inventory_purchased", await _new_inventory_account(client, auth, "1131"))
    await _remap(session, auth, "inventory_opening", await _new_inventory_account(client, auth, "1132"))
    later = await _lot(client, auth, 40.0, sku="OPEN-LATER")
    assert (await _state(session, auth, later))[_FIELD] == "1132"
    assert await _books_match_lots(session, auth, *accounts) == {
        "1130-OB": 130.0, "1130-P": 0.0, "1131": 0.0, "1132": 40.0}

    inv = await _sell(client, auth, (sold, 1))
    assert _credits(await _state(session, auth, f"je:auto:{inv}:fin")) == {"1130-OB": 100.0}
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [sold]})
    assert r.status_code == 200, r.text
    assert await _books_match_lots(session, auth, *accounts) == {
        "1130-OB": 30.0, "1130-P": 0.0, "1131": 0.0, "1132": 40.0}

    merged = await _merged(client, auth, [later, kept])
    assert (await _state(session, auth, merged["id"]))[_FIELD] == "1132"
    assert await _books_match_lots(session, auth, *accounts) == {
        "1130-OB": 0.0, "1130-P": 0.0, "1131": 0.0, "1132": 70.0}


# --- Received stock records the account its receipt booked it to -----------------------


@pytest.mark.parametrize("doc_type", ["purchase_order", "bill"])
async def test_received_stock_records_the_account_its_receipt_booked(session, client, auth, doc_type):
    await _remap(session, auth, "inventory_opening", await _new_inventory_account(client, auth, "1132"))
    doc = await _doc(client, auth, doc_type, [{"sku": "RCV", "name": "Goods", "quantity": 2, "unit_price": 20.0}],
                     total=40.0)
    if doc_type == "bill":
        r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
        assert r.status_code == 200, r.text
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": "RCV", "name": "Goods",
                                           "quantity_received": 2})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, doc))["received_item_ids"]
    assert (await _state(session, auth, parcel))[_FIELD] == "1130-P"
    assert await _books_match_lots(session, auth, "1130-OB", "1130-P", "1132") == {
        "1130-OB": 0.0, "1130-P": 40.0, "1132": 0.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("unfit", ["inactive", "heading"])
async def test_a_draft_made_available_again_is_refused_while_its_account_cannot_take_it(
        session, client, auth, unfit):
    """A lot returned to draft keeps the opening account it recorded. Made available
    again it goes back there, so when that account can no longer take postings the move
    is refused, naming the account, and nothing is booked."""
    h = auth["headers"]
    r = await client.post("/items", headers=h, json={"sku": "RDA-1", "name": "Lot", "quantity": 1,
                                                     "sell_by": "piece", "cost_total": 25.0})
    lot = r.json()["id"]
    for path in ("make-available", "revert-to-draft"):
        r = await client.post(f"/items/bulk/{path}", headers=h, json={"entity_ids": [lot]})
        assert r.status_code == 200, r.text
    assert (await _state(session, auth, lot))[LOT_ACCOUNT_FIELD] == "1130-OB"
    await _remap(session, auth, "inventory_opening", await _new_inventory_account(client, auth, "1131"))
    if unfit == "inactive":
        r = await client.patch("/accounting/accounts/1130-OB", headers=h, json={"is_active": False})
    else:
        r = await client.post("/accounting/accounts", headers=h, json={
            "code": "1133", "name": "Under opening", "account_type": "asset", "parent_code": "1130-OB"})
    assert r.status_code == 200, r.text
    before = await _account_net(session, auth["company_id"], "1130-OB")

    r = await client.post("/items/bulk/make-available", headers=h, json={"entity_ids": [lot]})

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "posting.continued_account_unusable" and "1130-OB" in detail["message"]
    session.expire_all()
    assert (await _state(session, auth, lot))["status"] == "draft"
    assert await _account_net(session, auth["company_id"], "1130-OB") == before
