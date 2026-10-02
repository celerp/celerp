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
                            sku: str | None = None) -> str:
    """Commit a lot on hand as an older release brought it in: no inventory account
    recorded on it and no entry booking it."""
    lot = f"item:{uuid.uuid4()}"
    await emit_event(session, company_id=company_id, entity_id=lot, entity_type="item",
                     event_type="item.created",
                     data={"sku": sku or f"OLD-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": qty,
                           "sell_by": "piece", "status": "available", "cost_total": cost},
                     actor_id=actor_id, location_id=None, source="api",
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    return lot


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
        code = (lot.state or {}).get(LOT_ACCOUNT_FIELD)
        if code not in held:
            missing.append((lot.entity_id, code))
            continue
        held[code] += value
    assert not missing, f"lots on hand that record no lot inventory account: {missing}"
    held = {code: round_money(v, currency) for code, v in held.items()}
    assert books == held, f"books {books} != stock recorded on them {held}"
    return books
