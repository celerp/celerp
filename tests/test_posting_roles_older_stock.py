# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Stock from before lots recorded their inventory account.

Older releases booked opening stock to 1130-OB and every later goods movement,
including the cost of opening stock sold, to 1130-P. On upgrade a company built
by Celerp itself moves its opening inventory balance into purchased inventory in
one entry, when the two accounts together hold exactly the stock on hand, and
every older lot then records 1130-P, including lots already sold, so a sale undone
later brings the lot back on its account. An older draft has never held stock, so it
records nothing, as a new draft does; made available, it records 1130-OB and is booked
there in the same step.
Nothing else changes: total inventory, retained earnings, cost of sales and older
documents stay as they were.

Anything the books cannot vouch for (a migration, a restored backup, an import,
accounts that do not add up) is placed on one default inventory account, against
what the inventory accounts hold together (test_older_stock_reconciliation), and the
company is told.

The companies here are built the way an older release wrote them: no posting
accounts in the settings, journal lines without roles, every receipt and sale
booked to 1130-P and pre-system stock carried on 1130-OB.
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from celerp.events.engine import emit_event
from celerp_accounting.models import Account
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.auto_je import _emit_auto_posted_je
from celerp.services.business_time import business_date_at
from celerp.services.company_lock import locked_company
from celerp.services.lot_origin import unresolved_differences
from stock_books import assert_settled, book_older_opening, older_release_lot
from test_cost_restatement import _state
from test_helpers import TZ
from test_money_stock_and_contact_invariants import _account_net
from test_posting_roles_lot_origin import _books_match_lots
from test_posting_roles_lots import _forget_origin, _lot, _sell
from test_posting_roles_merge import _merge
from test_posting_roles_rollout import _startup
from ui import i18n

pytestmark = pytest.mark.asyncio

_FIELD = "inventory_account_code"
_MARK = "inventory_origin_schema"


async def _older_release(session, auth) -> None:
    """The company as an older release left it: no posting accounts at all, and no work in
    progress account, which came later."""
    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if not k.startswith("posting_") and k != _MARK}
    await session.execute(delete(Account).where(Account.company_id == auth["company_id"], Account.code == "1130-WIP"))
    await session.commit()


async def _settings(session, auth, **changes) -> dict:
    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, **changes}
    await session.commit()
    return company.settings


async def _received(session, auth, cost: float, *, onto: str | None = None) -> str:
    """Goods received on a purchase order the way an older release booked them: a new
    lot (or more stock on the lot ``onto``) and a receipt entry debiting 1130-P."""
    cid, uid = auth["company_id"], auth["user_id"]
    po = f"doc:{uuid.uuid4()}"
    if onto is None:
        lot = f"item:{uuid.uuid4()}"
        await emit_event(session, company_id=cid, entity_id=lot, entity_type="item", event_type="item.created",
                         data={"sku": f"RCV-{uuid.uuid4().hex[:6]}", "name": "Received", "quantity": 1,
                               "sell_by": "piece", "status": "available", "cost_total": cost},
                         actor_id=uid, location_id=None, source="api",
                         idempotency_key=f"{po}:line:0", metadata_={"source_doc": po})
    else:
        lot = onto
        state = await _state(session, auth, onto)
        await emit_event(session, company_id=cid, entity_id=lot, entity_type="item",
                         event_type="item.quantity.adjusted",
                         data={"new_qty": float(state["quantity"]) + 1,
                               "cost_base": float(state["cost_total"]) + cost},
                         actor_id=uid, location_id=None, source="api",
                         idempotency_key=f"{po}:line:0", metadata_={"source_doc": po})
    await _emit_auto_posted_je(
        session, company_id=cid, user_id=uid, je_id=f"je:auto:{po}:rcv:1",
        idem_create=f"{po}:rcv:c", idem_posted=f"{po}:rcv:p", memo=f"Auto JE for {po} received",
        entries=[{"account": "1130-P", "debit": cost, "credit": 0.0},
                 {"account": "2110", "debit": 0.0, "credit": cost}],
        metadata_={"trigger": "doc.received", "doc_id": po})
    await session.commit()
    return lot


async def _sold_by_older_release(session, auth, lot: str, qty: float) -> None:
    """``qty`` of a lot sold the way an older release booked it: the lot shrinks and its
    cost is relieved from 1130-P, whichever account had carried it."""
    cid, uid = auth["company_id"], auth["user_id"]
    inv = f"doc:{uuid.uuid4()}"
    state = await _state(session, auth, lot)
    unit = float(state["cost_total"]) / float(state["quantity"])
    left = float(state["quantity"]) - qty
    await emit_event(session, company_id=cid, entity_id=lot, entity_type="item", event_type="item.quantity.adjusted",
                     data={"new_qty": left, "cost_base": round(unit * left, 2)}, actor_id=uid, location_id=None,
                     source="api", idempotency_key=f"{inv}:line:0", metadata_={"source_doc": inv})
    await _emit_auto_posted_je(
        session, company_id=cid, user_id=uid, je_id=f"je:auto:{inv}:fin", idem_create=f"{inv}:fin:c",
        idem_posted=f"{inv}:fin:p", memo=f"Auto JE for {inv} finalized",
        entries=[{"account": "5100", "debit": round(unit * qty, 2), "credit": 0.0},
                 {"account": "1130-P", "debit": 0.0, "credit": round(unit * qty, 2)}],
        metadata_={"trigger": "doc.finalized", "doc_id": inv})
    await session.commit()


