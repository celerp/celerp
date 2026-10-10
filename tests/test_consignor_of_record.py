# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A consigned lot's first sale records the consignor it is owed to on the lot, and
everything that later undoes or settles that sale is owed to that consignor, whatever the
consignment names since. Until then the consignor is the consignment's contact, which can
change; from the first sale, or once the consignment is billed, it is fixed. Goods whose
consignor is not known are not sold until one is chosen."""
from __future__ import annotations

import pytest

from celerp.models.projections import Projection
from test_consignment_in_sale import AP, _consign, _customer_return, _invoice, _sell, _settled, _state
from test_consignor_payable_per_consignor import CONSIGNOR_FIELD, _consignor, _owed_each

pytestmark = pytest.mark.asyncio


async def _rename_on_record(session, auth, doc: str, contact: str | None) -> None:
    """The consignment naming another contact after a sale, as merging its contact into
    another does."""
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc})
    row.state = {**row.state, "contact_id": contact}
    await session.commit()


async def test_the_first_sale_records_the_consignor_on_the_lot(client, session, auth):
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    doc = await _sell(client, session, auth, lot)
    session.expire_all()
    assert (await _state(session, auth, lot))[CONSIGNOR_FIELD] == a
    await _rename_on_record(session, auth, con, b)
    returned = await _customer_return(client, session, auth, doc, lot, 2)
    assert await _owed_each(client, auth, a, b) == (0.0, 0.0, 0.0)
    assert (await _state(session, auth, returned))[CONSIGNOR_FIELD] == a
    await _settled(client, session, auth)


async def test_part_of_a_sale_returned_is_owed_back_to_the_consignor_it_was_sold_for(client, session, auth):
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con, lot = await _consign(client, session, auth, qty=3, cost_price=4.0, contact_id=a)
    doc = await _sell(client, session, auth, lot)
    await _rename_on_record(session, auth, con, b)
    await _customer_return(client, session, auth, doc, lot, 1)
    assert await _owed_each(client, auth, a, b) == (8.0, 0.0, 0.0)
    await _settled(client, session, auth)


async def test_goods_whose_consignor_is_not_known_are_not_sold(client, session, auth):
    a = await _consignor(client, auth, "Consignor A")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    await _rename_on_record(session, auth, con, None)
    r, _doc = await _invoice(client, session, auth, lot)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "consignment.no_consignor"
    assert "choose the consignor" in detail["message"]
    assert await _owed_each(client, auth, a) == (0.0, 0.0)
    await _rename_on_record(session, auth, con, a)
    await _sell(client, session, auth, lot)
    assert await _owed_each(client, auth, a) == (8.0, 0.0)
    await _settled(client, session, auth)


async def _recontact(client, auth, doc: str, contact: str):
    return await client.patch(f"/docs/{doc}", headers=auth["headers"],
                              json={"fields_changed": {"contact_id": {"new": contact}}})


async def _ap_owed(client, auth, contact: str) -> float:
    r = await client.get(f"/accounting/ledger/{AP}", headers=auth["headers"], params={"contact_id": contact})
    assert r.status_code == 200, r.text
    return round(sum(float(li["credit"]) - float(li["debit"]) for li in r.json()["lines"]), 2)


def _fixed(r) -> None:
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "consignment.consignor_fixed"
    assert "owed to them" in detail["message"]


async def test_the_consignor_chosen_before_any_sale_is_owed_and_billed(client, session, auth):
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    r = await _recontact(client, auth, con, b)
    assert r.status_code == 200, r.text
    await _sell(client, session, auth, lot, 1)
    assert await _owed_each(client, auth, a, b) == (0.0, 4.0, 0.0)
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a, b) == (0.0, 0.0, 0.0)
    assert (await _ap_owed(client, auth, a), await _ap_owed(client, auth, b)) == (0.0, 10.0)
    await _settled(client, session, auth)


async def test_the_consignor_is_fixed_once_goods_are_sold(client, session, auth):
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    await _sell(client, session, auth, lot, 1)
    _fixed(await _recontact(client, auth, con, b))
    session.expire_all()
    assert (await _state(session, auth, con))["contact_id"] == a
    r = await _recontact(client, auth, con, a)
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a, b) == (0.0, 0.0, 0.0)
    assert (await _ap_owed(client, auth, a), await _ap_owed(client, auth, b)) == (10.0, 0.0)
    await _settled(client, session, auth)


async def test_a_sale_taken_back_still_leaves_the_consignor_fixed(client, session, auth):
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    r, doc = await _invoice(client, session, auth, lot)
    assert r.status_code == 200, r.text
    assert (await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={})).status_code == 200
    _fixed(await _recontact(client, auth, con, b))
    await _sell(client, session, auth, lot)
    assert await _owed_each(client, auth, a, b) == (8.0, 0.0, 0.0)
    await _settled(client, session, auth)


async def test_the_consignor_is_fixed_once_the_consignment_is_billed(client, session, auth):
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    await _sell(client, session, auth, lot, 1)
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    bill = r.json().get("target_doc_id") or r.json().get("id")
    _fixed(await _recontact(client, auth, con, b))
    _fixed(await _recontact(client, auth, bill, b))
    assert (await _ap_owed(client, auth, a), await _ap_owed(client, auth, b)) == (10.0, 0.0)
    await _settled(client, session, auth)
