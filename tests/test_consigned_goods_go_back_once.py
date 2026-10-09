# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Consigned goods go back to the consignor while they are held: goods a sale shipped
have left, and goods a standing invoice holds for a sale stay for it, but neither
counts against what is still on the shelf. A voided invoice holds nothing, and once its
goods have gone back to the consignor it cannot be restored as if it still had them."""
from __future__ import annotations

import pytest

from test_consignment_in_sale import AP, COGS, PAYABLE, PURCHASED, _books, _consign, _invoice, _sell, _settled, _state
from test_consignor_payable_per_consignor import _consignor, _owed_each

pytestmark = pytest.mark.asyncio


async def _back(client, auth, con: str, lot: str, qty: float):
    return await client.post(f"/docs/{con}/return-items", headers=auth["headers"],
                             json={"items": [{"item_id": lot, "quantity_returned": qty}]})


async def _post(client, auth, path: str):
    return await client.post(path, headers=auth["headers"], json={})


async def test_what_is_left_after_a_shipped_sale_goes_back(client, session, auth):
    a = await _consignor(client, auth, "Consignor A")
    con, lot = await _consign(client, session, auth, qty=3, cost_price=4.0, contact_id=a)
    await _sell(client, session, auth, lot, 1)
    r = await _back(client, auth, con, lot, 2)
    assert r.status_code == 200, r.text
    assert float((await _state(session, auth, lot))["quantity"]) == 0
    r = await _post(client, auth, f"/docs/{con}/convert")
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a) == (0.0, 0.0)
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {PAYABLE: 0.0, COGS: 5.0, PURCHASED: 0.0, AP: -5.0}
    await _settled(client, session, auth)


async def test_goods_an_invoice_holds_and_has_not_shipped_stay(client, session, auth):
    a = await _consignor(client, auth, "Consignor A")
    con, lot = await _consign(client, session, auth, qty=3, cost_price=4.0, contact_id=a)
    await _sell(client, session, auth, lot, 1)
    r, _doc = await _invoice(client, session, auth, lot, 1)
    assert r.status_code == 200, r.text
    r = await _back(client, auth, con, lot, 2)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "consignment.return.sold"
    r = await _back(client, auth, con, lot, 1)
    assert r.status_code == 200, r.text
    await _settled(client, session, auth)


async def test_goods_on_a_voided_invoice_go_back(client, session, auth):
    a = await _consignor(client, auth, "Consignor A")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    r, doc = await _invoice(client, session, auth, lot)
    assert r.status_code == 200, r.text
    assert (await _post(client, auth, f"/docs/{doc}/void")).status_code == 200
    r = await _back(client, auth, con, lot, 2)
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a) == (0.0, 0.0)
    await _settled(client, session, auth)


async def test_an_invoice_whose_goods_went_back_cannot_be_restored(client, session, auth):
    a = await _consignor(client, auth, "Consignor A")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    r, doc = await _invoice(client, session, auth, lot)
    assert r.status_code == 200, r.text
    assert (await _post(client, auth, f"/docs/{doc}/void")).status_code == 200
    assert (await _back(client, auth, con, lot, 1)).status_code == 200
    r = await _post(client, auth, f"/docs/{doc}/unvoid")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "consignment.unvoid.returned"
    con_state = await _state(session, auth, con)
    number = con_state.get("doc_number") or con_state.get("ref_id") or con
    assert number and number in detail["message"]
    assert (await _state(session, auth, doc))["status"] == "void"
    assert await _owed_each(client, auth, a) == (0.0, 0.0)
    await _settled(client, session, auth)


async def test_an_invoice_whose_goods_are_still_held_is_restored(client, session, auth):
    a = await _consignor(client, auth, "Consignor A")
    con, lot = await _consign(client, session, auth, qty=3, cost_price=4.0, contact_id=a)
    r, doc = await _invoice(client, session, auth, lot, 2)
    assert r.status_code == 200, r.text
    assert (await _post(client, auth, f"/docs/{doc}/void")).status_code == 200
    assert (await _back(client, auth, con, lot, 1)).status_code == 200
    r = await _post(client, auth, f"/docs/{doc}/unvoid")
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a) == (8.0, 0.0)
    await _settled(client, session, auth)
