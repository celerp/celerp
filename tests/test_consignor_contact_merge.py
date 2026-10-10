# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A consignor merged into another contact.

Once two contacts are merged they are one party, so everything owed to the one merged
away is owed to the survivor: the goods they consigned, the consignor payable already
posted for them, and the bill that settles it. Nothing stays split between the two.
"""
from __future__ import annotations

import pytest

from test_consignment_in_sale import _consign, _customer_return, _sell, _settled, _state
from test_consignor_of_record import _ap_owed
from test_consignor_payable_per_consignor import CONSIGNOR_FIELD, _consignor, _owed_each

pytestmark = pytest.mark.asyncio


async def test_a_merged_consignor_is_owed_and_billed_as_the_survivor(client, session, auth):
    """A consigns 2 recorded at 4 a unit and billed at 5; both sell, then A is merged into
    B. One comes back from the customer and the consignment is converted to a bill. What
    is owed on consignment and on the bill is B's alone, and A shows nothing on either."""
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    doc = await _sell(client, session, auth, lot, 2)
    assert await _owed_each(client, auth, a, b) == (8.0, 0.0, 0.0)

    r = await client.post("/crm/contacts/merge", headers=auth["headers"],
                          json={"target_contact_id": b, "source_contact_ids": [a]})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, lot))[CONSIGNOR_FIELD] == b
    assert (await _state(session, auth, con))["contact_id"] == b
    assert await _owed_each(client, auth, a, b) == (0.0, 8.0, 0.0)
    await _settled(client, session, auth)

    returned = await _customer_return(client, session, auth, doc, lot, 1)
    assert (await _state(session, auth, returned))[CONSIGNOR_FIELD] == b
    assert await _owed_each(client, auth, a, b) == (0.0, 4.0, 0.0)
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a, b) == (0.0, 0.0, 0.0)
    assert (await _ap_owed(client, auth, a), await _ap_owed(client, auth, b)) == (0.0, 10.0)
    await _settled(client, session, auth)
