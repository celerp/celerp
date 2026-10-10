# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Line actions on records imported from older versions, whose lines carry no line id.

The action gives each such line its id first, through the record's own update event, so
a hold names its line exactly as it does on a newer record."""
from __future__ import annotations

import pytest

from line_actions_support import doc, drop_line_ids, h, item, line, line_ids, lot, quotation, state  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _forget_hold_lines(session, *lot_ids: str) -> None:
    """Store the holds without the line they are for, as holds made before line ids were."""
    from sqlalchemy import select

    from celerp.models.projections import Projection
    session.expire_all()
    for row in (await session.execute(select(Projection).where(Projection.entity_id.in_(lot_ids)))).scalars():
        row.state = {k: v for k, v in row.state.items() if k != "status_line_entity_id"}
    await session.commit()


async def _act(client, h, entity_id, path, eid, **extra):
    base = "lists" if entity_id.startswith("list:") else "docs"
    return await client.post(f"/{base}/{entity_id}/{path}", headers=h, json={"line_entity_ids": [eid], **extra})


@pytest.mark.parametrize("qty", [6, 3])
async def test_reserve_on_a_line_without_id_holds_for_that_line(client, session, h, qty):
    a = await lot(client, h, "LG-1", 6)
    d = await doc(client, h, [line(a, qty, sku="LG-1")])
    await drop_line_ids(session, d)
    r = await _act(client, h, d, "reserve-lines", a, new_status="reserved")
    assert r.status_code == 200, r.text
    [held] = r.json()["reserved"]
    (lid,) = await line_ids(client, h, d)
    assert lid
    st = await item(client, h, held)
    assert (st["status"], st["status_line_entity_id"], float(st["quantity"])) == ("reserved", lid, float(qty))
    assert (await state(client, h, d))["line_items"][0]["item_id"] == held


async def test_reserve_on_a_list_line_without_id_holds_for_that_line(client, session, h):
    a = await lot(client, h, "LG-2", 5)
    q = await quotation(client, h, [line(a, 5, sku="LG-2")])
    await drop_line_ids(session, q)
    r = await _act(client, h, q, "reserve-lines", a, new_status="reserved")
    assert r.status_code == 200, r.text
    (lid,) = await line_ids(client, h, q)
    assert (await item(client, h, a))["status_line_entity_id"] == lid


async def test_an_older_hold_on_a_line_without_id_ships_and_gives_back(client, session, h):
    """A hold made before lines had ids names no line; the line still ships it, and the
    id it is given on the way does not count as changing a protected line."""
    a = await lot(client, h, "LG-3", 4)
    b = await lot(client, h, "LG-4", 2)
    d = await doc(client, h, [line(a, 4, sku="LG-3"), line(b, 2, sku="LG-4")])
    l0, l1 = await line_ids(client, h, d)
    for lid in (l0, l1):
        r = await client.post(f"/docs/{d}/reserve-lines", headers=h, json={"line_ids": [lid], "new_status": "reserved"})
        assert r.status_code == 200, r.text
    await drop_line_ids(session, d)
    await _forget_hold_lines(session, a, b)
    r = await _act(client, h, d, "fulfill-lines", a)
    assert r.status_code == 200, r.text
    assert (await item(client, h, a))["status"] == "sold"
    r = await _act(client, h, d, "set-available", b)
    assert r.status_code == 200, r.text
    assert (await item(client, h, b))["status"] == "available"
    assert all(await line_ids(client, h, d))