async def _opening_entry(session, auth, amount: float) -> None:
    """The opening inventory entry an older release posted for pre-system stock."""
    cid = auth["company_id"]
    await _emit_auto_posted_je(
        session, company_id=cid, user_id=auth["user_id"], je_id=f"je:auto:opening-inventory:{cid}",
        idem_create=f"opening-inv:{cid}:c:{amount}", idem_posted=f"opening-inv:{cid}:p:{amount}",
        memo="Opening inventory balance (pre-system stock)",
        entries=[{"account": "1130-OB", "debit": amount, "credit": 0.0},
                 {"account": "3200", "debit": 0.0, "credit": amount}],
        metadata_={"trigger": "opening_inventory.auto"})
    await session.commit()


async def _net(session, auth, *accounts: str) -> tuple[float, ...]:
    return tuple([await _account_net(session, auth["company_id"], a) for a in accounts])


async def _books(session, client, auth, purchased: float, opening: float) -> None:
    """1130-P and 1130-OB hold exactly the value of the lots on hand that record them."""
    assert await _books_match_lots(session, auth, "1130-P", "1130-OB") == {"1130-P": purchased, "1130-OB": opening}


async def _sold(client, auth, *lots: tuple[str, float]) -> None:
    """Sell the lots and ship them, so the stock leaves the books."""
    inv = await _sell(client, auth, *lots)
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [lot for lot, _ in lots]})
    assert r.status_code == 200, r.text


async def _accounts(session, auth, *lots: str) -> list[str | None]:
    session.expire_all()
    return [(await _state(session, auth, lot)).get(_FIELD) for lot in lots]


async def _reclassification(session, auth) -> dict | None:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"],
                                         "entity_id": f"je:auto:inventory-origin:{auth['company_id']}"})
    return row.state if row is not None else None


async def _marked(session, auth) -> bool:
    company = await locked_company(session, auth["company_id"])
    marked = _MARK in (company.settings or {})
    await session.commit()
    return marked


