# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Stock that records no inventory account is placed the moment it arrives.

Starting Celerp is not the only way such stock comes in: an import of another system's
items (item.snapshot), a backup restored as a new company and a finalized migration bring
lots that record no inventory account while Celerp runs. Each is placed at once, by the
same placement a start runs, with a notice of its own and the same bulk move and undo, so
it can be sold straight away and never waits for a restart.

The company comes in the way a demo company does, through the business-type import, and
is then made to look as an older release left it: no lot records an account and the
company has no posting accounts in its settings. The migrated company comes from a
Manager file whose books name no single inventory account.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select

from celerp.accounting_roles import INVENTORY_ORIGIN_KEY, INVENTORY_ORIGIN_SCHEMA
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.notification import Notification
from celerp.models.projections import Projection
from company_backup_support import download, restore
from migration_support import maker, real_client, real_engine  # noqa: F401
from test_consignment_in_sale import _consign
from test_helpers import TZ
from test_older_stock_placement import _PLACED, _accounts, _move, _off_books, _placements, _today, _view
from test_posting_roles_older_stock import _older_release, _settings
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

_FIELD = "inventory_account_code"


async def _owner(client) -> dict:
    """A newly registered company and its owner's credentials."""
    r = await client.post("/auth/register", json={"company_name": "Older Co", "email": f"o-{uuid.uuid4().hex[:8]}@test.example",
                                                  "name": "Owner", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await client.get("/companies/me", headers=headers)).json()
    return {"headers": headers, "company_id": uuid.UUID(str(me["id"])), "token": r.json()["access_token"]}


async def _demo(session, client, auth) -> list[str]:
    """The demo company's lots, as an older release left them."""
    r = await client.post("/companies/me/business-type", headers=auth["headers"], json={"vertical": "agricultural"})
    assert r.status_code == 200, r.text
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item")
        .execution_options(populate_existing=True))).scalars().all()
    assert rows
    for row in rows:
        row.state = {k: v for k, v in row.state.items() if k != _FIELD}
    await session.execute(delete(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id.in_([r.entity_id for r in rows]),
        LedgerEntry.event_type == "item.inventory_account.recorded"))
    await session.commit()
    await _older_release(session, auth)
    return [r.entity_id for r in rows]


async def _running(session, client) -> dict:
    """The demo company of a new owner, upgraded and its older stock placed at a start."""
    auth = await _owner(client)
    lots = await _demo(session, client, auth)
    await _settings(session, auth, **{INVENTORY_ORIGIN_KEY: INVENTORY_ORIGIN_SCHEMA})
    await _startup(session)
    assert None not in await _accounts(session, auth, lots)
    return auth


async def _snapshots(client, auth, *lots: tuple[float, str | None]) -> list[str]:
    """Another system's items imported as snapshots, one per (cost, consignment flag)."""
    records = []
    for i, (cost, flag) in enumerate(lots):
        eid = f"item:{uuid.uuid4()}"
        data = {"sku": f"SNAP-{i}", "name": "Snapshot lot", "quantity": 2, "sell_by": "piece",
                "status": "available", "cost_total": cost, **({"consignment_flag": flag} if flag else {})}
        records.append({"entity_id": eid, "event_type": "item.snapshot", "source": "csv",
                        "idempotency_key": f"snap:{eid}", "data": data})
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": records})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == len(records), r.text
    return [rec["entity_id"] for rec in records]


async def _placed_notices(session, company_id) -> list[Notification]:
    session.expire_all()
    return list((await session.execute(select(Notification).where(
        Notification.company_id == company_id, Notification.title == _PLACED)
        .order_by(Notification.created_at))).scalars().all())


async def _banner(session, company_id) -> str | None:
    session.expire_all()
    settings = (await session.get(Company, company_id)).settings
    assert settings.get("older_stock_banner_dismissed") != settings.get("older_stock_placed")
    return settings.get("older_stock_placed")


