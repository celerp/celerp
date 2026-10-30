# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Older stock placed on an inventory account by itself, moved in bulk, and undone.

Stock from before lots recorded their inventory account is placed at start, every lot
of it, on one default account: the account the books show holding most of it, or
purchased inventory. What the inventory accounts hold short of the stock is recognized
as opening stock, what they hold beyond it is left unresolved for the accountant
(test_older_stock_reconciliation), and the company is told. From
Settings > Accounting > Older stock the lots can be moved to another inventory account
in bulk, each move undone, and a placement's difference corrected
(test_older_stock_correction).

The stock comes in the way a demo company's does, through the batch import, and is then
made to look as an older release left it: no lot records an account and the company
has no posting accounts in its settings.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

from celerp.accounting_roles import INVENTORY_ORIGIN_KEY, INVENTORY_ORIGIN_SCHEMA
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.account_roles import role_map
from celerp.services.auto_je import _emit_auto_posted_je
from celerp.services.business_time import business_date_at
from celerp.services.company_lock import locked_company
from test_helpers import TZ, in_language
from test_posting_roles_lots import _new_inventory_account, _sell
from test_posting_roles_older_stock import (
    _books_over,
    _events,
    _lock_notices,
    _net,
    _older_release,
    _settings,
)
from test_posting_roles_rollout import _startup
from ui import i18n

_FIELD = "inventory_account_code"
_PLACED = "Existing inventory is ready"
_UNMAPPED = "Older stock waits for a posting account"
_TAB = "/settings/accounting?tab=older-stock"
_LOCALES = ("am", "ar", "de", "en", "es", "fr", "id", "it", "ja", "pt", "th", "vi")


# --- Older stock as a demo import brings it in -----------------------------------------


async def _imported(session, client, auth, costs: list[float], **fields) -> list[str]:
    """Lots brought in by the batch import, 500 to a call, then stripped of the account
    each records and of the event recording it, as an older release never recorded one.
    The import's opening entry stays on 1130-OB, where an older release booked opening
    stock."""
    ids: list[str] = []
    for start in range(0, len(costs), 500):
        records = [{"entity_id": f"item:{uuid.uuid4()}", "event_type": "item.created", "source": "csv",
                    "idempotency_key": f"older:{uuid.uuid4()}",
                    "data": {"sku": f"OLD-{start + i:05}", "name": "Older lot", "quantity": 1, "sell_by": "piece",
                             "status": "available", "cost_total": cost, **fields}}
                   for i, cost in enumerate(costs[start:start + 500])]
        r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": records})
        assert r.status_code == 200, r.text
        assert r.json()["created"] == len(records), r.text
        ids += [rec["entity_id"] for rec in records]
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id.in_(ids))
        .execution_options(populate_existing=True))).scalars().all()
    for row in rows:
        row.state = {k: v for k, v in row.state.items() if k != _FIELD}
    await session.execute(delete(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id.in_(ids),
        LedgerEntry.event_type == "item.inventory_account.recorded"))
    await session.commit()
    return ids


async def _written_down(session, auth, code: str, amount: float) -> None:
    """An entry taking ``amount`` off ``code`` against retained earnings, so the account
    no longer holds all the stock booked to it."""
    je = f"je:{uuid.uuid4()}"
    await _emit_auto_posted_je(
        session, company_id=auth["company_id"], user_id=auth["user_id"], je_id=je, idem_create=f"{je}:c",
        idem_posted=f"{je}:p", memo="Stock written down", metadata_={"trigger": "manual"},
        entries=[{"account": "3200", "debit": amount, "credit": 0.0},
                 {"account": code, "debit": 0.0, "credit": amount}])
    await session.commit()


async def _marked(session, auth) -> None:
    """A company an earlier release already upgraded, its older stock still unplaced."""
    await _settings(session, auth, **{INVENTORY_ORIGIN_KEY: INVENTORY_ORIGIN_SCHEMA})


async def _older_company(session, client, auth, costs: list[float], short: float = 0.0) -> list[str]:
    """An upgraded company whose older lots record no account, with 1130-OB holding their
    value less ``short``."""
    lots = await _imported(session, client, auth, costs)
    await _older_release(session, auth)
    if short:
        await _written_down(session, auth, "1130-OB", short)
    await _marked(session, auth)
    return lots


async def _accounts(session, auth, lots: list[str]) -> list[str | None]:
    session.expire_all()
    rows = {r.entity_id: r.state for r in (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id.in_(lots)))).scalars()}
    return [rows[lot].get(_FIELD) for lot in lots]


async def _notices(session, auth, title: str) -> list:
    from celerp.models.notification import Notification

    session.expire_all()
    return list((await session.execute(select(Notification).where(
        Notification.company_id == auth["company_id"], Notification.title == title))).scalars().all())


