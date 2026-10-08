# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The books agree with the stock they carry.

One check shared by every stock-boundary test. Only the accounts lots are carried on
count: the purchased and opening inventory accounts, every account that has served
either. For each, the posted balance equals the value of the owned lots on hand
(lot_origin.held_value) that record it. Clearing accounts that hold value not yet in
a lot are not part of it.

With Accounting on, every lot of the company's own that is on hand records one of
those accounts, except the ones the caller names as unplaced (stock from another
system's books, waiting for the user to place it); their value is left out of the
comparison. With Accounting off nothing is booked, so no account may carry anything.

The value production runs hold is checked apart from the lots (assert_wip_carried):
every account that has served work in progress holds exactly what the open runs kept
on it still hold, and a completed run holds nothing. assert_settled runs both and then
proves the books check (lot_origin.stock_off_books) has nothing to report.

Also shared: the stock an older release left behind, for the tests of what happens to it.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from celerp.accounting_roles import LOT_ACCOUNT_FIELD, SCHEMA_KEY, AccountRole
from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.account_roles import scope_codes
from celerp.services.lot_origin import held_value
from celerp.services.money import round_money

SAMPLE_STOCK_ENTRY = "je:auto:opening-stock:sample-stock:"
"""Id prefix of the journal entry a new company's sample stock is booked with at
registration (lot_origin.recognize_opening_lots), for tests that count the entries
their own documents write."""

async def older_release_lot(session, company_id, actor_id, cost: float, *, qty: float = 1,
                            sku: str | None = None, status: str = "available") -> str:
    """Commit a lot as an older release brought it in (on hand unless ``status`` says
    otherwise): no inventory account recorded on it and no entry booking it."""
    lot = f"item:{uuid.uuid4()}"
    await emit_event(session, company_id=company_id, entity_id=lot, entity_type="item",
                     event_type="item.created",
                     data={"sku": sku or f"OLD-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": qty,
                           "sell_by": "piece", "status": status, "cost_total": cost},
                     actor_id=actor_id, location_id=None, source="api",
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    return lot


async def book_older_opening(session, company_id, user_id) -> None:
    """Commit the opening inventory entry an older release booked for its pre-system stock,
    under the company lock every caller of book_opening_inventory holds."""
    from celerp.services.auto_je import book_opening_inventory
    from celerp.services.company_lock import locked_company

    await locked_company(session, company_id)
    await book_opening_inventory(session, company_id=company_id, user_id=user_id, in_production=Decimal("0"))
    await session.commit()


_LOT_ROLES = (AccountRole.INVENTORY_PURCHASED.value, AccountRole.INVENTORY_OPENING.value)


async def assert_books_carry_stock(session, company_id, *, unplaced=()) -> dict[str, Decimal]:
    """Assert the lot-carrying accounts hold exactly the stock recorded on them, and
    return each account's balance."""
    session.expire_all()
    settings = (await session.get(Company, company_id)).settings or {}
    currency = settings.get("currency", "USD")
    rows = list((await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type.in_(("item", "journal_entry"))))).scalars())
    lots = [r for r in rows if r.entity_type == "item"]
    lines = [e for r in rows if r.entity_type == "journal_entry" and (r.state or {}).get("status") == "posted"
             for e in r.state.get("entries") or []]
    codes = {code for role in _LOT_ROLES for code in scope_codes(settings, role)}
    books = {code: round_money(sum((Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
                                    for e in lines if e.get("account") == code), Decimal("0")), currency)
             for code in codes}
    if SCHEMA_KEY not in settings:
        assert not any(books.values()), f"Accounting is off, yet lot accounts carry {books}"
        return books
    held = dict.fromkeys(codes, Decimal("0"))
    missing = []
    for lot in lots:
        value = held_value(lot)
        if value is None or lot.entity_id in unplaced:
            continue
        if not value and not float((lot.state or {}).get("quantity") or 0):
            continue  # used up: it holds nothing on any account
        code = (lot.state or {}).get(LOT_ACCOUNT_FIELD)
        if code not in held:
            missing.append((lot.entity_id, code))
            continue
        held[code] += round_money(value, currency)  # each posting moves a lot's value to the cent
    assert not missing, f"lots on hand that record no lot inventory account: {missing}"
    held = {code: round_money(v, currency) for code, v in held.items()}
    assert books == held, f"books {books} != stock recorded on them {held}"
    return books


def _run_wip(state: dict) -> Decimal:
    return sum((Decimal(str(state.get(k) or 0)) * sign for k, sign in
                (("wip_issued", 1), ("wip_transferred", -1), ("wip_wasted", -1))), Decimal("0"))


async def assert_wip_carried(session, company_id) -> dict[str, Decimal]:
    """Assert every account that has kept work in progress holds exactly what the open runs
    kept on it still hold, and that no completed run holds anything; return the balances."""
    session.expire_all()
    settings = (await session.get(Company, company_id)).settings or {}
    currency = settings.get("currency", "USD")
    rows = list((await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type.in_(("mfg_order", "journal_entry"))))).scalars())
    runs = [r.state or {} for r in rows if r.entity_type == "mfg_order"]
    lines = [e for r in rows if r.entity_type == "journal_entry" and (r.state or {}).get("status") == "posted"
             for e in r.state.get("entries") or []]
    for run in runs:
        if run.get("status") in ("completed", "cancelled"):
            assert _run_wip(run) == 0, f"a {run.get('status')} run still holds {_run_wip(run)}: {run}"
    codes = set(scope_codes(settings, AccountRole.WORK_IN_PROGRESS.value)) | {
        r["wip_account_code"] for r in runs if r.get("wip_account_code")}
    books = {code: round_money(sum((Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
                                    for e in lines if e.get("account") == code), Decimal("0")), currency)
             for code in codes}
    if SCHEMA_KEY not in settings:
        assert not any(books.values()), f"Accounting is off, yet work in progress accounts carry {books}"
        return books
    held = dict.fromkeys(codes, Decimal("0"))
    for run in runs:
        if _run_wip(run):
            assert run.get("wip_account_code"), f"a run holds {_run_wip(run)} on no account: {run}"
            held[run["wip_account_code"]] += _run_wip(run)
    held = {code: round_money(v, currency) for code, v in held.items()}
    assert books == held, f"work in progress books {books} != what open runs hold {held}"
    return books


async def assert_settled(client, session, auth) -> None:
    """The books carry the stock and the work in progress, and the books check finds
    nothing to report."""
    from celerp.services.lot_origin import stock_off_books

    cid = auth["company_id"]
    await assert_books_carry_stock(session, cid)
    await assert_wip_carried(session, cid)
    assert (found := await stock_off_books(session, cid)) == [], found