async def _placement(session, auth, part: str = "opening") -> dict | None:
    """The entry placing older stock booked, if there was one: the opening stock it
    recognized, or (``part`` "transfer") what it moved between inventory accounts."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.like(f"je:auto:older-stock-placed:%:{part}")))).scalars().all()
    (row,) = rows or [None]
    return row.state if row is not None else None


async def _told_placed(session, auth) -> None:
    from celerp.models.notification import Notification

    session.expire_all()
    (notice,) = (await session.execute(select(Notification).where(
        Notification.company_id == auth["company_id"],
        Notification.title == "Existing inventory is ready"))).scalars().all()
    assert notice.action_url == "/settings/accounting?tab=older-stock#older-stock-reconciliation"
    assert notice.priority == "high"
    assert "Sales and shipments can continue normally." in notice.body


async def _events(session, auth) -> int:
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))


def _moved(je: dict) -> dict[str, tuple[float, float]]:
    return {e["account"]: (e["debit"], e["credit"]) for e in je["entries"]}


# --- The owner's matrix: a native company's older stock is normalized once -------------


async def test_all_old_purchased_stock_records_purchased_inventory_with_no_entry(session, client, auth):
    await _older_release(session, auth)
    lots = [await _received(session, auth, 20.0), await _received(session, auth, 60.0)]
    await _startup(session)
    assert await _accounts(session, auth, *lots) == ["1130-P", "1130-P"]
    assert await _reclassification(session, auth) is None
    assert await _net(session, auth, "1130-P", "1130-OB") == (80.0, 0.0)
    assert await _marked(session, auth)
    assert await _placement(session, auth) is None  # every lot normalized: nothing placed


async def test_all_old_opening_stock_moves_into_purchased_inventory(session, client, auth):
    await _older_release(session, auth)
    lots = [await _lot(client, auth, 30.0), await _lot(client, auth, 40.0)]
    await _opening_entry(session, auth, 70.0)
    before = await _net(session, auth, "3200", "5100")
    await _startup(session)
    assert await _accounts(session, auth, *lots) == ["1130-P", "1130-P"]
    assert _moved(await _reclassification(session, auth)) == {"1130-P": (70.0, 0.0), "1130-OB": (0.0, 70.0)}
    assert await _net(session, auth, "1130-P", "1130-OB") == (70.0, 0.0)
    assert await _net(session, auth, "3200", "5100") == before
    await _sold(client, auth, (lots[0], 1))
    await _books(session, client, auth, purchased=40.0, opening=0.0)


async def test_opening_stock_partly_sold_by_an_older_release_is_set_right(session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 130.0, qty=13)
    await _opening_entry(session, auth, 130.0)
    await _sold_by_older_release(session, auth, lot, 10)
    assert await _net(session, auth, "1130-P", "1130-OB") == (-100.0, 130.0)
    await _startup(session)
    assert _moved(await _reclassification(session, auth)) == {"1130-P": (130.0, 0.0), "1130-OB": (0.0, 130.0)}
    assert await _net(session, auth, "1130-P", "1130-OB") == (30.0, 0.0)
    assert await _accounts(session, auth, lot) == ["1130-P"]
    await _sold(client, auth, (lot, 3))
    await _books(session, client, auth, purchased=0.0, opening=0.0)


async def test_opening_stock_never_shown_on_a_balance_sheet_is_booked_then_moved(session, client, auth):
    """An older release posted the opening inventory entry only when the balance sheet
    was opened. A company that never opened it still carries its pre-system stock on no
    account, so the upgrade books that entry first and then moves it as usual."""
    await _older_release(session, auth)
    lot = await _lot(client, auth, 100.0, qty=10)
    other = await _lot(client, auth, 20.0, qty=4)
    await _sold_by_older_release(session, auth, lot, 2)
    assert await _net(session, auth, "1130-P", "1130-OB") == (-20.0, 0.0)
    await _startup(session)
    assert await _accounts(session, auth, lot, other) == ["1130-P", "1130-P"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (100.0, 0.0)
    await _sold(client, auth, (lot, 1))
    await _books(session, client, auth, purchased=90.0, opening=0.0)
    await assert_settled(client, session, auth)


async def test_purchased_and_opening_stock_together(session, client, auth):
    await _older_release(session, auth)
    opening, bought = await _lot(client, auth, 30.0), await _received(session, auth, 20.0)
    await _opening_entry(session, auth, 30.0)
    await _startup(session)
    assert await _accounts(session, auth, opening, bought) == ["1130-P", "1130-P"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (50.0, 0.0)


async def test_a_lot_of_opening_stock_topped_up_by_a_purchase_order(session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 10.0)
    await _opening_entry(session, auth, 10.0)
    await _received(session, auth, 15.0, onto=lot)
    await _startup(session)
    assert _moved(await _reclassification(session, auth)) == {"1130-P": (10.0, 0.0), "1130-OB": (0.0, 10.0)}
    assert await _accounts(session, auth, lot) == ["1130-P"]
    await _sold(client, auth, (lot, 2))
    await _books(session, client, auth, purchased=0.0, opening=0.0)


async def test_many_older_lots_then_merges_and_sales_keep_each_account_equal_to_its_lots(session, client, auth):
    await _older_release(session, auth)
    opening = await _lot(client, auth, 30.0)
    part_sold = await _lot(client, auth, 40.0, qty=8)
    bought = await _received(session, auth, 20.0)
    mixed = await _lot(client, auth, 10.0)
    await _opening_entry(session, auth, 80.0)
    await _received(session, auth, 15.0, onto=mixed)
    await _sold_by_older_release(session, auth, part_sold, 5)  # 25 relieved from 1130-P
    assert await _net(session, auth, "1130-P", "1130-OB") == (10.0, 80.0)

    await _startup(session)
    assert await _accounts(session, auth, opening, part_sold, bought, mixed) == ["1130-P"] * 4
    await _books(session, client, auth, purchased=90.0, opening=0.0)

    r = await _merge(client, auth, [opening, bought])
    assert r.status_code == 200, r.text
    merged = r.json()["id"]
    assert await _accounts(session, auth, merged) == ["1130-P"]
    await _books(session, client, auth, purchased=90.0, opening=0.0)
    await _sold(client, auth, (part_sold, 3), (mixed, 2))
    await _books(session, client, auth, purchased=50.0, opening=0.0)
    await _sold(client, auth, (merged, 2))
    await _books(session, client, auth, purchased=0.0, opening=0.0)


async def _placed(session, auth, lot: str, code: str) -> None:
    """Value a lot on ``code`` the way an older release valued every lot it sold."""
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": lot}, populate_existing=True)
    row.state = {**row.state, _FIELD: code}
    await session.commit()


async def _shipped_by_older_release(session, client, auth, lot: str, qty: float) -> str:
    """Sell and ship ``qty`` of a lot through the real invoice, with its cost relieved
    from 1130-P as an older release did. Returns the invoice."""
    await _placed(session, auth, lot, "1130-P")
    inv = await _sell(client, auth, (lot, qty))
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    return inv


async def _as_older_release(session, auth, lots: list[str], invoices: list[str]) -> None:
    """What an older release left behind: lots recording no account, finalize allocations
    naming no account, and no posting accounts in the settings."""
    for lot in lots:
        await _forget_origin(session, auth, lot)
    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_type == "journal_entry"))).scalars()
    for row in rows:
        if not any(row.entity_id.startswith(f"je:auto:{inv}:") for inv in invoices):
            continue
        allocations = (row.metadata_ or {}).get("cogs_allocations")
        if allocations:
            row.metadata_ = {**row.metadata_, "cogs_allocations": {
                line: {**alloc, "lots": [{k: v for k, v in lot.items() if k != "account"}
                                         for lot in alloc.get("lots") or []]}
                for line, alloc in allocations.items()}}
    await session.commit()
    await _older_release(session, auth)


async def _status(session, auth, lot: str) -> str:
    session.expire_all()
    return (await _state(session, auth, lot))["status"]


async def _revert(client, auth, inv: str, lot: str) -> None:
    r = await client.post(f"/docs/{inv}/revert-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text


async def test_opening_stock_all_sold_and_shipped_by_an_older_release_leaves_both_accounts_at_zero(session, client,
                                                                                                   auth):
    lot = await older_release_lot(session, auth["company_id"], auth["user_id"], 100.0, qty=10)
    await _opening_entry(session, auth, 100.0)
    inv = await _shipped_by_older_release(session, client, auth, lot, 10)
    await _as_older_release(session, auth, [lot], [inv])
    assert await _status(session, auth, lot) == "sold"
    assert await _net(session, auth, "1130-P", "1130-OB") == (-100.0, 100.0)
    await _startup(session)
    assert _moved(await _reclassification(session, auth)) == {"1130-P": (100.0, 0.0), "1130-OB": (0.0, 100.0)}
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 0.0)
    assert await _accounts(session, auth, lot) == ["1130-P"]


async def test_stock_sold_before_the_upgrade_comes_back_on_its_account_when_the_sale_is_undone(session, client, auth):
    lot = await older_release_lot(session, auth["company_id"], auth["user_id"], 100.0)
    await _opening_entry(session, auth, 100.0)
    inv = await _shipped_by_older_release(session, client, auth, lot, 1)
    await _as_older_release(session, auth, [lot], [inv])
    assert await _status(session, auth, lot) == "sold"
    await _startup(session)
    assert await _accounts(session, auth, lot) == ["1130-P"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 0.0)

    await _revert(client, auth, inv, lot)
    assert await _status(session, auth, lot) == "available"
    assert await _accounts(session, auth, lot) == ["1130-P"]
    # Back in stock, the lot takes its cost of sales off the invoice and sits on its account.
    assert await _net(session, auth, "1130-P", "1130-OB") == (100.0, 0.0)

    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert await _status(session, auth, lot) == "sold"
    await _books(session, client, auth, purchased=0.0, opening=0.0)


async def test_running_it_again_changes_nothing(session, client, auth):
    from celerp.services.lot_origin import normalize_legacy_inventory_origins

    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    await _startup(session)
    events = await _events(session, auth)
    await _startup(session)
    assert await _events(session, auth) == events
    # Even asked again with the upgrade unmarked, the books it left need nothing more.
    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != _MARK}
    await normalize_legacy_inventory_origins(session, auth["company_id"])
    await session.commit()
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lot) == ["1130-P"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (30.0, 0.0)


async def test_a_failure_part_way_leaves_no_entry_and_no_lot_recorded(session, client, auth, monkeypatch):
    import celerp.services.lot_origin as lot_origin

    await _older_release(session, auth)
    lots = [await _lot(client, auth, 30.0), await _lot(client, auth, 40.0)]
    await _opening_entry(session, auth, 70.0)
    real, calls = lot_origin._record, []

    async def failing(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        await real(*args, **kwargs)

    monkeypatch.setattr(lot_origin, "_record", failing)
    with contextlib.suppress(RuntimeError):
        await _startup(session)
    await session.rollback()
    assert len(calls) == 2
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, *lots) == [None, None]
    assert not await _marked(session, auth)
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 70.0)

    monkeypatch.setattr(lot_origin, "_record", real)
    await _startup(session)
    assert await _accounts(session, auth, *lots) == ["1130-P", "1130-P"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (70.0, 0.0)


async def test_accounts_holding_more_than_the_stock_are_placed_with_the_difference_left_unresolved(
        session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _books_over(session, client, auth)
    await _startup(session)
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, lot) == ["1130-P"]
    assert await _placement(session, auth) is None and await _placement(session, auth, "transfer") is None
    assert await _net(session, auth, "1130-P", "1130-OB") == (40.0, 0.0)
    assert await unresolved_differences(session, auth["company_id"]) == {"1130-P": Decimal("10")}
    assert await _marked(session, auth)
    await _told_placed(session, auth)


async def test_an_opening_entry_short_of_the_stock_is_brought_current_then_moved(session, client, auth):
    """An older release restated its opening inventory entry only when the balance sheet
    was opened, so an entry short of the stock is one not yet brought current."""
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 20.0)
    await _startup(session)
    assert await _accounts(session, auth, lot) == ["1130-P"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (30.0, 0.0)
    await assert_settled(client, session, auth)


async def _from_migration(session, client, auth) -> None:
    cid = auth["company_id"]
    await emit_event(session, company_id=cid, entity_id=f"item:{uuid.uuid4()}", entity_type="item",
                     event_type="item.created", data={"sku": "MIG", "name": "Migrated", "quantity": 1,
                                                      "sell_by": "piece", "status": "available", "cost_total": 5.0},
                     actor_id=None, location_id=None, source="migration", idempotency_key=f"mig:{uuid.uuid4()}")
    await session.commit()


async def _from_bundle_import(session, client, auth) -> None:
    cid = auth["company_id"]
    await emit_event(session, company_id=cid, entity_id=f"item:{uuid.uuid4()}", entity_type="item",
                     event_type="item.snapshot", data={"sku": "IMP", "name": "Imported", "quantity": 1,
                                                       "sell_by": "piece", "status": "available", "cost_total": 5.0},
                     actor_id=None, location_id=None, source="import:bundle", idempotency_key=f"cif:{uuid.uuid4()}")
    await session.commit()


async def _restored(session, client, auth) -> None:
    await _settings(session, auth, restored_backup={"backup_id": str(uuid.uuid4())})


async def _custom_opening_account(session, client, auth) -> None:
    from celerp_accounting.models import Account
    from sqlalchemy import update

    await session.execute(update(Account).where(Account.company_id == auth["company_id"], Account.code == "1130-OB")
                          .values(is_active=False))
    await session.commit()


async def _migration_run(session, client, auth) -> None:
    from celerp.models.migration import MigrationRun

    session.add(MigrationRun(company_id=auth["company_id"], created_by_user_id=auth["user_id"],
                             scan_claim_sha256=uuid.uuid4().hex * 2, source_system="fake_source",
                             source_artifact_sha256="0" * 64, adapter_version="1", cif_version="1",
                             mode="full_history"))
    await session.commit()


async def _source_controls(session, client, auth) -> None:
    """Books a migration brought in: its source controls, and the posting accounts its
    finalize mapped, which the start does not guess for source books."""
    from celerp.services.account_roles import with_role

    company = await locked_company(session, auth["company_id"])
    settings = {**company.settings, "posting_source_controls": {"inventory": "1400"}}
    for role, code in (("inventory_purchased", "1130-P"), ("inventory_opening", "1130-OB"),
                       ("retained_earnings", "3200")):
        settings = with_role(settings, role, code)
    company.settings = settings
    await session.commit()


# Where each is placed: opening inventory when it holds the stock or the books show stock
# brought in from elsewhere, purchased inventory when opening inventory cannot take it;
# stock the accounts fall short of is recognized as opening stock.
@pytest.mark.parametrize("source,code,difference,books", [
    (_from_migration, "1130-OB", 5.0, (0.0, 35.0)),
    (_from_bundle_import, "1130-OB", 5.0, (0.0, 35.0)),
    (_restored, "1130-OB", 0.0, (0.0, 30.0)),
    (_custom_opening_account, "1130-P", 30.0, (30.0, 30.0)),
    (_migration_run, "1130-OB", 0.0, (0.0, 30.0)),
    (_source_controls, "1130-OB", 0.0, (0.0, 30.0))])
async def test_a_company_the_books_cannot_vouch_for_is_placed(session, client, auth, source, code, difference,
                                                              books):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    await source(session, client, auth)
    await _startup(session)
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, lot) == [code]
    entry = await _placement(session, auth)
    if difference:
        assert _moved(entry) == {code: (difference, 0.0), "3200": (0.0, difference)}
    else:
        assert entry is None
    assert await _net(session, auth, "1130-P", "1130-OB") == books
    assert await _marked(session, auth)
    await _told_placed(session, auth)


async def test_stock_already_on_a_third_inventory_account_places_the_rest_on_opening_inventory(
        session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    elsewhere = await _lot(client, auth, 10.0)
    await _placed(session, auth, elsewhere, "1131")
    await _opening_entry(session, auth, 40.0)  # 1130-P and 1130-OB add up to all the stock
    await _startup(session)
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, lot, elsewhere) == ["1130-OB", "1131"]
    # The 10 opening inventory holds beyond the stock on it is left for the accountant.
    assert await _placement(session, auth) is None
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 40.0)
    assert await unresolved_differences(session, auth["company_id"]) == {"1130-OB": Decimal("10")}
    assert await _marked(session, auth)


async def test_an_earlier_move_into_purchased_inventory_is_never_made_again(session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    cid = auth["company_id"]
    await _emit_auto_posted_je(
        session, company_id=cid, user_id=None, je_id=f"je:auto:inventory-origin:{cid}",
        idem_create=f"inventory-origin:{cid}:c", idem_posted=f"inventory-origin:{cid}:p",
        memo="Opening inventory moved into purchased inventory, where older releases booked its sales",
        entries=[{"account": "1130-P", "debit": 5.0, "credit": 0.0},
                 {"account": "1130-OB", "debit": 0.0, "credit": 5.0}],
        metadata_={"trigger": "inventory_origin.normalized"})
    await session.commit()
    await _startup(session)
    assert _moved(await _reclassification(session, auth)) == {"1130-P": (5.0, 0.0), "1130-OB": (0.0, 5.0)}
    # The earlier move is not made again; the lot is placed on the account holding most
    # of it, with the rest moved there and nothing booked against equity.
    assert await _accounts(session, auth, lot) == ["1130-OB"]
    assert _moved(await _placement(session, auth, "transfer")) == {"1130-OB": (5.0, 0.0), "1130-P": (0.0, 5.0)}
    assert await _placement(session, auth) is None
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 30.0)
    assert await _marked(session, auth)


async def test_a_period_lock_that_forbids_the_entry_leaves_everything_for_a_later_start(session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    await _settings(session, auth, lock_date=business_date_at(datetime.now(timezone.utc), TZ))
    events = await _events(session, auth)
    await _startup(session)
    assert await _events(session, auth) == events
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, lot) == [None]
    assert not await _marked(session, auth)

    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "lock_date"}
    await session.commit()
    await _startup(session)
    assert await _accounts(session, auth, lot) == ["1130-P"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (30.0, 0.0)


async def _lock_notices(session, auth) -> list:
    from celerp.models.notification import Notification

    session.expire_all()
    return list((await session.execute(select(Notification).where(
        Notification.company_id == auth["company_id"],
        Notification.title == "Older stock waits for an open period"))).scalars().all())


async def test_an_upgrade_a_period_lock_holds_back_is_told_once_and_cleared_when_it_runs(session, client, auth):
    """Older stock the upgrade could not place cannot be sold or moved until its account is
    known: the company is told which lock holds it back and how to go on (move the lock),
    once however often it restarts, and the notice is taken off the bell once the upgrade
    runs."""
    await _older_release(session, auth)
    await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    day = business_date_at(datetime.now(timezone.utc), TZ)
    await _settings(session, auth, lock_date=day)
    await _startup(session)
    await _startup(session)
    (notice,) = await _lock_notices(session, auth)
    assert day in notice.body
    assert notice.body == i18n.t("notice.older_stock_locked.body", "en", day=day)
    assert "Move the period lock" in notice.body and "Choose its inventory account" not in notice.body
    assert i18n.t("notice.older_stock_locked.body", "de", day=day) != notice.body
    assert notice.action_url == "/settings/accounting?tab=period-lock"
    assert notice.i18n["title"] == "notice.older_stock_locked.title" and notice.i18n["params"] == {"day": day}

    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "lock_date"}
    await session.commit()
    await _startup(session)
    assert await _marked(session, auth)
    assert await _lock_notices(session, auth) == []


# The entry is dated the company's business day, not the server's. Each case puts the
# two on opposite sides of midnight, with the books locked through the earlier day.
_NEW_YORK = ("America/New_York", datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc), date(2026, 10, 2))  # Oct 1 there
_BANGKOK = ("Asia/Bangkok", datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc), date(2026, 10, 1))  # Oct 2 there


def _clock(monkeypatch, instant: datetime, host_day: date) -> None:
    """Freeze the clock at ``instant`` on a server whose own calendar reads ``host_day``."""
    import datetime as datetime_module

    import celerp.events.engine as event_engine
    import celerp.services.auto_je as auto_je
    import celerp.services.business_time as business_time
    import celerp.services.lot_origin as lot_origin

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(host_day.year, host_day.month, host_day.day)

    for module in (event_engine, auto_je, business_time, lot_origin):
        monkeypatch.setattr(module, "datetime", FixedDateTime, raising=False)
        monkeypatch.setattr(module, "date", FixedDate, raising=False)
    monkeypatch.setattr(datetime_module, "date", FixedDate)


def _upgrade_failures(monkeypatch, auth) -> list:
    """The startup failures logged for this company: a period lock is a refusal, not one."""
    import celerp_accounting.routes as accounting_routes

    failures, real = [], accounting_routes.logger

    class Spy:
        def exception(self, msg, *args, **kwargs):
            if str(auth["company_id"]) in map(str, args):
                failures.append(msg % args)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(accounting_routes, "logger", Spy())
    return failures


async def _locked_through(session, auth, tz: str) -> None:
    await _settings(session, auth, timezone=tz, lock_date="2026-10-01")


async def test_the_move_is_refused_when_the_business_day_is_locked_though_the_servers_is_not(
        session, client, auth, monkeypatch):
    tz, instant, host_day = _NEW_YORK
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    await _locked_through(session, auth, tz)
    _clock(monkeypatch, instant, host_day)
    failures = _upgrade_failures(monkeypatch, auth)
    await _startup(session)
    assert failures == []
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, lot) == [None]
    assert not await _marked(session, auth)


async def test_the_move_is_dated_the_business_day_when_the_servers_day_is_locked(session, client, auth, monkeypatch):
    tz, instant, host_day = _BANGKOK
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    await _locked_through(session, auth, tz)
    _clock(monkeypatch, instant, host_day)
    await _startup(session)
    je = await _reclassification(session, auth)
    assert je is not None and je["ts"][:10] == "2026-10-02"
    assert await _accounts(session, auth, lot) == ["1130-P"]
    assert await _marked(session, auth)


async def test_turning_accounting_on_is_refused_when_the_business_day_is_locked(session, client, auth, monkeypatch):
    tz, instant, host_day = _NEW_YORK
    await _without_accounting(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _locked_through(session, auth, tz)
    _clock(monkeypatch, instant, host_day)
    failures = _upgrade_failures(monkeypatch, auth)
    await _startup(session)
    assert failures == []
    assert await _accounts(session, auth, lot) == [None]
    assert not await _marked(session, auth)
    (notice,) = await _lock_notices(session, auth)
    assert "2026-10-01" in notice.body


async def _opening_inventory(session, auth) -> dict | None:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"],
                                         "entity_id": f"je:auto:opening-inventory:{auth['company_id']}"})
    return row.state if row is not None and row.state.get("status") == "posted" else None




async def test_opening_stock_is_refused_on_a_locked_business_day_though_the_servers_is_not(
        session, auth, monkeypatch):
    from fastapi import HTTPException

    tz, instant, host_day = _NEW_YORK
    await older_release_lot(session, auth["company_id"], auth["user_id"], 30.0)
    await _locked_through(session, auth, tz)
    _clock(monkeypatch, instant, host_day)
    with pytest.raises(HTTPException):
        await book_older_opening(session, auth["company_id"], auth["user_id"])
    await session.rollback()
    assert await _opening_inventory(session, auth) is None
    assert await _net(session, auth, "1130-OB") == (0.0,)


async def test_opening_stock_is_dated_the_business_day_when_the_servers_day_is_locked(session, auth, monkeypatch):
    tz, instant, host_day = _BANGKOK
    await older_release_lot(session, auth["company_id"], auth["user_id"], 30.0)
    await _locked_through(session, auth, tz)
    _clock(monkeypatch, instant, host_day)
    await book_older_opening(session, auth["company_id"], auth["user_id"])
    je = await _opening_inventory(session, auth)
    assert je is not None and je["ts"][:10] == "2026-10-02"
    assert await _net(session, auth, "1130-OB") == (30.0,)


async def test_a_company_created_with_lots_recording_their_account_is_never_normalized(session, client, auth):
    assert await _marked(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _forget_origin(session, auth, lot)  # would be moved to 1130-P if it entered
    await _startup(session)
    assert await _reclassification(session, auth) is None
    # Only placed, on the account holding it, with nothing booked.
    assert await _accounts(session, auth, lot) == ["1130-OB"]
    assert await _placement(session, auth) is None
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 30.0)


# --- Stock entered before Accounting was turned on is opening stock -------------------


async def _without_accounting(session, auth) -> None:
    from celerp_accounting.models import Account

    await _older_release(session, auth)
    await session.execute(delete(Account).where(Account.company_id == auth["company_id"]))
    await session.commit()


async def test_stock_entered_before_accounting_is_turned_on_becomes_opening_stock(session, client, auth):
    await _without_accounting(session, auth)
    lots = [await _lot(client, auth, 30.0), await _lot(client, auth, 20.0)]
    assert await _accounts(session, auth, *lots) == [None, None]
    await _startup(session)
    assert await _accounts(session, auth, *lots) == ["1130-OB", "1130-OB"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 50.0)
    assert await _marked(session, auth)
    await _sold(client, auth, (lots[0], 1))
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 20.0)


async def test_stock_sold_before_accounting_comes_back_with_its_account_and_value_when_the_sale_is_undone(
        session, client, auth):
    # Older releases posted every sale to 1130-P even with no chart of accounts.
    lot = await older_release_lot(session, auth["company_id"], auth["user_id"], 100.0)
    inv = await _shipped_by_older_release(session, client, auth, lot, 1)
    await _as_older_release(session, auth, [lot], [inv])
    await _without_accounting(session, auth)
    assert await _status(session, auth, lot) == "sold"
    await _startup(session)
    assert await _accounts(session, auth, lot) == ["1130-P"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 0.0)
    assert await _marked(session, auth)

    await _revert(client, auth, inv, lot)
    assert await _status(session, auth, lot) == "available"
    assert await _accounts(session, auth, lot) == ["1130-P"]
    r = await client.post(f"/docs/{inv}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    # No balance sheet visit: undoing the sale itself puts the value back on the lot's account.
    assert await _books_match_lots(session, auth, "1130-P", "1130-OB") == {"1130-P": 100.0, "1130-OB": 0.0}


async def test_restored_stock_is_not_assumed_to_be_opening_stock_when_accounting_is_turned_on(session, client, auth):
    await _without_accounting(session, auth)
    await _restored(session, client, auth)
    lot = await _lot(client, auth, 30.0)
    await _startup(session)
    # Not assumed to be opening stock: placed on purchased inventory, its value booked there.
    assert await _accounts(session, auth, lot) == ["1130-P"]
    assert _moved(await _placement(session, auth)) == {"1130-P": (30.0, 0.0), "3200": (0.0, 30.0)}
    assert await _net(session, auth, "1130-P", "1130-OB") == (30.0, 0.0)


# --- Placed older stock sells from the account it was placed on ------------------------


async def test_placed_older_stock_sells_from_the_account_it_was_placed_on(session, client, auth):
    await _older_release(session, auth)
    await _restored(session, client, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    await _startup(session)
    assert await _accounts(session, auth, lot) == ["1130-OB"]
    await _sold(client, auth, (lot, 1))
    await _books(session, client, auth, purchased=0.0, opening=0.0)


# --- A draft from an older release has never held stock -------------------------------


async def _draft(client, auth, cost: float, qty: float) -> str:
    """A manual item left as a draft: not stock yet, so nothing is booked for it."""
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"DFT-{uuid.uuid4().hex[:6]}", "name": "Draft", "quantity": qty, "sell_by": "piece",
        "cost_total": cost})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _make_available(client, auth, lot: str) -> None:
    r = await client.post("/items/bulk/make-available", headers=auth["headers"], json={"entity_ids": [lot]})
    assert r.status_code == 200, r.text


async def _draft_becomes_opening_stock_and_sells(session, client, auth, draft: str, purchased: float) -> None:
    """The draft records nothing until it is made available; then it records opening
    inventory and is booked there at once, and selling it relieves the same account.
    No report is opened, so the books agree with the stock on their own."""
    assert await _status(session, auth, draft) == "draft"
    assert await _accounts(session, auth, draft) == [None]
    await _make_available(client, auth, draft)
    assert await _accounts(session, auth, draft) == ["1130-OB"]
    books = {"1130-P": purchased, "1130-OB": 200.0}
    assert await _books_match_lots(session, auth, "1130-P", "1130-OB") == books
    await _sold(client, auth, (draft, 1))
    assert await _books_match_lots(session, auth, "1130-P", "1130-OB") == {**books, "1130-OB": 100.0}
    await _sold(client, auth, (draft, 1))
    assert await _books_match_lots(session, auth, "1130-P", "1130-OB") == {**books, "1130-OB": 0.0}


async def test_an_older_draft_records_opening_inventory_when_it_is_made_available(session, client, auth):
    lot = await older_release_lot(session, auth["company_id"], auth["user_id"], 30.0)
    await _opening_entry(session, auth, 30.0)
    draft = await _draft(client, auth, 200.0, 2)
    await _as_older_release(session, auth, [lot, draft], [])
    await _startup(session)
    assert _moved(await _reclassification(session, auth)) == {"1130-P": (30.0, 0.0), "1130-OB": (0.0, 30.0)}
    assert await _accounts(session, auth, lot) == ["1130-P"]
    assert await _marked(session, auth)
    await _draft_becomes_opening_stock_and_sells(session, client, auth, draft, purchased=30.0)


async def test_an_older_draft_records_opening_inventory_when_made_available_after_accounting_is_turned_on(
        session, client, auth):
    # Older releases posted every sale to 1130-P even with no chart of accounts, so turning
    # Accounting on upgrades these books like any older company's.
    sold = await older_release_lot(session, auth["company_id"], auth["user_id"], 100.0)
    inv = await _shipped_by_older_release(session, client, auth, sold, 1)
    draft = await _draft(client, auth, 200.0, 2)
    await _as_older_release(session, auth, [sold, draft], [inv])
    await _without_accounting(session, auth)
    await _startup(session)
    assert await _accounts(session, auth, sold) == ["1130-P"]
    assert await _marked(session, auth)
    await _draft_becomes_opening_stock_and_sells(session, client, auth, draft, purchased=0.0)


async def test_an_older_draft_records_nothing_on_upgrade_when_the_opening_account_cannot_take_it(
        session, client, auth):
    draft = await _draft(client, auth, 200.0, 2)
    await _as_older_release(session, auth, [draft], [])
    await _custom_opening_account(session, client, auth)
    await _startup(session)
    assert await _marked(session, auth)
    assert await _accounts(session, auth, draft) == [None]
    r = await client.post("/items/bulk/make-available", headers=auth["headers"], json={"entity_ids": [draft]})
    assert r.status_code == 409, r.text
    assert "Settings > Accounting > Posting accounts" in r.json()["detail"]["message"]
    assert (await _status(session, auth, draft), *await _accounts(session, auth, draft)) == ("draft", None)


async def _books_over(session, client, auth) -> None:
    """Purchased inventory carries 40, more than all the stock: a receipt booked goods
    that never became a lot."""
    cid, po = auth["company_id"], f"doc:{uuid.uuid4()}"
    await _emit_auto_posted_je(
        session, company_id=cid, user_id=auth["user_id"], je_id=f"je:auto:{po}:rcv:1",
        idem_create=f"{po}:rcv:c", idem_posted=f"{po}:rcv:p", memo=f"Auto JE for {po} received",
        entries=[{"account": "1130-P", "debit": 40.0, "credit": 0.0},
                 {"account": "2110", "debit": 0.0, "credit": 40.0}],
        metadata_={"trigger": "doc.received", "doc_id": po})
    await session.commit()


async def _books_restored(session, client, auth) -> None:
    await _opening_entry(session, auth, 30.0)
    await _restored(session, client, auth)


@pytest.mark.parametrize("books", [_books_over, _books_restored])
async def test_an_older_draft_is_booked_once_made_available_where_the_books_cannot_vouch_for_the_stock(
        session, client, auth, books):
    lot = await _lot(client, auth, 30.0)
    draft = await _draft(client, auth, 200.0, 2)
    await _as_older_release(session, auth, [lot, draft], [])
    await books(session, client, auth)
    await _startup(session)
    assert await _reclassification(session, auth) is None
    assert (await _accounts(session, auth, lot, draft))[1] is None
    assert await _marked(session, auth)
    before = await _account_net(session, auth["company_id"], "1130-OB")
    await _make_available(client, auth, draft)
    assert await _accounts(session, auth, draft) == ["1130-OB"]
    assert await _account_net(session, auth["company_id"], "1130-OB") == round(before + 200.0, 2)


async def _accounting_enabled(session, auth, enabled: bool) -> None:
    from celerp.modules.registry import disable_for_company, enable_for_company

    company = await locked_company(session, auth["company_id"])
    company.settings = (enable_for_company(company.settings, "celerp-accounting")[0] if enabled
                        else disable_for_company(company.settings, "celerp-accounting"))
    await session.commit()


async def test_older_stock_waits_for_accounting_to_be_turned_on_then_is_placed(session, client, auth):
    # An older company that never turned Accounting on: one lot sold and shipped, one archived.
    cid, uid = auth["company_id"], auth["user_id"]
    sold = await older_release_lot(session, cid, uid, 100.0)
    inv = await _shipped_by_older_release(session, client, auth, sold, 1)
    archived = await older_release_lot(session, cid, uid, 20.0)
    await emit_event(session, company_id=cid, entity_id=archived, entity_type="item", event_type="item.status.set",
                     data={"new_status": "archived"}, actor_id=uid, location_id=None, source="api",
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    await _as_older_release(session, auth, [sold], [inv])
    await _accounting_enabled(session, auth, False)

    await _startup(session)
    assert not await _marked(session, auth)
    assert await _accounts(session, auth, sold, archived) == [None, None]

    await _accounting_enabled(session, auth, True)
    await _startup(session)
    assert await _marked(session, auth)
    assert None not in await _accounts(session, auth, sold, archived)
    await _revert(client, auth, inv, sold)
    assert (await client.post(f"/docs/{inv}/revert-to-draft", headers=auth["headers"], json={})).status_code == 200
    assert await _status(session, auth, sold) == "available"
    assert await _books_match_lots(session, auth, "1130-P", "1130-OB") == {"1130-P": 100.0, "1130-OB": 0.0}
    await _sold(client, auth, (sold, 1))
    await assert_settled(client, session, auth)