async def _entries(session, auth, prefix: str) -> list[dict]:
    """The posted entries whose id starts with ``prefix``, oldest first."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.like(f"{prefix}%")).order_by(Projection.created_at))).scalars().all()
    return [{**r.state, "id": r.entity_id} for r in rows if r.state.get("status") == "posted"]


async def _placements(session, auth) -> list[dict]:
    return [e for e in await _entries(session, auth, "je:auto:older-stock-placed:") if ":correction:" not in e["id"]]


def _lines(je: dict) -> dict[str, tuple[float, float]]:
    return {e["account"]: (e["debit"], e["credit"]) for e in je["entries"]}


async def _off_books(session, auth) -> list[dict]:
    from celerp.services.lot_origin import stock_off_books

    session.expire_all()
    return await stock_off_books(session, auth["company_id"])


async def _placed_lots(session, auth) -> list[str]:
    """The lots recorded on an account by an automatic placement, once per recording."""
    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"],
        LedgerEntry.event_type == "item.inventory_account.recorded"))).scalars().all()
    return [r.entity_id for r in rows if (r.metadata_ or {}).get("placement")]


def _today() -> str:
    return business_date_at(datetime.now(timezone.utc), TZ)


async def _view(client, auth, **params) -> dict:
    r = await client.get("/accounting/older-stock", headers=auth["headers"], params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _move(client, auth, item_ids: list[str], code: str, key: str | None = "default"):
    body = {"item_ids": item_ids, "code": code}
    if key is not None:
        body["key"] = uuid.uuid4().hex if key == "default" else key
    return await client.post("/accounting/older-stock/move", headers=auth["headers"], json=body)


def _refused(r, status: int, key: str) -> dict:
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == key, detail
    assert in_language("de", detail) != detail["message"], detail
    return detail


# --- Placed at start -------------------------------------------------------------------


# Importing 2000 lots is the slow part, not placing them: 25-35s on a workstation,
# past 120s on a 2-vCPU CI runner shared with a second test worker.
@pytest.mark.timeout(240)
async def test_thousands_of_imported_older_lots_are_placed_in_one_start(session, client, auth):
    lots = await _imported(session, client, auth, [1.0] * 2000)
    await _older_release(session, auth)
    await _settings(session, auth, restored_backup={"backup_id": str(uuid.uuid4())})
    await _written_down(session, auth, "1130-OB", 10.0)
    await _startup(session)
    assert set(await _accounts(session, auth, lots)) == {"1130-OB"}
    assert sorted(await _placed_lots(session, auth)) == sorted(lots)
    (entry,) = await _placements(session, auth)
    assert _lines(entry) == {"1130-OB": (10.0, 0.0), "3200": (0.0, 10.0)}
    (notice,) = await _notices(session, auth, _PLACED)
    assert notice.priority == "high" and notice.action_url == f"{_TAB}#older-stock-reconciliation"
    assert "2000 older stock items" in notice.body
    assert await _net(session, auth, "1130-OB") == (2000.0,)
    assert await _off_books(session, auth) == []


async def test_a_migrated_company_places_its_stock_on_opening_inventory(session, client, auth):
    from test_posting_roles_older_stock import _migration_run

    lots = await _imported(session, client, auth, [30.0, 20.0])
    await _older_release(session, auth)
    await _migration_run(session, client, auth)
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB", "1130-OB"]
    assert await _placements(session, auth) == []
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 50.0)
    (notice,) = await _notices(session, auth, _PLACED)
    assert "2 older stock items" in notice.body
    assert await _off_books(session, auth) == []


async def test_accounts_holding_more_than_the_stock_leave_the_difference_unresolved(session, client, auth):
    lots = await _imported(session, client, auth, [30.0])
    await _older_release(session, auth)
    await _written_down(session, auth, "1130-OB", 30.0)
    await _books_over(session, client, auth)  # 1130-P carries 40 for 30 of stock
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-P"]
    assert await _placements(session, auth) == []
    assert await _net(session, auth, "1130-P", "1130-OB") == (40.0, 0.0)
    (notice,) = await _notices(session, auth, _PLACED)
    assert notice.body == i18n.t("notice.older_stock_placed.body", "en", **notice.i18n["params"])
    assert await _off_books(session, auth) == [
        {"kind": "unresolved_difference", "account": "1130-P", "amount": 10.0}]


async def test_existing_upgraded_company_with_unplaced_lots_is_placed_at_the_next_start(session, client, auth):
    lots = await _older_company(session, client, auth, [30.0, 20.0])
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB", "1130-OB"]
    assert await _placements(session, auth) == []
    assert len(await _notices(session, auth, _PLACED)) == 1
    assert await _off_books(session, auth) == []


async def test_placing_again_changes_nothing(session, client, auth):
    from celerp.services.lot_origin import place_older_stock

    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    events = await _events(session, auth)
    await _startup(session)
    assert not await place_older_stock(session, auth["company_id"])
    await session.commit()
    assert await _events(session, auth) == events
    assert len(await _placements(session, auth)) == 1
    assert len(await _notices(session, auth, _PLACED)) == 1


async def test_placement_held_by_a_period_lock_writes_nothing_and_runs_once_unlocked(session, client, auth):
    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await _settings(session, auth, lock_date=_today())
    events = await _events(session, auth)
    await _startup(session)
    await _startup(session)
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == [None]
    (notice,) = await _lock_notices(session, auth)
    assert notice.body == i18n.t("notice.older_stock_locked.body", "en", day=_today())
    assert "Choose its inventory account" not in notice.body
    assert _today() in notice.body

    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "lock_date"}
    await session.commit()
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    (entry,) = await _placements(session, auth)
    assert _lines(entry) == {"1130-OB": (10.0, 0.0), "3200": (0.0, 10.0)}
    assert await _lock_notices(session, auth) == []


async def test_a_period_lock_holds_back_placement_that_needs_no_adjustment_and_tells_the_company(
        session, client, auth):
    """Books that already tie out need no adjustment, but placing still records each lot
    on today's date, so a lock through today holds it back with the lock notice."""
    lots = await _older_company(session, client, auth, [30.0])
    await _settings(session, auth, lock_date=_today())
    events = await _events(session, auth)
    await _startup(session)
    await _startup(session)
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == [None]
    (notice,) = await _lock_notices(session, auth)
    assert notice.body == i18n.t("notice.older_stock_locked.body", "en", day=_today())

    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "lock_date"}
    await session.commit()
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    assert await _placements(session, auth) == []
    assert await _lock_notices(session, auth) == []


async def _waiting_for_retained(session, client, auth) -> list[str]:
    """Older lots a start left waiting because opening stock must be recognized and
    retained earnings (3200) cannot take entries, and a new equity account 3210 ready to
    be set as retained earnings."""
    from celerp_accounting.models import Account

    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await session.execute(update(Account).where(Account.company_id == auth["company_id"], Account.code == "3200")
                          .values(is_active=False))
    await session.commit()
    await _startup(session)
    assert await _accounts(session, auth, lots) == [None]
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": "3210", "name": "Account 3210", "account_type": "equity", "parent_code": "3000"})
    assert r.status_code == 200, r.text
    return lots


async def test_a_posting_account_change_is_kept_when_a_period_lock_holds_placement_back(session, client, auth):
    lots = await _waiting_for_retained(session, client, auth)
    await _settings(session, auth, lock_date=_today())
    r = await client.put("/accounting/posting-accounts/retained_earnings", headers=auth["headers"],
                         json={"code": "3210"})
    assert r.status_code == 200, r.text
    session.expire_all()
    assert role_map(await _settings(session, auth))["retained_earnings"] == "3210"
    assert await _accounts(session, auth, lots) == [None]
    assert len(await _lock_notices(session, auth)) == 1


async def test_setting_the_posting_account_withdraws_its_notice_while_a_lock_holds_placement(
        session, client, auth):
    await _waiting_for_retained(session, client, auth)
    assert len(await _notices(session, auth, _UNMAPPED)) == 1
    await _settings(session, auth, lock_date=_today())
    r = await client.put("/accounting/posting-accounts/retained_earnings", headers=auth["headers"],
                         json={"code": "3210"})
    assert r.status_code == 200, r.text
    assert await _notices(session, auth, _UNMAPPED) == []
    assert len(await _lock_notices(session, auth)) == 1


def _days_from_today(days: int) -> str:
    return (date.fromisoformat(_today()) + timedelta(days=days)).isoformat()


async def _lock_through(client, auth, day: str) -> None:
    r = await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": day})
    assert r.status_code == 200, r.text
    assert r.json()["lock_date"] == day


