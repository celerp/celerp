# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Placing older stock on its inventory account while the stock is in use.

The upgrade that records each older lot's inventory account runs at startup, while
requests may already be undoing a sale of one of those lots. The two run on their
own connections against committed data in a database of the test's own. The only
acceptable outcome is the serial one: the undo waits for the upgrade, then finds
the lot on its account and the books still matching the stock.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from company_backup_support import company, owner, token
from migration_support import auth, maker
from stock_books import book_older_opening, older_release_lot
from test_posting_roles_race_pg import _until_blocked

pytestmark = pytest.mark.asyncio

_FIELD = "inventory_account_code"


@pytest_asyncio.fixture
async def own_client(committed_engine, monkeypatch):
    """An API client whose requests commit for real in the test's own database."""
    import celerp.db
    from httpx import ASGITransport, AsyncClient

    from celerp.db import get_session
    from celerp.main import app

    async def _session():
        async with maker(committed_engine)() as s:
            yield s

    @asynccontextmanager
    async def _session_ctx():
        async with maker(committed_engine)() as s:
            yield s

    monkeypatch.setattr(celerp.db, "engine", committed_engine)
    app.dependency_overrides[get_session] = _session
    try:
        with patch("celerp.gateway.client._client", MagicMock()), \
             patch("celerp.gateway.state.get_session_token", return_value="test-session-token"), \
             patch("celerp.middleware.get_session_ctx", _session_ctx):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


async def _post(client, tok: str, path: str, body: dict | None = None) -> dict:
    r = await client.post(path, headers=auth(tok), json=body or {})
    assert r.status_code == 200, r.text
    return r.json()


async def _rows(engine, cid, entity_type: str):
    from celerp.models.projections import Projection

    async with maker(engine)() as s:
        return {r.entity_id: r.state for r in (await s.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == entity_type))).scalars()}


async def _net(engine, cid, code: str) -> float:
    entries = [e for state in (await _rows(engine, cid, "journal_entry")).values()
               if state.get("status") == "posted" for e in state.get("entries") or []]
    return round(sum(float(e.get("debit") or 0) - float(e.get("credit") or 0)
                     for e in entries if e.get("account") == code), 2)


async def _older_books(engine, client):
    """A company as an older release left it: lot ``held`` on hand, lot ``sold`` sold and
    shipped with its cost relieved from 1130-P, the opening entry on 1130-OB, and no lot
    or allocation recording an account."""
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.services.company_lock import locked_company
    from test_helpers import provision_company_books

    user = await owner(engine)
    cid = await company(engine, user, "Origin Race", "origin-race", settings={"currency": "USD"})
    async with maker(engine)() as s:
        await provision_company_books(s, cid)
        await s.commit()
    tok = await token(engine, user, cid)
    async with maker(engine)() as s:
        held = await older_release_lot(s, cid, user, 30.0)
        sold = await older_release_lot(s, cid, user, 100.0)
    async with maker(engine)() as s:
        for lot in (held, sold):
            row = await s.get(Projection, {"company_id": cid, "entity_id": lot})
            row.state = {**row.state, _FIELD: "1130-P"}
        await s.commit()
        await book_older_opening(s, cid, user)
    inv = (await _post(client, tok, "/docs", {"doc_type": "invoice", "total": 150.0, "line_items": [
        {"entity_id": sold, "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]}))["id"]
    await _post(client, tok, f"/docs/{inv}/finalize")
    await _post(client, tok, f"/docs/{inv}/fulfill-lines", {"line_entity_ids": [sold]})
    async with maker(engine)() as s:
        for lot in (held, sold):
            row = await s.get(Projection, {"company_id": cid, "entity_id": lot})
            row.state = {k: v for k, v in row.state.items() if k != _FIELD}
        for row in (await s.execute(select(LedgerEntry).where(
                LedgerEntry.company_id == cid, LedgerEntry.entity_id.like(f"je:auto:{inv}:%")))).scalars():
            if (allocations := (row.metadata_ or {}).get("cogs_allocations")):
                row.metadata_ = {**row.metadata_, "cogs_allocations": {
                    line: {**a, "lots": [{k: v for k, v in lot.items() if k != "account"} for lot in a["lots"]]}
                    for line, a in allocations.items()}}
        found = await locked_company(s, cid)
        found.settings = {k: v for k, v in found.settings.items()
                          if not k.startswith("posting_") and k != "inventory_origin_schema"}
        await s.commit()
    assert (await _net(engine, cid, "1130-P"), await _net(engine, cid, "1130-OB")) == (-100.0, 130.0)
    return cid, tok, inv, held, sold


async def test_undoing_an_older_sale_waits_for_the_upgrade_and_finds_the_lot_on_its_account(
        committed_engine, own_client):
    import asyncio

    from celerp.services.account_roles import reconcile_company
    from celerp.services.lot_origin import normalize_legacy_inventory_origins

    cid, tok, inv, held, sold = await _older_books(committed_engine, own_client)
    async with maker(committed_engine)() as upgrade:
        await reconcile_company(upgrade, cid)
        assert await normalize_legacy_inventory_origins(upgrade, cid)
        undo = asyncio.create_task(own_client.post(f"/docs/{inv}/revert-lines", headers=auth(tok),
                                                   json={"line_entity_ids": [sold]}))
        await _until_blocked(committed_engine, undo)
        await upgrade.commit()
    r = await asyncio.wait_for(undo, timeout=30)
    assert r.status_code == 200, r.text

    items = await _rows(committed_engine, cid, "item")
    assert (items[held].get(_FIELD), items[sold].get(_FIELD)) == ("1130-P", "1130-P")
    assert items[sold]["status"] == "available"
    # 130 moved from 1130-OB; the sold lot came back into stock with its cost.
    assert (await _net(committed_engine, cid, "1130-P"), await _net(committed_engine, cid, "1130-OB")) == (130.0, 0.0)
    await _post(own_client, tok, f"/docs/{inv}/revert-to-draft")
    assert (await _net(committed_engine, cid, "1130-P"), await _net(committed_engine, cid, "1130-OB")) == (130.0, 0.0)


async def test_two_starts_upgrading_the_same_company_upgrade_it_once(committed_engine, own_client):
    """Two servers start against the same database: the second waits for the first's
    upgrade, then finds the company done and posts nothing more."""
    import asyncio

    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.services.account_roles import reconcile_company
    from celerp.services.lot_origin import RECORDED, normalize_legacy_inventory_origins

    cid, tok, inv, held, sold = await _older_books(committed_engine, own_client)
    async with maker(committed_engine)() as first, maker(committed_engine)() as second:
        await reconcile_company(first, cid)
        assert await normalize_legacy_inventory_origins(first, cid)

        async def start():
            await reconcile_company(second, cid)
            done = await normalize_legacy_inventory_origins(second, cid)
            await second.commit()
            return done

        other = asyncio.create_task(start())
        await _until_blocked(committed_engine, other)
        await first.commit()
        assert await asyncio.wait_for(other, timeout=30)

    async with maker(committed_engine)() as s:
        recorded = (await s.execute(select(LedgerEntry.entity_id).where(
            LedgerEntry.company_id == cid, LedgerEntry.event_type == RECORDED))).scalars().all()
        move = await s.get(Projection, {"company_id": cid, "entity_id": f"je:auto:inventory-origin:{cid}"})
    assert len(recorded) == len(set(recorded)) and {held, sold} <= set(recorded)
    assert move.state["status"] == "posted"
    assert (await _net(committed_engine, cid, "1130-P"), await _net(committed_engine, cid, "1130-OB")) == (30.0, 0.0)
