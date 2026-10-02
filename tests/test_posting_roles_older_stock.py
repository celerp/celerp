# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Stock from before lots recorded their inventory account.

Older releases booked opening stock to 1130-OB and every later goods movement,
including the cost of opening stock sold, to 1130-P. On upgrade a company built
by Celerp itself moves its opening inventory balance into purchased inventory in
one entry, when the two accounts together hold exactly the stock on hand, and
every older lot then records 1130-P. Nothing else changes: total inventory,
retained earnings, cost of sales and older documents stay as they were.

Anything the books cannot vouch for (a migration, a restored backup, an import,
accounts that do not add up) is left alone, and each such lot waits for the
user to pick its account, which is accepted only when that account holds it.

The companies here are built the way an older release wrote them: no posting
accounts in the settings, journal lines without roles, every receipt and sale
booked to 1130-P and pre-system stock carried on 1130-OB.
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import date

import pytest
from sqlalchemy import delete, func, select

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.auto_je import _emit_auto_posted_je
from celerp.services.company_lock import locked_company
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net
from test_posting_roles_lot_origin import _books_match_lots, _open_books
from test_posting_roles_lots import _lot, _sell
from test_posting_roles_merge import _merge
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

_FIELD = "inventory_account_code"
_MARK = "inventory_origin_schema"
_REPAIR = "Settings > Accounting > Posting accounts"


async def _older_release(session, auth) -> None:
    """The company as an older release left it: no posting accounts at all."""
    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if not k.startswith("posting_") and k != _MARK}
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
    await _open_books(client, auth)
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


async def _events(session, auth) -> int:
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))


async def _choose(client, auth, item_id: str, code: str):
    return await client.put(f"/accounting/posting-accounts/older-stock/{item_id}", headers=auth["headers"],
                            json={"code": code})


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


async def test_opening_stock_all_sold_by_an_older_release_leaves_both_accounts_at_zero(session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 100.0, qty=10)
    await _opening_entry(session, auth, 100.0)
    await _sold_by_older_release(session, auth, lot, 10)
    await _startup(session)
    assert _moved(await _reclassification(session, auth)) == {"1130-P": (100.0, 0.0), "1130-OB": (0.0, 100.0)}
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 0.0)


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


async def test_accounts_that_do_not_add_up_to_the_stock_are_left_for_the_user(session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 20.0)  # the opening entry predates 10 of this stock
    await _startup(session)
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, lot) == [None]
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 20.0)
    assert await _marked(session, auth)


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


@pytest.mark.parametrize("source", [_from_migration, _from_bundle_import, _restored, _custom_opening_account])
async def test_a_company_the_books_cannot_vouch_for_is_left_alone(session, client, auth, source):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    await source(session, client, auth)
    await _startup(session)
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, lot) == [None]
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 30.0)
    assert await _marked(session, auth)


async def test_a_period_lock_that_forbids_the_entry_leaves_everything_for_a_later_start(session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    await _settings(session, auth, lock_date=str(date.today()))
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


async def test_a_company_created_with_lots_recording_their_account_never_enters(session, client, auth):
    assert await _marked(session, auth)
    lot = await _lot(client, auth, 30.0)
    await _open_books(client, auth)
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": lot}, populate_existing=True)
    row.state = {k: v for k, v in row.state.items() if k != _FIELD}  # would be normalized if it entered
    await session.commit()
    await _startup(session)
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, lot) == [None]
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


async def test_restored_stock_is_not_assumed_to_be_opening_stock_when_accounting_is_turned_on(session, client, auth):
    await _without_accounting(session, auth)
    await _restored(session, client, auth)
    lot = await _lot(client, auth, 30.0)
    await _startup(session)
    assert await _accounts(session, auth, lot) == [None]
    assert await _net(session, auth, "1130-OB") == (0.0,)


# --- What the normalization leaves unresolved waits for the user's pick -------------------


async def test_an_unresolved_lot_moves_its_cost_once_an_account_that_holds_it_is_picked(session, client, auth):
    await _older_release(session, auth)
    await _restored(session, client, auth)
    lot = await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 30.0)
    await _startup(session)
    assert await _accounts(session, auth, lot) == [None]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "total": 50.0,
        "line_items": [{"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 50.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert "has no recorded inventory account" in r.json()["detail"] and _REPAIR in r.json()["detail"]
    assert r.headers["X-Celerp-Fix"] == "/settings/accounting?tab=posting-accounts"

    r = await _choose(client, auth, lot, "1130-P")
    assert r.status_code == 422, r.text
    assert "books need reconciling" in r.json()["detail"]
    r = await _choose(client, auth, lot, "1210")
    assert r.status_code == 422, r.text
    r = await _choose(client, auth, lot, "1130-OB")
    assert r.status_code == 200, r.text
    assert await _accounts(session, auth, lot) == ["1130-OB"]
    assert (await _choose(client, auth, lot, "1130-OB")).status_code == 409

    await _sold(client, auth, (lot, 1))
    await _books(session, client, auth, purchased=0.0, opening=0.0)