async def test_moving_the_period_lock_before_today_places_held_stock_at_once(session, client, auth):
    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await _settings(session, auth, lock_date=_today())
    await _startup(session)
    (notice,) = await _lock_notices(session, auth)
    assert "placed at once" in notice.body

    await _lock_through(client, auth, _days_from_today(1))
    assert await _accounts(session, auth, lots) == [None]
    (notice,) = await _lock_notices(session, auth)
    assert notice.body == i18n.t("notice.older_stock_locked.body", "en", day=_days_from_today(1))

    await _lock_through(client, auth, _days_from_today(-1))
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    assert len(await _placements(session, auth)) == 1
    assert await _lock_notices(session, auth) == []
    assert len(await _notices(session, auth, _PLACED)) == 1


async def test_a_lock_moved_before_today_withdraws_its_notice_even_when_placing_fails(session, client, auth):
    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await _settings(session, auth, lock_date=_today())
    await _startup(session)
    assert len(await _lock_notices(session, auth)) == 1
    with patch("celerp.services.lot_origin.post_opening_stock_delta", AsyncMock(side_effect=RuntimeError("boom"))):
        await _lock_through(client, auth, _days_from_today(-1))
    assert await _accounts(session, auth, lots) == [None]
    assert await _lock_notices(session, auth) == []
    assert len(await _notices(session, auth, i18n.t("notice.older_stock_failed.title", "en"))) == 1


async def test_the_posting_accounts_notice_stays_while_one_is_missing_and_goes_once_none_is(
        session, client, auth):
    from celerp.accounting_roles import ROLES_KEY
    from celerp.services.posting_readiness import notify_unmapped

    title = i18n.t("notice.posting_unmapped.title", "en")
    company = await locked_company(session, auth["company_id"])
    roles = {k: v for k, v in company.settings[ROLES_KEY].items() if k not in ("general_expense", "sales_revenue")}
    company.settings = {**company.settings, ROLES_KEY: roles}
    await session.flush()
    assert await notify_unmapped(session, auth["company_id"])
    await session.commit()

    for role, code, left in (("general_expense", "6950", ["sales_revenue"]), ("sales_revenue", "4100", None)):
        r = await client.put(f"/accounting/posting-accounts/{role}", headers=auth["headers"], json={"code": code})
        assert r.status_code == 200, r.text
        notices = await _notices(session, auth, title)
        if left is None:
            assert notices == []
        else:
            (notice,) = notices
            assert notice.i18n["params"] == {"roles": left}


async def test_a_placement_that_fails_keeps_the_posting_account_change_and_tells_the_company(
        session, client, auth):
    lots = await _waiting_for_retained(session, client, auth)
    with patch("celerp.services.lot_origin.post_opening_stock_delta", AsyncMock(side_effect=RuntimeError("boom"))):
        r = await client.put("/accounting/posting-accounts/retained_earnings", headers=auth["headers"],
                             json={"code": "3210"})
    assert r.status_code == 200, r.text
    session.expire_all()
    assert role_map(await _settings(session, auth))["retained_earnings"] == "3210"
    assert await _accounts(session, auth, lots) == [None]
    assert await _placed_lots(session, auth) == []
    (notice,) = await _notices(session, auth, i18n.t("notice.older_stock_failed.title", "en"))
    assert notice.priority == "high" and notice.action_url == _TAB
    assert notice.body == i18n.t("notice.older_stock_failed.body", "en")


# --- Who a notice is for ------------------------------------------------------------------


async def _bell(client, headers, title: str) -> dict:
    r = await client.get("/notifications", headers=headers)
    assert r.status_code == 200, r.text
    (notice,) = [n for n in r.json()["items"] if n["title"] == title]
    return notice


async def test_the_placed_notice_links_only_accountants_to_where_reconciliation_stands(session, client, auth):
    from test_company_settings_owners import _user_with_role

    await _older_company(session, client, auth, [30.0, 20.0])
    await _startup(session)
    clerk = await _user_with_role(client, session, auth["headers"], "operator")
    accountant = await _bell(client, auth["headers"], _PLACED)
    seller = await _bell(client, clerk, _PLACED)
    assert accountant["action_url"] == f"{_TAB}#older-stock-reconciliation"
    assert seller["action_url"] is None
    assert seller["body"] == accountant["body"] == i18n.t("notice.older_stock_placed.body", "en", count=2)


async def test_a_posting_refused_for_its_account_tells_the_clerk_who_fixes_it_and_notifies_accountants(
        session, client, auth):
    from test_company_settings_owners import _user_with_role

    from celerp.accounting_roles import ROLES_KEY

    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, ROLES_KEY: {**company.settings[ROLES_KEY], "sales_revenue": "1110"}}
    await session.commit()
    clerk = await _user_with_role(client, session, auth["headers"], "operator")
    r = await client.post("/docs", headers=clerk, json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100, "line_total": 100}],
        "subtotal": 100, "tax": 0, "total": 100})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=clerk)
    detail = _refused(r, 409, "posting.refused")
    assert "Someone who manages accounting must choose the account" in detail["message"]
    assert "they have been notified" in detail["message"]

    title = i18n.t("notice.posting_unmapped.title", "en")
    (notice,) = await _notices(session, auth, title)
    assert notice.i18n["params"] == {"roles": ["sales_revenue"]}
    assert (await _bell(client, auth["headers"], title))["action_url"] == "/settings/accounting?tab=posting-accounts"
    assert (await _bell(client, clerk, title))["action_url"] is None

    r = await client.put("/accounting/posting-accounts/sales_revenue", headers=auth["headers"], json={"code": "4100"})
    assert r.status_code == 200, r.text
    assert await _notices(session, auth, title) == []
    r = await client.post(f"/docs/{doc}/finalize", headers=clerk)
    assert r.status_code == 200, r.text


# --- Where reconciliation stands ---------------------------------------------------------


async def test_stock_the_books_hold_exactly_is_reconciled(session, client, auth):
    await _older_company(session, client, auth, [30.0, 20.0])
    await _startup(session)
    (placement,) = (await _view(client, auth))["placements"]
    assert (placement["classification"], placement["provisional"]) == ("exact", 0.0)
    assert (await _view(client, auth))["reconciliation"] == {"state": "reconciled", "waiting": 0, "reasons": [],
                                                            "open": []}


async def test_an_unresolved_difference_is_named_for_review_until_it_is_corrected(session, client, auth):
    await _imported(session, client, auth, [30.0])
    await _older_release(session, auth)
    await _written_down(session, auth, "1130-OB", 30.0)
    await _books_over(session, client, auth)  # 1130-P carries 40 for 30 of stock
    await _startup(session)
    (placement,) = (await _view(client, auth))["placements"]
    assert (await _view(client, auth))["reconciliation"] == {"state": "review", "waiting": 0, "reasons": [], "open": [
        {"placement": placement["pid"], "account": "1130-P", "kind": "unresolved", "amount": 10.0}]}

    r = await client.post("/accounting/older-stock/corrections", headers=auth["headers"],
                          json={"placements": [placement["pid"]], "kind": "recognize"})
    assert r.status_code == 200, r.text
    assert (await _view(client, auth))["reconciliation"]["state"] == "reconciled"


