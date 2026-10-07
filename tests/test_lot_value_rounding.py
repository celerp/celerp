"""Stock recorded on an inventory account is summed lot by lot at the cent, the way every
posting moves it, so issuing a seventh of two lots never leaves the account a cent off its stock."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from celerp.services.lot_origin import account_rooms
from mfg_runs import complete, issue, product
from stock_books import assert_books_carry_stock

pytestmark = pytest.mark.asyncio


async def _lot(client, auth):
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"R-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 7, "sell_by": "piece",
        "status": "available", "cost_total": 999.99})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.parametrize("step", ["issue", "complete"])
async def test_lots_left_with_part_cents_still_match_their_account(client, session, auth, step):
    cid = auth["company_id"]
    lots = [await _lot(client, auth), await _lot(client, auth)]
    for lot in lots:
        item = await product(client, auth, [(lot, 1.0)])
        r = await client.post(f"/manufacturing/items/{item}/build", headers=auth["headers"], json={"quantity": 1})
        assert r.status_code == 200, r.text
        order = r.json()["id"]
        r = await (issue(client, auth, order, key=f"i-{uuid.uuid4()}") if step == "issue"
                   else complete(client, auth, order, key=f"c-{uuid.uuid4()}"))
        assert r.status_code == 200, r.text
    session.expire_all()
    rooms = await account_rooms(session, cid, {"1130-OB", "1130-P"})
    assert rooms == {"1130-OB": Decimal("0.00"), "1130-P": Decimal("0.00")}, rooms
    books = await assert_books_carry_stock(session, cid)
    assert books["1130-OB"] == Decimal("1714.26")  # each lot keeps 999.99 - 142.86 = 857.13
