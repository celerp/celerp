# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Consigned goods an invoice has set aside keep their consignor through contact merges,
either direction and chained: what is owed on consignment shows only under the surviving
contact, ties to what is billed, and clears once every consignment is converted to a bill,
with the books carrying the stock throughout."""
from __future__ import annotations

import pytest

from test_consignment_in_sale import _consign, _settled, _state
from test_consignor_of_record import _ap_owed
from test_consignor_payable_per_consignor import CONSIGNOR_FIELD, _consignor, _owed_each
from test_cost_follows_goods import _ship
from test_invoice_unshipped_books import _invoice

pytestmark = pytest.mark.asyncio


async def _merge(client, auth, target, *sources):
    r = await client.post("/crm/contacts/merge", headers=auth["headers"],
                          json={"target_contact_id": target, "source_contact_ids": list(sources)})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("direction", ["held_into_other", "other_into_held"])
async def test_set_aside_consigned_goods_survive_chained_merges(client, session, auth, direction):
    a = await _consignor(client, auth, "Consignor A")
    b = await _consignor(client, auth, "Consignor B")
    c = await _consignor(client, auth, "Consignor C")
    con_a, lot_a = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    con_b, lot_b = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=b)
    sku_a = (await _state(session, auth, lot_a))["sku"]
    inv = await _invoice(client, auth, lot_a, sku_a, 2)  # A's goods set aside, unshipped
    if direction == "held_into_other":
        await _merge(client, auth, b, a)
        survivor = b
    else:
        await _merge(client, auth, a, b)
        survivor = a
    await _settled(client, session, auth)
    await _merge(client, auth, c, survivor)  # and on into a third
    assert (await _state(session, auth, lot_a))[CONSIGNOR_FIELD] == c
    await _settled(client, session, auth)
    await _ship(client, auth, inv, lot_a)
    owed = await _owed_each(client, auth, a, b, c)
    assert owed[0] == 0.0 and owed[1] == 0.0 and owed[3] == 0.0, owed
    await _settled(client, session, auth)
    for con in (con_a, con_b):
        r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
        assert r.status_code == 200, r.text
    owed = await _owed_each(client, auth, a, b, c)
    ap = (await _ap_owed(client, auth, a), await _ap_owed(client, auth, b), await _ap_owed(client, auth, c))
    await _settled(client, session, auth)
    assert owed == (0.0, 0.0, 0.0, 0.0), owed
    assert ap[0] == 0.0 and ap[1] == 0.0 and ap[2] > 0, ap