async def _unbalanced(session, company_id) -> float:
    """Total debits less total credits over every posted entry."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry"))).scalars().all()
    lines = [e for p in rows if p.state.get("status") == "posted" for e in p.state.get("entries", [])]
    return round(sum(float(e.get("debit") or 0) - float(e.get("credit") or 0) for e in lines), 2)


async def _invoiced(client, auth, lot: str) -> int:
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "line_items": [
        {"entity_id": lot, "name": "Snapshot lot", "quantity": 1, "unit_price": 50.0}], "total": 50.0})
    assert r.status_code == 200, r.text
    return (await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"])).status_code


# --- An import while Celerp runs -------------------------------------------------------


async def test_lots_imported_while_running_are_placed_at_once_with_their_own_notice(session, client):
    auth = await _running(session, client)
    cid = auth["company_id"]
    before, shown = len(await _placed_notices(session, cid)), await _banner(session, cid)

    costed, = await _snapshots(client, auth, (40.0, None))

    (account,) = await _accounts(session, auth, [costed])
    assert account
    notices = await _placed_notices(session, cid)
    assert len(notices) == before + 1
    assert notices[-1].i18n["params"]["count"] == 1 and notices[-1].priority == "high"
    assert await _banner(session, cid) not in (None, shown)
    assert costed in {lot["item_id"] for lot in (await _view(client, auth))["lots"]}
    assert await _off_books(session, auth) == []
    assert await _unbalanced(session, cid) == 0.0
    assert await _invoiced(client, auth, costed) == 200


async def test_a_lot_placed_on_arrival_can_be_moved_in_bulk_and_the_move_undone(session, client):
    auth = await _running(session, client)
    lot, = await _snapshots(client, auth, (40.0, None))
    (placed,) = await _accounts(session, auth, [lot])
    view = await _view(client, auth)
    target = next(c["code"] for c in view["targets"] if c["code"] != placed)

    r = await _move(client, auth, [lot], target)
    assert r.status_code == 200, r.text
    assert await _accounts(session, auth, [lot]) == [target]
    r = await client.post(f"/accounting/older-stock/moves/{r.json()['result']['move_id']}/undo",
                          headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _accounts(session, auth, [lot]) == [placed]
    assert await _off_books(session, auth) == []
    assert await _unbalanced(session, auth["company_id"]) == 0.0


async def test_a_lot_with_no_value_imported_while_running_is_placed_and_books_nothing(session, client):
    auth = await _running(session, client)
    entries = await _placements(session, auth)

    lot, = await _snapshots(client, auth, (0.0, None))

    assert (await _accounts(session, auth, [lot]))[0]
    assert await _placements(session, auth) == entries
    assert await _unbalanced(session, auth["company_id"]) == 0.0


async def test_consigned_stock_is_never_placed_by_an_import_while_running(session, client):
    auth = await _running(session, client)
    _, consigned = await _consign(client, session, auth, qty=2, cost_price=4.0)
    before = len(await _placed_notices(session, auth["company_id"]))

    lot, = await _snapshots(client, auth, (40.0, None))

    assert (await _accounts(session, auth, [lot]))[0]
    assert await _accounts(session, auth, [consigned]) == [None]
    notices = await _placed_notices(session, auth["company_id"])
    assert len(notices) == before + 1 and notices[-1].i18n["params"]["count"] == 1
    assert await _off_books(session, auth) == []


# --- A backup restored while Celerp runs -----------------------------------------------


async def _restored_company(real_client, real_engine, monkeypatch, *, upgraded: bool = False,
                            locked: bool = False):
    """A demo company as an older release left it, holding goods received on consignment,
    backed up and restored as a new company while Celerp runs. ``upgraded`` backs it up
    from a company already marked upgraded whose lots still record no account; ``locked``
    with its books locked through today. Returns the restored company, the demo lots and
    the consigned lot (by their ids in the backed-up company)."""
    source = await _owner(real_client)
    tok = source["token"]
    # the bundled modules run here, at the version the backup records
    monkeypatch.setattr("celerp.modules.loader.is_running", lambda name: True)
    monkeypatch.setattr("celerp.modules.loader.running_version", lambda name: "9999")
    async with maker(real_engine)() as s:
        lots = await _demo(s, real_client, source)
        if upgraded:
            await _settings(s, source, **{INVENTORY_ORIGIN_KEY: INVENTORY_ORIGIN_SCHEMA})
        _, consigned = await _consign(real_client, s, source, qty=2, cost_price=4.0)
        if locked:
            # _today is the day in TZ, so the books are locked through the company's own today
            await _settings(s, source, timezone=TZ, lock_date=_today())
    r = await restore(real_client, tok, await download(real_client, tok), mode="new_company")
    assert r.status_code == 201, r.text
    restored = {"company_id": uuid.UUID(r.json()["company_id"]),
                "headers": {"Authorization": f"Bearer {r.json()['access_token']}"}}
    return restored, lots, consigned


async def _lots(s, company_id) -> tuple[list[Projection], list[Projection]]:
    """The company's own lots that are not drafts, and its consigned ones."""
    rows = (await s.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item"))).scalars().all()
    consigned = [r for r in rows if r.state.get("consignment_flag") == "in"]
    return [r for r in rows if r not in consigned and r.state.get("status") != "draft"], consigned


@pytest.mark.parametrize("upgraded", [False, True], ids=["older-backup", "upgraded-backup"])
async def test_a_backup_restored_while_running_has_its_stock_placed_at_once(real_client, real_engine, monkeypatch,
                                                                            upgraded):
    new, lots, _ = await _restored_company(real_client, real_engine, monkeypatch, upgraded=upgraded)
    cid = new["company_id"]
    async with maker(real_engine)() as s:
        owned, consigned = await _lots(s, cid)
        assert len(owned) == len(lots) + 1 and len(consigned) == 1  # the demo lots and the consignment's template
        assert all(r.state.get(_FIELD) for r in owned), [r.state.get("sku") for r in owned if not r.state.get(_FIELD)]
        assert consigned[0].state.get(_FIELD) is None
        notices = await _placed_notices(s, cid)
        assert len(notices) == 1 and notices[0].priority == "high"
        assert notices[0].i18n["params"]["count"] == len(owned)
        assert await _banner(s, cid)
        assert await _off_books(s, new) == []
        assert await _unbalanced(s, cid) == 0.0
        assert INVENTORY_ORIGIN_KEY in (await s.get(Company, cid)).settings
    view = await _view(real_client, new)
    assert len(view["lots"]) == len(owned)


async def test_a_backup_restored_while_its_books_are_locked_waits_and_is_placed_once_unlocked(
        real_client, real_engine, monkeypatch):
    new, lots, _ = await _restored_company(real_client, real_engine, monkeypatch, upgraded=True, locked=True)
    cid = new["company_id"]
    async with maker(real_engine)() as s:
        owned, _ = await _lots(s, cid)
        assert len(owned) == len(lots) + 1 and not any(r.state.get(_FIELD) for r in owned)
        assert await _placed_notices(s, cid) == []
        waiting = (await s.execute(select(Notification).where(
            Notification.company_id == cid, Notification.action_url.like("%tab=period-lock%")))).scalars().all()
        assert len(waiting) == 1

    r = await real_client.post("/accounting/period-lock", headers=new["headers"],
                               json={"lock_date": (date.fromisoformat(_today()) - timedelta(days=1)).isoformat()})
    assert r.status_code == 200, r.text

    async with maker(real_engine)() as s:
        owned, _ = await _lots(s, cid)
        assert all(r.state.get(_FIELD) for r in owned)
        assert len(await _placed_notices(s, cid)) == 1
        assert await _unbalanced(s, cid) == 0.0


# --- A migration finalized while Celerp runs -----------------------------------------


async def test_a_migration_finalized_while_running_has_its_stock_placed_at_once(real_engine, monkeypatch, tmp_path):
    from celerp.services import account_roles
    from fixtures.manager_io.support import BASIC
    from test_migration_inventory_provenance import _migrated

    # Source books that name several inventory accounts, so no migrated lot can take one.
    monkeypatch.setattr(account_roles, "_source_inventory", lambda settings: None)
    books = await _migrated(real_engine, monkeypatch, tmp_path, source=BASIC)
    cid = books.run.company_id
    async with maker(real_engine)() as s:
        owned, _ = await _lots(s, cid)
        assert owned and all(r.state.get(_FIELD) for r in owned), [
            r.state.get("sku") for r in owned if not r.state.get(_FIELD)]
        notices = await _placed_notices(s, cid)
        assert len(notices) == 1 and notices[0].i18n["params"]["count"] == len(owned)
        assert await _banner(s, cid)
        assert await _off_books(s, {"company_id": cid, "headers": books.headers}) == []
        assert await _unbalanced(s, cid) == 0.0