async def test_older_stock_waiting_for_a_setting_needs_action_naming_why(session, client, auth):
    await _waiting_for_retained(session, client, auth)
    reconciliation = (await _view(client, auth))["reconciliation"]
    assert (reconciliation["state"], reconciliation["waiting"], reconciliation["open"]) == ("action", 1, [])
    (reason,) = reconciliation["reasons"]
    assert reason["body"] == "notice.older_stock_unmapped.body_retained"
    assert [p["message_key"] for p in reason["params"]["problems"]] == ["posting.problem.unset"]


async def test_older_stock_that_failed_to_place_needs_action_naming_the_failure(session, client, auth):
    await _waiting_for_retained(session, client, auth)
    with patch("celerp.services.lot_origin.post_opening_stock_delta", AsyncMock(side_effect=RuntimeError("boom"))):
        r = await client.put("/accounting/posting-accounts/retained_earnings", headers=auth["headers"],
                             json={"code": "3210"})
    assert r.status_code == 200, r.text
    reconciliation = (await _view(client, auth))["reconciliation"]
    assert (reconciliation["state"], reconciliation["waiting"]) == ("action", 1)
    assert [reason["body"] for reason in reconciliation["reasons"]] == ["notice.older_stock_failed.body"]


async def test_a_lot_whose_account_is_already_recorded_is_not_reported_placed_again(session, client, auth):
    """A lot whose stored state lost its account while the event recording it stands is
    not placed again: nothing is written for it and no second placed notice is sent."""
    lots = await _older_company(session, client, auth, [30.0, 20.0])
    await _startup(session)
    assert len(await _notices(session, auth, _PLACED)) == 1
    row = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id == lots[0]))).scalar_one()
    row.state = {k: v for k, v in row.state.items() if k != _FIELD}
    await session.commit()
    events = await _events(session, auth)
    for _ in range(3):
        await _startup(session)
    assert await _events(session, auth) == events
    assert len(await _notices(session, auth, _PLACED)) == 1
    assert await _placements(session, auth) == []


async def test_older_stock_waits_for_retained_earnings_then_is_placed_when_it_is_set(session, client, auth):
    from celerp_accounting.models import Account

    role, code, (new, kind, parent) = "retained_earnings", "3200", ("3210", "equity", "3000")

    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await session.execute(update(Account).where(Account.company_id == auth["company_id"], Account.code == code)
                          .values(is_active=False))
    await session.commit()
    events = await _events(session, auth)
    await _startup(session)
    await _startup(session)
    assert await _accounts(session, auth, lots) == [None]
    assert await _events(session, auth) == events
    (notice,) = await _notices(session, auth, _UNMAPPED)
    assert notice.action_url == "/settings/accounting?tab=posting-accounts"
    assert "Retained earnings has no account set." in notice.body  # an inactive account is never mapped
    assert "Settings > Accounting > Posting accounts" in notice.body

    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": new, "name": f"Account {new}", "account_type": kind, "parent_code": parent})
    assert r.status_code == 200, r.text
    r = await client.put(f"/accounting/posting-accounts/{role}", headers=auth["headers"], json={"code": new})
    assert r.status_code == 200, r.text
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    (entry,) = await _placements(session, auth)
    assert _lines(entry) == {"1130-OB": (10.0, 0.0), new: (0.0, 10.0)}
    assert await _notices(session, auth, _UNMAPPED) == []


async def test_older_stock_with_no_cost_is_placed_at_zero(session, client, auth):
    lots = await _older_company(session, client, auth, [0.0, 30.0])
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB", "1130-OB"]
    assert await _placements(session, auth) == []
    assert await _net(session, auth, "1130-OB") == (30.0,)


async def test_consigned_and_draft_lots_are_not_placed(session, client, auth):
    owned = await _imported(session, client, auth, [30.0])
    consigned = await _imported(session, client, auth, [40.0])
    draft = await _imported(session, client, auth, [50.0], status="draft")
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": consigned[0]},
                            populate_existing=True)
    row.state = {**row.state, "consignment_flag": "in"}  # held for a consignor, as goods received on consignment
    await session.commit()
    await _older_release(session, auth)
    await _marked(session, auth)
    await _startup(session)
    assert await _accounts(session, auth, owned + consigned + draft) == ["1130-OB", None, None]


# --- Moving placed lots in bulk ---------------------------------------------------------


async def test_moving_lots_moves_their_value_with_one_entry(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0, 20.0, 30.0])
    await _startup(session)
    r = await _move(client, auth, lots, "1130-P")
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert (result["moved"], result["value"], result["code"]) == (3, 60.0, "1130-P")
    assert await _accounts(session, auth, lots) == ["1130-P"] * 3
    (entry,) = await _entries(session, auth, f"je:auto:older-stock-moved:{result['move_id']}")
    assert _lines(entry) == {"1130-P": (60.0, 0.0), "1130-OB": (0.0, 60.0)}
    assert await _net(session, auth, "1130-P", "1130-OB") == (60.0, 0.0)
    assert await _off_books(session, auth) == []

    # A lot already on the account is skipped: nothing moves, nothing is booked.
    r = await _move(client, auth, lots[:1], "1130-P")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["moved"] == 0
    assert len(await _entries(session, auth, "je:auto:older-stock-moved:")) == 1
    view = r.json()
    assert {lot["item_id"]: lot["account"] for lot in view["lots"]} == dict.fromkeys(lots, "1130-P")
    assert view["moves"][0]["count"] == 3 and view["moves"][0]["code"] == "1130-P"


async def test_a_move_naming_no_lots_is_refused(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    events = await _events(session, auth)
    _refused(await _move(client, auth, [], "1130-P"), 422, "older_stock.move.nothing_selected")
    _refused(await _move(client, auth, [f"item:{i}" for i in range(10001)], "1130-P"), 422,
             "older_stock.move.too_many")
    for key in (None, "", " ", "k" * 65):
        _refused(await _move(client, auth, lots, "1130-P", key=key), 422, "older_stock.move.no_key")
    assert await _events(session, auth) == events


async def test_a_move_to_no_account_is_refused(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    events = await _events(session, auth)
    _refused(await _move(client, auth, lots, "  "), 422, "posting.choose_account")
    assert await _events(session, auth) == events


async def test_a_move_to_an_account_that_cannot_hold_inventory_is_refused(session, client, auth):
    from celerp_accounting.models import Account

    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    await _new_inventory_account(client, auth, "1131")
    await session.execute(update(Account).where(Account.company_id == auth["company_id"], Account.code == "1131")
                          .values(is_active=False))
    await session.commit()
    events = await _events(session, auth)
    _refused(await _move(client, auth, lots, "1120"), 422, "posting.older_stock.never_inventory")
    _refused(await _move(client, auth, lots, "1999"), 422, "posting.older_stock.not_in_chart")
    r = await _move(client, auth, lots, "1131")
    assert r.status_code == 422, r.text
    assert "1131" in r.json()["detail"]["message"]
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-OB"]


async def test_a_lot_with_no_account_or_not_owned_cannot_be_moved(session, client, auth):
    from test_posting_roles_lots import _lot

    placed = await _older_company(session, client, auth, [10.0])
    consigned = await _imported(session, client, auth, [40.0])
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": consigned[0]},
                            populate_existing=True)
    row.state = {**row.state, "consignment_flag": "in"}
    await session.commit()
    await _startup(session)
    bought = await _lot(client, auth, 5.0, sku="NEW-1")  # records its account when it is made
    events = await _events(session, auth)
    for lot in (consigned[0], bought):
        detail = _refused(await _move(client, auth, [*placed, lot], "1130-P"), 422, "older_stock.move.not_placed")
        assert detail["params"]["sku"] in ("OLD-00000", "NEW-1")
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, placed) == ["1130-OB"]


