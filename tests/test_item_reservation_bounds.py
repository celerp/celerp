# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A reservation holds part of an available lot: never more than the lot holds, never less
than nothing, and never on a lot that is not available."""
from __future__ import annotations

import pytest

from test_cost_restatement import _item
from ui import i18n


async def _reserved(client, auth, lot) -> float:
    r = await client.get(f"/items/{lot}", headers=auth["headers"])
    return float(r.json().get("reserved_quantity") or 0)


def _move(client, auth, lot, action, quantity):
    return client.post(f"/items/{lot}/{action}", headers=auth["headers"], json={"quantity": quantity})


@pytest.mark.asyncio
@pytest.mark.parametrize("action, quantity", [("reserve", 5), ("reserve", -7), ("reserve", 0),
                                              ("unreserve", 3), ("unreserve", -1)])
async def test_a_reservation_outside_the_lot_is_refused(client, auth, action, quantity):
    lot = await _item(client, auth, 20.0, qty=2, sku="RSV-2")
    assert (await _move(client, auth, lot, "reserve", 1)).status_code == 200
    r = await _move(client, auth, lot, action, quantity)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "item.reserve_out_of_range"
    assert "RSV-2" in r.json()["detail"]["message"]
    assert await _reserved(client, auth, lot) == 1


@pytest.mark.asyncio
async def test_a_reservation_up_to_the_lot_and_back_is_accepted(client, auth):
    lot = await _item(client, auth, 20.0, qty=2)
    assert (await _move(client, auth, lot, "reserve", 2)).status_code == 200
    assert (await _move(client, auth, lot, "unreserve", 2)).status_code == 200
    assert await _reserved(client, auth, lot) == 0


@pytest.mark.asyncio
async def test_a_lot_that_is_not_available_cannot_be_reserved(client, auth):
    lot = await _item(client, auth, 20.0, qty=2, sku="RSV-EXP")
    assert (await client.post(f"/items/{lot}/expire", headers=auth["headers"])).status_code == 200
    r = await _move(client, auth, lot, "reserve", 1)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "item.reserve_not_available" and detail["params"]["status"] == "expired"
    i18n.set_lang("de")
    try:
        assert i18n.refusal_text(detail) == "RSV-EXP ist Abgelaufen: Nur verfügbarer Bestand kann reserviert werden."
    finally:
        i18n.set_lang("en")
    assert await _reserved(client, auth, lot) == 0
