# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A migrated bill whose goods the migration already put in stock cannot be received again.

The migration brings a purchase invoice's stock in as an inventory position, so the
bill arrives already received: receiving it again is refused before anything moves,
and stock and books stay exactly as migrated.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from migration_support import OWNER_EMAIL, auth, finalize_run, maker, real_client, real_engine  # noqa: F401
from test_migration_e2e import migrate

pytestmark = pytest.mark.asyncio


async def _stock_and_books(engine, company_id) -> tuple:
    from celerp.models.projections import Projection

    async with maker(engine)() as s:
        rows = (await s.execute(select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type.in_(("item", "journal_entry"))))).scalars().all()
    items = sorted((r.entity_id, r.state.get("quantity"), r.state.get("cost_total"))
                   for r in rows if r.entity_type == "item")
    journals = sorted((r.entity_id, repr(r.state.get("entries"))) for r in rows if r.entity_type == "journal_entry")
    return items, journals


@pytest.mark.parametrize("decisions", [
    {"mode": "full_history"},
    {"mode": "cutover", "cutover_date": "2026-02-28"},
])
async def test_migrated_bill_cannot_be_received_twice(real_engine, real_client, monkeypatch, tmp_path, decisions):
    """RED before the change: a migrated bill carries no receipt state, so Receive Goods
    takes its lines again and adds a second lot of the stock the migration already holds."""
    from celerp.models.company import User
    from celerp.models.migration import MigrationEntityMap, MigrationRun
    from celerp.models.projections import Projection
    from fixtures.manager_io.support import BASIC, ref
    from test_helpers import make_authed_token

    run, rejected = await migrate(real_engine, BASIC.read_bytes(), "basic.manager", decisions, monkeypatch, tmp_path)
    assert rejected == []
    assert run.status == "ready_to_finalize", run.error_summary
    async with maker(real_engine)() as s:
        await finalize_run(s, await s.get(MigrationRun, run.id))
    async with maker(real_engine)() as s:
        maps = {(m.source_type, m.source_external_id): m.target_entity_id for m in (await s.execute(
            select(MigrationEntityMap).where(MigrationEntityMap.migration_run_id == run.id))).scalars()}
        bill_id = maps[("PurchaseInvoice", ref("BILL1"))]
        widget = maps[("InventoryItem", ref("WID"))]
        bill = (await s.get(Projection, (run.company_id, bill_id))).state
        user_id = await s.scalar(select(User.id).where(User.email == OWNER_EMAIL))
        token = await make_authed_token(s, str(user_id), str(run.company_id), "owner")
        location = str(await s.scalar(select(Projection.state["location_id"].as_string()).where(
            Projection.company_id == run.company_id, Projection.entity_id == widget)))

    before = await _stock_and_books(real_engine, run.company_id)
    assert (widget, 15, 60.0) in before[0]

    # Receiving the goods again is refused before anything moves.
    for qty in (10, 1):
        r = await real_client.post(f"/docs/{bill_id}/receive", headers=auth(token), json={
            "location_id": location,
            "received_items": [{"po_line_index": 0, "quantity_received": qty}],
        })
        assert r.status_code == 422, r.text
        assert "at most 0 more can be received" in r.json()["detail"]["message"]
    assert await _stock_and_books(real_engine, run.company_id) == before

    # Because the bill shows the goods the migration stocked as received, on every line.
    assert [li["quantity_received"] for li in bill["line_items"]] == [li["quantity"] for li in bill["line_items"]]
    assert bill.get("received_item_ids") == []
