# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An invoice a data migration brought over with part of its goods delivered before then
(``pre366`` company ``migrated``): the older release made each delivery a sold lot of the
line's item that named no product, so Demand Planning asked for the rest of the order under
the lot as if it were a product of its own.

After the first start of this release, a lot whose line names its product is stock of that
product, through the ledger, so a rebuild keeps it and a later start changes nothing. A lot
whose line names no product is left as it is and the company is told which, once.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

import pre366
from celerp.models.ledger import LedgerEntry
from celerp.models.notification import Notification
from test_i18n_posting_refusals import _catalog, shown_in
from test_pre366_runs import _row

pytestmark = pytest.mark.asyncio


async def _board(client, old) -> dict[str, float]:
    r = await client.get("/manufacturing/to-make", headers=old["headers"])
    assert r.status_code == 200, r.text
    return {row["item_id"]: row["demand"] for row in r.json()["items"]}


async def _notices(session, old) -> list[Notification]:

    session.expire_all()
    return list((await session.execute(select(Notification).where(
        Notification.company_id == old["company_id"], Notification.title == "Delivered goods with no product",
        Notification.priority == "high"))).scalars())


async def _events(session, old) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == old["company_id"]))).scalar()


async def test_a_lot_its_line_names_the_product_of_is_stock_of_that_product(client, session):
    old = await pre366.load(session, "migrated")
    fg, lot = old["items"]["FG"], old["items"]["LOT_FG"]
    assert "catalog_item_id" not in (await _row(session, old, lot)).state
    await pre366.last_started_on_older_release(session)

    await pre366.start()

    assert (await _row(session, old, lot)).state["catalog_item_id"] == fg
    # 5 of FG ordered, 2 delivered before the move: FG still owes 3, and no row stands for the lot.
    board = await _board(client, old)
    assert board.get(fg) == 3 and lot not in board, board

    from celerp.projections.engine import ProjectionEngine

    await ProjectionEngine.rebuild(session, company_id=old["company_id"])
    await session.commit()
    assert (await _row(session, old, lot)).state["catalog_item_id"] == fg

    events = await _events(session, old)
    await pre366.start()
    assert await _events(session, old) == events


async def test_a_lot_its_line_names_no_product_of_is_reported_not_guessed(client, session):
    """The second line orders SPL-1, units split off a component under their own SKU: the
    migration's record does not say what product they are, so none is given."""
    old = await pre366.upgraded(session, "migrated")
    lot = (await _row(session, old, old["items"]["LOT_SPL"])).state
    assert not lot.get("catalog_item_id") and not lot.get("parent_item_id"), lot
    number = (await _row(session, old, old["items"]["INVOICE"])).state["ref_id"]

    (notice,) = await _notices(session, old)
    assert number in notice.body and "SPL-1" in notice.body and "FG-4" not in notice.body, notice.body
    key = "notice.historical_lots_no_product"
    assert notice.i18n == {"title": f"{key}.title", "body": f"{key}.body",
                           "params": {"number": number, "skus": "SPL-1"}}
    de = _catalog("de")
    assert shown_in("de", notice) == {"id": str(notice.id), "title": de[f"{key}.title"],
                                      "body": de[f"{key}.body"].format(number=number, skus="SPL-1")}

    # Told once: not again on the next start, read or not.
    await pre366.start()
    notice.read = True
    await session.commit()
    await pre366.start()
    assert len(await _notices(session, old)) == 1
