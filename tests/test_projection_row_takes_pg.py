# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every writer that removes or replays a company's records takes them under the company's
lock, taken first, and names the company in each statement that takes its rows. The lock
checker's census is the witness: no take of a projection or ledger row outside the company
lock that the transaction then holds while it waits (tests/lock_rank.py R4)."""
from __future__ import annotations

import sys

import pytest
from sqlalchemy import select

from test_helpers import lock_every_company, register_admin

pytestmark = pytest.mark.asyncio

_CLASSES = ("row:projections", "row:ledger")


def _ungated(*paths: str):
    return sys.modules["lock_rank"].ungated_takes(_CLASSES, *paths)


async def _item(client, headers: dict, sku: str) -> str:
    r = await client.post("/items", headers=headers, json={
        "status": "available", "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_erasing_items_takes_their_rows_under_the_company_lock(client, session):
    from celerp.models.projections import Projection
    from celerp.services.item_erasure import erase_items

    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    first, second = await _item(client, h, "ERASE-1"), await _item(client, h, "ERASE-2")
    company_id = await session.scalar(select(Projection.company_id).where(Projection.entity_id == first))
    with _ungated("celerp/services/item_erasure.py") as ungated:
        await erase_items(session, company_id, [first, second])
        await session.flush()
    assert ungated == set()
    assert await session.scalar(select(Projection.entity_id).where(Projection.entity_id.in_([first, second]))) is None


async def test_a_full_rebuild_names_each_company_in_what_it_takes(client, session):
    from celerp.projections.engine import ProjectionEngine

    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    await _item(client, h, "REPLAY-1")
    await lock_every_company(session)
    with _ungated("celerp/projections/") as ungated:
        await ProjectionEngine.rebuild(session)
        await session.flush()
    assert ungated == set()
