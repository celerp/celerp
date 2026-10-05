# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A start hook that fails keeps the work of every other hook, and holds the records back.

Each module's start hook runs in its own savepoint, so one that fails rolls back only its
own changes: the stock Accounting placed stays placed. Until a start runs every hook, the
records are not current and changes to them are refused, and the next start tries again.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

import pre366
from celerp.accounting_roles import INVENTORY_ORIGIN_KEY, LOT_ACCOUNT_FIELD
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.notification import Notification
from celerp.models.projections import Projection
from stock_books import assert_books_carry_stock, assert_wip_carried
from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)

pytestmark = pytest.mark.asyncio


async def _marked(session, cid) -> bool:
    session.expire_all()
    return INVENTORY_ORIGIN_KEY in ((await session.get(Company, cid)).settings or {})


async def _lot_account(session, cid, item_id):
    session.expire_all()
    return (await session.get(Projection, {"company_id": cid, "entity_id": item_id})).state.get(LOT_ACCOUNT_FIELD)


async def test_a_failing_start_hook_keeps_the_other_hooks_work_and_holds_the_records_back(
        client, session, monkeypatch):
    import celerp_docs.historical_lots as hl
    from celerp.main import app

    a = await pre366.load(session, "main")
    b = await pre366.load(session, "main")
    await pre366.last_started_on_older_release(session)
    real = hl.link_historical_lots

    async def failing(s, company_id, lots=None):
        if company_id == b["company_id"]:
            await s.execute(text("SELECT 1/0"))  # fails in the database, as a real fault would
        return await real(s, company_id, lots)

    monkeypatch.setattr(hl, "link_historical_lots", failing)
    await pre366.start()

    assert app.state.data_current is False
    assert await _marked(session, a["company_id"]) and await _marked(session, b["company_id"])
    assert await _lot_account(session, a["company_id"], a["items"]["A"])
    titles = (await session.execute(select(Notification.title).where(
        Notification.company_id == a["company_id"]))).scalars().all()
    assert "Stored records could not be brought up to date" in titles
    r = await client.post(f"/items/{a['items']['A']}/adjust", json={"new_qty": 1}, headers=a["headers"])
    assert r.status_code == 503, r.text

    monkeypatch.setattr(hl, "link_historical_lots", real)
    await pre366.start()
    assert app.state.data_current is True
    for old in (a, b):
        await assert_books_carry_stock(session, old["company_id"])
        await assert_wip_carried(session, old["company_id"])


async def test_the_doctor_reports_but_does_not_repair_while_the_records_are_held_back(
        client, session, auth, monkeypatch):
    from celerp.main import app

    async def events() -> int:
        return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == auth["company_id"]))

    monkeypatch.setattr(app.state, "data_current", False, raising=False)
    before = await events()
    r = await client.post("/admin/doctor?fix=true", headers=auth["headers"])
    assert r.status_code == 503, r.text
    assert "notification bell" in r.json()["detail"]
    session.expire_all()
    assert await events() == before
    r = await client.post("/admin/doctor", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "dry-run"