async def test_a_lot_an_unshipped_invoice_holds_cannot_be_moved(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0, 20.0])
    await _startup(session)
    inv = await _sell(client, auth, (lots[0], 1))
    events = await _events(session, auth)
    detail = _refused(await _move(client, auth, lots, "1130-P"), 409, "older_stock.move.invoiced")
    assert detail["params"]["sku"] == "OLD-00000" and detail["params"]["doc"] == inv
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-OB", "1130-OB"]


async def test_a_move_on_a_locked_day_writes_nothing(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    await _settings(session, auth, lock_date=_today())
    events = await _events(session, auth)
    r = await _move(client, auth, lots, "1130-P")
    assert r.status_code == 422, r.text
    await session.rollback()
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-OB"]


async def test_a_move_sent_twice_moves_once(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0, 20.0])
    await _startup(session)
    key = uuid.uuid4().hex
    first = await _move(client, auth, lots, "1130-P", key=key)
    assert first.status_code == 200, first.text
    events = await _events(session, auth)
    again = await _move(client, auth, lots, "1130-P", key=key)
    assert again.status_code == 200, again.text
    assert again.json()["result"]["move_id"] == first.json()["result"]["move_id"]
    assert await _events(session, auth) == events
    assert len(await _entries(session, auth, "je:auto:older-stock-moved:")) == 1
    assert await _net(session, auth, "1130-P", "1130-OB") == (30.0, 0.0)


async def test_a_move_key_reused_for_a_different_move_is_refused(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0, 20.0])
    await _startup(session)
    key = uuid.uuid4().hex
    first = await _move(client, auth, lots[:1], "1130-P", key=key)
    assert first.status_code == 200, first.text
    events = await _events(session, auth)
    for item_ids, code in ((lots, "1130-P"), (lots[:1], "1130-OB")):
        r = await _move(client, auth, item_ids, code, key=key)
        detail = _refused(r, 409, "older_stock.move.key_reused")
        assert "nothing was moved" in detail["message"]
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-P", "1130-OB"]


async def test_a_lot_moved_since_cannot_be_moved_from_its_old_account(session, client, auth):
    from fastapi import HTTPException

    from celerp.events.engine import emit_event

    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    events = await _events(session, auth)
    with pytest.raises(HTTPException) as refused:
        await emit_event(session, company_id=auth["company_id"], entity_id=lots[0], entity_type="item",
                         event_type="item.inventory_account.moved",
                         data={_FIELD: "1131", "from_account": "1130-P"}, actor_id=auth["user_id"],
                         location_id=None, source="system", idempotency_key=f"lot-move:stale:{lots[0]}",
                         metadata_={"move": "stale"})
    await session.rollback()
    assert refused.value.status_code == 409
    assert refused.value.detail["message_key"] == "inventory.account_moved_since"
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-OB"]


async def test_undo_puts_lots_back_once_and_refuses_after_a_later_move(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0, 20.0])
    await _startup(session)
    r = await _move(client, auth, lots, "1130-P")
    assert r.status_code == 200, r.text
    move = r.json()["result"]["move_id"]

    r = await client.post(f"/accounting/older-stock/moves/{move}/undo", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _accounts(session, auth, lots) == ["1130-OB", "1130-OB"]
    (entry,) = await _entries(session, auth, f"je:auto:older-stock-moved:{move}:undo")
    assert _lines(entry) == {"1130-OB": (30.0, 0.0), "1130-P": (0.0, 30.0)}
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 30.0)
    moved = {m["move_id"]: m for m in r.json()["moves"]}
    assert moved[move]["undone"] and moved[f"{move}:undo"]["undo_of"] == move
    _refused(await client.post(f"/accounting/older-stock/moves/{move}/undo", headers=auth["headers"]), 409,
             "older_stock.undo.done")

    r = await _move(client, auth, lots, "1130-P")
    later = r.json()["result"]["move_id"]
    await _new_inventory_account(client, auth, "1131")
    assert (await _move(client, auth, lots[:1], "1131")).status_code == 200
    events = await _events(session, auth)
    detail = _refused(await client.post(f"/accounting/older-stock/moves/{later}/undo", headers=auth["headers"]),
                      409, "older_stock.undo.moved_since")
    assert detail["params"]["sku"] == "OLD-00000"
    assert await _events(session, auth) == events
    _refused(await client.post("/accounting/older-stock/moves/nothing/undo", headers=auth["headers"]), 404,
             "older_stock.undo.no_move")


# --- Moving every matching lot ------------------------------------------------------------


async def _move_all(client, auth, selection: str, code: str, key: str | None = None, **filters):
    return await client.post("/accounting/older-stock/move-all", headers=auth["headers"], json={
        **filters, "selection": selection, "code": code, "key": key or uuid.uuid4().hex})


async def test_the_move_targets_listed_are_exactly_the_accounts_a_move_accepts(session, client, auth):
    """One rule decides both: the picker never offers an account the move refuses, nor
    leaves out one it takes (a new inventory account), and a bank is neither."""
    from celerp_accounting.models import Account
    from test_older_stock_reconciliation import _bank

    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    await _new_inventory_account(client, auth, "1131")
    await _new_inventory_account(client, auth, "1132")
    await session.execute(update(Account).where(Account.company_id == auth["company_id"], Account.code == "1132")
                          .values(is_active=False))
    await session.commit()
    bank = await _bank(client, auth)
    listed = {a["code"] for a in (await _view(client, auth))["targets"]}
    assert {"1130-OB", "1130-P", "1131"} <= listed
    assert not {bank, "1132", "1120", "1130"} & listed
    codes = (await session.execute(select(Account.code).where(Account.company_id == auth["company_id"]))).scalars()
    accepted = set()
    for code in sorted(codes):
        r = await _move(client, auth, lots, code)
        assert r.status_code in (200, 422), (code, r.text)
        if r.status_code == 200:
            accepted.add(code)
    assert accepted == listed


