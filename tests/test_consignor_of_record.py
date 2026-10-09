# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A consigned lot's first sale records the consignor it is owed to on the lot, and
everything that later undoes or settles that sale is owed to that consignor, whatever the
consignment names since. Goods whose consignor is not known are not sold until one is
chosen."""
from __future__ import annotations

import pytest

from celerp.models.projections import Projection
from test_consignment_in_sale import _consign, _customer_return, _invoice, _sell, _settled, _state
from test_consignor_payable_per_consignor import CONSIGNOR_FIELD, _consignor, _owed_each

pytestmark = pytest.mark.asyncio


async def _forget_consignor(session, auth, lot: str) -> None:
    """A lot from before lots recorded their consignor."""
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": lot})
    row.state = {k: v for k, v in row.state.items() if k != CONSIGNOR_FIELD}
    await session.commit()


async def _rename_on_record(session, auth, doc: str, contact: str | None) -> None:
    """A consignment whose contact changed before the consignor was fixed by a sale."""
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc})
    row.state = {**row.state, "contact_id": contact}
    await session.commit()


async def test_the_first_sale_records_the_consignor_on_an_older_lot(client, session, auth):
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    await _forget_consignor(session, auth, lot)
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
    await _forget_consignor(session, auth, lot)
    doc = await _sell(client, session, auth, lot)
    await _rename_on_record(session, auth, con, b)
    await _customer_return(client, session, auth, doc, lot, 1)
    assert await _owed_each(client, auth, a, b) == (8.0, 0.0, 0.0)
    await _settled(client, session, auth)


async def test_goods_whose_consignor_is_not_known_are_not_sold(client, session, auth):
    a = await _consignor(client, auth, "Consignor A")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    await _forget_consignor(session, auth, lot)
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