async def test_moving_all_matching_lots_moves_every_page_as_one_undoable_move(session, client, auth):
    lots = await _older_company(session, client, auth, [1.0] * 100 + [2.0] * 20)
    await _startup(session)
    view = await _view(client, auth, q="OLD-000")
    assert len(view["lots"]) == 50
    shown = view["matching"]
    assert (shown["count"], shown["value"]) == (100, 100.0)
    every = (await _view(client, auth))["matching"]
    assert (every["count"], every["value"]) == (120, 140.0) and every["selection"] != shown["selection"]

    key = uuid.uuid4().hex
    r = await _move_all(client, auth, shown["selection"], "1130-P", key=key, q="OLD-000")
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert (result["moved"], result["value"], result["code"]) == (100, 100.0, "1130-P")
    assert await _accounts(session, auth, lots) == ["1130-P"] * 100 + ["1130-OB"] * 20
    (entry,) = await _entries(session, auth, "je:auto:older-stock-moved:")
    assert _lines(entry) == {"1130-P": (100.0, 0.0), "1130-OB": (0.0, 100.0)}

    # Sent again, the same move is answered and nothing more moves.
    events = await _events(session, auth)
    again = await _move_all(client, auth, shown["selection"], "1130-P", key=key, q="OLD-000")
    assert again.status_code == 200, again.text
    assert again.json()["result"]["move_id"] == result["move_id"]
    assert await _events(session, auth) == events

    # One undo puts every lot back.
    r = await client.post(f"/accounting/older-stock/moves/{result['move_id']}/undo", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _accounts(session, auth, lots) == ["1130-OB"] * 120
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 140.0)


async def test_moving_all_matching_lots_is_refused_when_the_lots_matching_changed(session, client, auth):
    lots = await _older_company(session, client, auth, [1.0] * 3)
    await _startup(session)
    selection = (await _view(client, auth))["matching"]["selection"]
    events = await _events(session, auth)
    detail = _refused(await _move_all(client, auth, "0" * 64, "1130-P", account="1130-OB"), 409,
                      "older_stock.move.changed")
    assert detail["params"] == {"count": 3}
    _refused(await _move_all(client, auth, "", "1130-P", q="nothing"), 422, "older_stock.move.nothing_selected")
    with patch("celerp.services.lot_origin.MAX_MOVE", 2):
        _refused(await _move_all(client, auth, selection, "1130-P"), 422, "older_stock.move.too_many")
    _refused(await _move_all(client, auth, selection, "1120"), 422, "posting.older_stock.never_inventory")
    r = await client.post("/accounting/older-stock/move-all", headers=auth["headers"],
                          json={"selection": selection, "code": "1130-P"})
    _refused(r, 422, "older_stock.move.no_key")
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-OB"] * 3


# --- Reading the Older stock tab in pages ----------------------------------------------------


async def _reads(db_engine, client, auth, **params) -> tuple[dict, list[int]]:
    """The view, and the rows each query reading it returned; a read of the posted entries
    counts as -1, as the books grow with every entry whatever the page."""
    from sqlalchemy import event

    fetched: list[int] = []

    def counted(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            fetched.append(-1 if "journal_entry" in repr(parameters) else cursor.rowcount)

    event.listen(db_engine.sync_engine, "after_cursor_execute", counted)
    try:
        view = await _view(client, auth, **params)
    finally:
        event.remove(db_engine.sync_engine, "after_cursor_execute", counted)
    return view, fetched


async def test_the_older_stock_view_reads_one_page_in_a_fixed_number_of_queries(session, client, auth, _db_engine):
    """Each list is read a page at a time by the database, and a move's value comes with
    its page, not one lookup per move: more lots, moves and corrections read no more
    queries and no more rows, but for the posted entries the review reads to check the
    books (stock_off_books)."""
    lots = await _older_company(session, client, auth, [1.0] * 30, short=3.0)
    await _startup(session)
    for lot in lots[:3]:
        assert (await _move(client, auth, [lot], "1130-P")).status_code == 200
    small, few = await _reads(_db_engine, client, auth, limit=2)
    for lot in lots[3:12]:
        assert (await _move(client, auth, [lot], "1130-P")).status_code == 200
    large, many = await _reads(_db_engine, client, auth, limit=2)
    assert len(large["moves"]) == 2 and large["totals"]["moves"] == 12 and small["totals"]["moves"] == 3
    assert len(large["lots"]) == 2 and large["totals"]["lots"] == 30
    assert len(many) == len(few), (few, many)
    assert sorted(many) == sorted(few), (few, many)


async def _renamed(client, auth, item_id: str, old: str, new: str) -> None:
    r = await client.patch(f"/items/{item_id}", headers=auth["headers"],
                           json={"fields_changed": {"name": {"old": old, "new": new}}})
    assert r.status_code == 200, r.text


async def test_moving_all_is_refused_when_one_lot_left_the_filter_and_another_joined(session, client, auth):
    """The same count and value matching is not the same lots: the move confirmed for
    two lots is refused once one of them leaves the filter and another joins, nothing
    moves, and the list shown again can be confirmed."""
    lots = await _older_company(session, client, auth, [1.0] * 3)
    await _startup(session)
    for lot in lots[:2]:
        await _renamed(client, auth, lot, "Older lot", "Shelf A")
    shown = (await _view(client, auth, q="shelf a"))["matching"]
    assert (shown["count"], shown["value"]) == (2, 2.0)

    await _renamed(client, auth, lots[1], "Shelf A", "Older lot")
    await _renamed(client, auth, lots[2], "Older lot", "Shelf A")
    events = await _events(session, auth)
    detail = _refused(await _move_all(client, auth, shown["selection"], "1130-P", q="shelf a"), 409,
                      "older_stock.move.changed")
    assert detail["params"] == {"count": 2}
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-OB"] * 3

    now = (await _view(client, auth, q="shelf a"))["matching"]
    assert (now["count"], now["value"]) == (2, 2.0) and now["selection"] != shown["selection"]
    r = await _move_all(client, auth, now["selection"], "1130-P", q="shelf a")
    assert r.status_code == 200, r.text
    assert await _accounts(session, auth, lots) == ["1130-P", "1130-OB", "1130-P"]


async def test_older_stock_routes_need_manage_accounting(session, client, auth):
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User
    from test_helpers import make_authed_token

    cid, viewer = auth["company_id"], uuid.uuid4()
    session.add(User(id=viewer, email=f"viewer-{viewer.hex[:8]}@test.co", name="Viewer", auth_hash="x",
                     is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=viewer, company_id=cid, role="viewer", is_active=True))
    await session.commit()
    headers = {"Authorization": f"Bearer {await make_authed_token(session, str(viewer), str(cid), 'viewer')}"}
    for method, path, body in (("get", "/accounting/older-stock", None),
                               ("post", "/accounting/older-stock/move", {"item_ids": ["x"], "code": "1130-P",
                                                                         "key": "k"}),
                               ("post", "/accounting/older-stock/move-all", {"selection": "s", "code": "1130-P",
                                                                             "key": "k"}),
                               ("post", "/accounting/older-stock/moves/m/undo", None),
                               ("post", "/accounting/older-stock/retry", None),
                               ("post", "/accounting/older-stock/corrections", {"placements": ["p"],
                                                                                "kind": "recognize"})):
        r = await getattr(client, method)(path, headers=headers, **({"json": body} if body else {}))
        assert r.status_code == 403, (path, r.status_code, r.text)


# --- Settings > Accounting > Older stock -----------------------------------------------


_VIEW = {
    "lots": [{"item_id": "item:old-1", "sku": "OLD-1", "name": "Older lot", "account": "1130-OB", "value": 30.0,
              "placement": "p1"},
             {"item_id": "item:old-2", "sku": "OLD-2", "name": "", "account": "1130-OB", "value": 1234.5,
              "placement": "p1"}],
    "placements": [{"pid": "p1", "day": "2026-10-14", "count": 2, "account": "1130-OB",
                    "classification": "opening_stock", "transferred": 0.0, "recognized": 10.0, "unresolved": 0.0,
                    "journal": ["je:auto:older-stock-placed:p1:opening"], "corrections": []}],
    "moves": [{"move_id": "m1", "day": "2026-10-14", "code": "1130-P", "count": 1, "value": 5.0, "undone": False,
               "undo_of": None}],
    "targets": [{"code": "1130-OB", "name": "Inventory opening", "account_type": "asset"},
                   {"code": "1130-P", "name": "Inventory purchased", "account_type": "asset"}],
    "accounts": ["1130-OB"],
    "currency": "USD",
    "totals": {"lots": 120},
    "matching": {"count": 120, "value": 4321.5, "selection": "sel-120"},
}
_EMPTY = {**_VIEW, "lots": [], "placements": [], "moves": [], "accounts": [], "totals": {},
          "matching": {"count": 0, "value": 0.0, "selection": "sel-0"}}


@pytest.fixture
async def ui_client():
    from ui.app import app as ui_app

    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        yield c


def _cookies() -> dict:
    from test_helpers import make_test_token

    return {"celerp_token": make_test_token(role="owner")}


async def _tab(ui_client, view=None, error=None, query: str = "") -> tuple[str, AsyncMock]:
    company = {"name": "Test Corp", "current_role": "owner", "settings": {}}
    get = AsyncMock(return_value=view, side_effect=error)
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
         patch("ui.api_client.get_bank_accounts", new=AsyncMock(return_value={"items": []})), \
         patch("ui.api_client.get_older_stock", new=get):
        ui_client.cookies.update(_cookies())
        r = await ui_client.get(f"{_TAB}{query}")
    assert r.status_code == 200
    return r.content.decode(), get


async def test_the_older_stock_tab_lists_placed_lots_and_its_empty_state(ui_client):
    body, get = await _tab(ui_client, _VIEW, query="&account=1130-OB&q=OLD")
    assert get.await_args.args[1] == {"account": "1130-OB", "q": "OLD"}
    assert f'href="{_TAB}"' in body
    assert 'id="older-stock-table"' in body and "data-operation-key" in body
    assert "bulk-select-all" in body and 'value="item:old-1"' in body
    assert "OLD-1" in body and "Older lot" in body and ">--<" in body
    assert "$1,234.50" in body
    # The shared bulk toolbar posts the ticked lots to the move route (its action list carries the URL).
    assert 'id="bulkbar-older-stock-table"' in body and "/settings/accounting/older-stock/move" in body
    assert "Move to account" in body
    assert "/settings/accounting/older-stock/moves/m1/undo" in body
    assert "/settings/accounting/older-stock/corrections" in body and "Leave unresolved" in body
    rows = body.split('id="older-stock-placements"')[1].split("</table>")[0]
    assert "Book against retained earnings" not in rows  # nothing is left unresolved to book
    assert ">—<" not in body  # an empty value shows "--", never a dash glyph

    body, _ = await _tab(ui_client, _EMPTY)
    assert "No older stock was placed automatically." in body
    assert "No moves yet." in body


async def test_the_older_stock_tab_opens_with_where_reconciliation_stands(ui_client):
    """The state comes first: what waits and the setting to fix, or each open difference
    with the way to review it, or that everything is reconciled."""
    waiting = {"state": "action", "waiting": 3, "open": [], "reasons": [{
        "title": "notice.older_stock_unmapped.title", "body": "notice.older_stock_unmapped.body_retained",
        "action_url": "/settings/accounting?tab=posting-accounts",
        "params": {"problems": [{"message": "Retained earnings is not set.", "message_key": "posting.problem.unset",
                                 "params": {"role": "Retained earnings"}}]}}]}
    body, _ = await _tab(ui_client, {**_EMPTY, "reconciliation": waiting})
    summary = body.split('id="older-stock-reconciliation"')[1].split("</div>")[0]
    assert body.index('id="older-stock-reconciliation"') < body.index('id="older-stock-table"')
    assert "Action required" in summary and "3 older stock items have no inventory account yet." in summary
    assert "Retained earnings" in summary and 'href="/settings/accounting?tab=posting-accounts"' in summary

    review = {"state": "review", "waiting": 0, "reasons": [], "open": [
        {"placement": "p1", "account": "1130-P", "kind": "unresolved", "amount": 10.0},
        {"placement": "p2", "account": "1130-OB", "kind": "provisional", "amount": 150.0}]}
    body, _ = await _tab(ui_client, {**_VIEW, "reconciliation": review})
    summary = body.split('id="older-stock-reconciliation"')[1].split("</div>")[0]
    assert "Review needed" in summary and "sales continue normally" in summary
    assert "1130-P: a difference of $10.00 is unresolved." in summary
    assert "1130-OB: $150.00 of cost" in summary and 'href="#older-stock-placements"' in summary

    body, _ = await _tab(ui_client, {**_VIEW, "reconciliation": {"state": "reconciled", "waiting": 0,
                                                                  "reasons": [], "open": []}})
    assert "All older stock is placed and matches the books." in body


async def test_the_older_stock_tab_shows_an_api_failure_as_an_error(ui_client):
    from ui.api_client import APIError

    body, _ = await _tab(ui_client, error=APIError(500, "The books could not be read"))
    assert 'class="error-banner"' in body and "The books could not be read" in body


async def test_the_bulk_move_re_renders_the_card_with_a_flash(ui_client):
    from ui.api_client import APIError

    moved = {**_VIEW, "result": {"moved": 2, "value": 1264.5, "code": "1130-P", "move_id": "m2"}}
    move = AsyncMock(return_value=moved)
    ui_client.cookies.update(_cookies())
    with patch("ui.api_client.move_older_stock", new=move):
        r = await ui_client.post("/settings/accounting/older-stock/move",
                                 data={"selected": ["item:old-1", "item:old-2"], "code": "1130-P",
                                       "idempotency_key": "op-1"})
    assert r.status_code == 200
    body = r.content.decode()
    assert move.await_args.args[1:] == (["item:old-1", "item:old-2"], "1130-P", "op-1")
    assert 'id="older-stock-card"' in body and "Moved 2 lots to 1130-P." in body
    assert "hx-redirect" not in r.headers

    refused = AsyncMock(side_effect=APIError(409, "OLD-1 is on an invoice not yet shipped (doc:1)."))
    with patch("ui.api_client.move_older_stock", new=refused), \
         patch("ui.api_client.get_older_stock", new=AsyncMock(return_value=_VIEW)):
        r = await ui_client.post("/settings/accounting/older-stock/move",
                                 data={"selected": ["item:old-1"], "code": "1130-P", "idempotency_key": "op-2"})
    body = r.content.decode()
    assert r.status_code == 200 and 'id="older-stock-card"' in body
    assert "OLD-1 is on an invoice not yet shipped (doc:1)." in body


async def test_moving_all_matching_lots_states_count_and_value_and_asks_first(ui_client):
    body, _ = await _tab(ui_client, _VIEW, query="&q=OLD")
    (form,) = [f.split("</form>")[0] for f in body.split("<form") if 'id="older-stock-move-all"' in f]
    assert "Move all 120 matching lots (value $4,321.50)" in form
    assert "hx-confirm" in form and "You can undo it from Moves." in form
    assert "/settings/accounting/older-stock/move-all?q=OLD" in body
    assert 'name="selection" value="sel-120"' in form and 'name="idempotency_key"' in form
    body, _ = await _tab(ui_client, _EMPTY)
    assert 'id="older-stock-move-all"' not in body

    moved = {**_VIEW, "result": {"moved": 120, "value": 4321.5, "code": "1130-P", "move_id": "m3"}}
    move = AsyncMock(return_value=moved)
    with patch("ui.api_client.move_all_older_stock", new=move), \
         patch("ui.api_client.get_older_stock", new=AsyncMock(return_value=_VIEW)):
        ui_client.cookies.update(_cookies())
        r = await ui_client.post("/settings/accounting/older-stock/move-all?q=OLD",
                                 data={"selection": "sel-120", "code": "1130-P", "idempotency_key": "op-3"})
    assert r.status_code == 200
    assert move.await_args.args[1:] == ({"q": "OLD"}, "sel-120", "1130-P", "op-3")
    assert "Moved 120 lots to 1130-P." in r.content.decode()


async def test_a_changed_selection_shows_the_new_matching_lots_to_confirm(ui_client):
    """Refused because the lots matching changed, the card says so and offers the move
    again for the lots matching now, carrying their selection."""
    from ui.api_client import APIError

    detail = {"message": "The matching lots changed since the list was shown, so nothing was moved. "
                         "121 lots match now. Check them and confirm again.",
              "message_key": "older_stock.move.changed", "params": {"count": 121}}
    now = {**_VIEW, "matching": {"count": 121, "value": 4400.0, "selection": "sel-121"}}
    ui_client.cookies.update(_cookies())
    with patch("ui.api_client.move_all_older_stock", new=AsyncMock(side_effect=APIError(409, "changed", detail))), \
         patch("ui.api_client.get_older_stock", new=AsyncMock(return_value=now)):
        r = await ui_client.post("/settings/accounting/older-stock/move-all?q=OLD",
                                 data={"selection": "sel-120", "code": "1130-P", "idempotency_key": "op-4"})
    body = r.content.decode()
    assert r.status_code == 200 and "121 lots match now" in body
    (form,) = [f.split("</form>")[0] for f in body.split("<form") if 'id="older-stock-move-all"' in f]
    assert "Move all 121 matching lots (value $4,400.00)" in form and 'name="selection" value="sel-121"' in form


def test_older_stock_notices_translate():
    locales = Path(__file__).resolve().parents[1] / "ui" / "locales"
    keys = ("notice.older_stock_placed.title", "notice.older_stock_placed.body",
            "notice.older_stock_unmapped.title",
            "notice.older_stock_unmapped.body_inventory", "notice.older_stock_unmapped.body_held",
            "notice.older_stock_unmapped.body_retained",
            "event.item.inventory_account.moved", "older_stock.tab", "older_stock.move",
            "older_stock.move.nothing_selected", "older_stock.move.too_many", "older_stock.move.no_key",
            "older_stock.move.not_placed", "older_stock.move.invoiced", "older_stock.move.key_reused",
            "notice.older_stock_locked.body", "notice.older_stock_failed.title", "notice.older_stock_failed.body", "older_stock.undo.done",
            "older_stock.undo.moved_since", "older_stock.undo.no_move", "older_stock.book_difference",
            "older_stock.leave_unresolved", "older_stock.corrected_msg", "older_stock.correct.nothing_selected",
            "older_stock.correct.too_many", "older_stock.correct.no_kind", "older_stock.correct.no_placement",
            "older_stock.correct.changed_since", "older_stock.correct.unbalanced", "older_stock.summary",
            "older_stock.col_transferred", "older_stock.col_classification", "older_stock.col_journal",
            "older_stock.col_by", "older_stock.col_amount", "older_stock.col_correction", "older_stock.corrections", "older_stock.no_corrections",
            "older_stock.kind.recognize", "older_stock.kind.unresolve", "enum.older_stock_classification.exact",
            "enum.older_stock_classification.transfer", "enum.older_stock_classification.opening_stock",
            "enum.older_stock_classification.unresolved", "enum.older_stock_classification.uncertain",
            "older_stock.book_difference_confirm", "older_stock.open.uncertain", "older_stock.open.stock_gap",
            "older_stock.correct.uncertain",
            "inventory.account_moved_since")
    for lang in _LOCALES:
        catalog = json.loads((locales / f"{lang}.json").read_text(encoding="utf-8"))
        missing = [k for k in keys if not catalog.get(k)]
        assert missing == [], (lang, missing)
        assert "notice.older_stock_unplaced.body" not in catalog, lang
        assert "notice.older_stock_placed.body_adjusted" not in catalog, lang
        for key in keys:
            assert "—" not in catalog[key], (lang, key)
    en = json.loads((locales / "en.json").read_text(encoding="utf-8"))
    assert "Choose its inventory account" not in en["notice.older_stock_locked.body"]
