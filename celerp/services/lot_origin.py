# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Inventory accounts for stock from before lots recorded their own.

A lot records the inventory account its value is booked into when it first takes on
stock (events.engine._record_lot_account). Stock from an older release recorded none.
Those releases booked pre-system stock to the opening inventory account and every later
goods movement, including the cost of opening stock sold, to the purchased inventory
account, so neither account alone says where a lot's value sits; together they hold all
of it. On upgrade, a company Celerp built itself has its opening inventory balance moved
into purchased inventory by one entry, and every older lot records purchased inventory
(normalize_legacy_inventory_origins). Stock entered before Accounting was turned on is
opening stock, booked by the opening inventory entry (open_inventory_origins).

Nothing else is assumed. Stock in a company whose books came from elsewhere (a
migration, a bundle import, a restored backup), or whose two accounts do not add up to
the stock on hand, records no account and refuses to move its cost
(account_roles.lot_account) until the user picks the account that carries it here
(choose_lot_account). A pick is accepted only when that account holds the lot's value
beyond the stock already recorded on it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import (
    INVENTORY_ORIGIN_KEY,
    INVENTORY_ORIGIN_SCHEMA,
    LOT_ACCOUNT_FIELD,
    SOURCE_CONTROLS_KEY,
    AccountRole,
)
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.account_roles import role_map, scope_codes, target_problems
from celerp.services.money import round_money

RECORDED = "item.inventory_account.recorded"

# Statuses of lots whose stock is not on the books: no longer owned, or (draft) not yet
# committed. Must match get_valuation()'s filter.
NOT_HELD = frozenset({"archived", "deleted", "void", "sold", "fulfilled", "merged", "expired", "draft", "disposed"})

_INVENTORY = (AccountRole.INVENTORY_PURCHASED.value, AccountRole.INVENTORY_OPENING.value)


def held_value(row: Projection) -> Decimal | None:
    """The value of the goods a lot holds on the books, or None when it holds none on
    them (not owned, not committed, consigned in, or not stocked)."""
    s = row.state or {}
    if str(s.get("status") or "").lower() in NOT_HELD:
        return None
    if s.get("consignment_flag") == "in" or row.consignment_flag == "in":
        return None
    if (s.get("inventory_type") or "stocked") != "stocked":
        return None
    if float(s.get("cost_total") or 0):
        return Decimal(str(s["cost_total"]))
    cost = s.get("cost_price") or s.get("cost price")
    return Decimal(str(cost)) * Decimal(str(s.get("quantity") or 0)) if cost is not None else Decimal("0")


async def _items(session: AsyncSession, company_id) -> list[Projection]:
    return list((await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item"))).scalars())


async def _posted_entries(session: AsyncSession, company_id) -> list[tuple[str, dict]]:
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry"))).scalars()
    return [(row.entity_id, entry) for row in rows if (row.state or {}).get("status") == "posted"
            for entry in row.state.get("entries") or []]


def _room(entries: list[tuple[str, dict]], items: list[Projection], code: str) -> Decimal:
    """What ``code`` holds beyond the value of the lots on hand that record it."""
    balance = sum((Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
                   for _, e in entries if e.get("account") == code), Decimal("0"))
    recorded = sum((held_value(r) or Decimal("0") for r in items if (r.state or {}).get(LOT_ACCOUNT_FIELD) == code),
                   Decimal("0"))
    return balance - recorded


def unrecorded(items: list[Projection]) -> list[Projection]:
    """The lots on hand that record no inventory account."""
    return [r for r in items if not (r.state or {}).get(LOT_ACCOUNT_FIELD) and held_value(r) is not None]


async def unrecorded_lots(session: AsyncSession, company_id) -> list[dict]:
    """Stock on hand that records no inventory account, oldest first."""
    rows = sorted(unrecorded(await _items(session, company_id)), key=lambda r: (r.created_at is None, r.created_at))
    return [{"item_id": r.entity_id, "sku": r.state.get("sku") or "", "name": r.state.get("name") or "",
             "value": float(held_value(r))} for r in rows]


async def _record(session: AsyncSession, company_id, item_id: str, code: str, why: str, actor_id) -> None:
    from celerp.events.engine import emit_event

    await emit_event(session, company_id=company_id, entity_id=item_id, entity_type="item", event_type=RECORDED,
                     data={LOT_ACCOUNT_FIELD: code}, actor_id=actor_id, location_id=None, source="system",
                     idempotency_key=f"lot-account:{item_id}", metadata_={"recorded_by": why})


async def _foreign(session: AsyncSession, company_id, settings: dict) -> bool:
    """Whether the company's books could have come from outside Celerp, so they cannot
    vouch for where its stock sits: a migration run or the source-book controls one
    leaves, a restored backup, or stock brought in by a migration or a bundle import.
    Only a company with none of these is Celerp's own."""
    from celerp.models.migration import MigrationRun

    if SOURCE_CONTROLS_KEY in settings or settings.get("restored_backup"):
        return True
    if await session.scalar(select(MigrationRun.id).where(MigrationRun.company_id == company_id).limit(1)):
        return True
    return await session.scalar(select(LedgerEntry.id).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "item",
        or_(LedgerEntry.event_type == "item.snapshot",
            and_(LedgerEntry.event_type == "item.created", LedgerEntry.source == "migration"))).limit(1)) is not None


async def _locked(session: AsyncSession, company_id, roles: list[str]) -> tuple[dict, dict[str, str] | None]:
    """Lock the company, its chart, the accounts of ``roles`` and its lots, in that order.
    Returns the settings and each role's account, or None for the accounts when one of
    them cannot take an entry (unmapped, inactive, the wrong type) or two share an account."""
    from celerp.services.company_lock import lock_chart, locked_company
    from celerp.services.journal_accounts import lock_accounts

    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    await lock_chart(session, company_id)
    current = role_map(settings)
    accounts = await lock_accounts(session, company_id, {current[r] for r in roles if current.get(r)})
    await session.execute(select(Projection.entity_id).where(
        Projection.company_id == company_id, Projection.entity_type == "item").with_for_update())
    codes = {r: current.get(r) for r in roles}
    if accounts is None or target_problems(roles, current, accounts) or len(set(codes.values())) < len(roles):
        return settings, None
    return settings, codes


async def _mark(session: AsyncSession, company_id) -> None:
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), INVENTORY_ORIGIN_KEY: INVENTORY_ORIGIN_SCHEMA}
    await session.flush()


async def _period_open(session: AsyncSession, company_id, today: str) -> bool:
    from celerp.events.engine import _check_period_lock

    try:
        await _check_period_lock(session, company_id, {"ts": today})
    except HTTPException as exc:
        if exc.status_code == 422:
            return False
        raise
    return True


async def normalize_legacy_inventory_origins(session: AsyncSession, company_id) -> bool:
    """Give the older stock of a company Celerp built itself the inventory account it sits
    in (module docstring), all in one savepoint. The purchased (P) and opening (OB)
    inventory accounts must both take entries, and together hold exactly the stock on
    hand (V); then one entry dated today moves OB, beyond the stock recording OB, into P,
    every older lot records P, and the company is marked upgraded. Retained earnings, cost
    of sales, total inventory and older documents are untouched. When the books cannot
    vouch for the stock, nothing moves and the company is still marked, leaving each older
    lot for the user to place. A period lock that forbids the entry writes nothing and
    leaves the company unmarked, to retry on a later start. Running it again changes
    nothing. Returns whether the company was marked."""
    from celerp.services.auto_je import _emit_auto_posted_je, _line

    async with session.begin_nested():
        purchased, opening = AccountRole.INVENTORY_PURCHASED.value, AccountRole.INVENTORY_OPENING.value
        settings, codes = await _locked(session, company_id, [purchased, opening])
        items = await _items(session, company_id)
        pending = unrecorded(items)
        if codes is None or not pending or await _foreign(session, company_id, settings):
            await _mark(session, company_id)
            return True
        p, ob = codes[purchased], codes[opening]
        currency = settings.get("currency", "USD")
        held = [(r, held_value(r)) for r in items if held_value(r) is not None]
        if any((r.state or {}).get(LOT_ACCOUNT_FIELD) not in (None, "", p, ob) for r, _ in held):
            await _mark(session, company_id)
            return True
        entries = await _posted_entries(session, company_id)
        balance = {code: sum((Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
                              for _, e in entries if e.get("account") == code), Decimal("0")) for code in (p, ob)}
        value = sum((v for _, v in held), Decimal("0"))
        if round_money(balance[p] + balance[ob], currency) != round_money(value, currency):
            await _mark(session, company_id)
            return True
        on_opening = sum((v for r, v in held if (r.state or {}).get(LOT_ACCOUNT_FIELD) == ob), Decimal("0"))
        moved = round_money(balance[ob] - on_opening, currency)
        je_id = f"je:auto:inventory-origin:{company_id}"
        if moved:
            today = str(date.today())
            if await session.get(Projection, {"company_id": company_id, "entity_id": je_id}) is not None:
                await _mark(session, company_id)  # moved once already; the books have changed since
                return True
            if not await _period_open(session, company_id, today):
                return False
            amount, (debit, credit) = abs(moved), ((p, purchased), (ob, opening)) if moved > 0 else ((ob, opening), (p, purchased))
            await _emit_auto_posted_je(
                session, company_id=company_id, user_id=None, je_id=je_id,
                idem_create=f"inventory-origin:{company_id}:c", idem_posted=f"inventory-origin:{company_id}:p",
                memo="Opening inventory moved into purchased inventory, where older releases booked its sales",
                entries=[_line(debit[0], debit[1], debit=float(amount)), _line(credit[0], credit[1], credit=float(amount))],
                metadata_={"trigger": "inventory_origin.normalized"}, ts=today)
        for row in sorted(pending, key=lambda r: r.entity_id):
            await _record(session, company_id, row.entity_id, p, "normalized", None)
        await _mark(session, company_id)
        if moved:
            await _notify_moved(session, company_id, p, ob, abs(moved), currency)
    return True


async def _notify_moved(session: AsyncSession, company_id, p: str, ob: str, amount: Decimal, currency: str) -> None:
    from celerp.notifications import service as notification_service

    await notification_service.create(
        session, company_id, "accounting", "Older stock moved to purchased inventory",
        f"Older releases booked the cost of opening stock sold to {p}, so {p} and {ob} only matched your stock "
        f"together. One entry dated today moved {amount} {currency} from {ob} to {p}, and your older stock now "
        f"sits on {p}. Total inventory and retained earnings are unchanged.")


async def open_inventory_origins(session: AsyncSession, company_id, user_id=None) -> bool:
    """When Accounting is first turned on for a company Celerp built itself, the stock it
    already holds is opening stock: each such lot records the opening inventory account
    and the opening inventory entry books it, in one savepoint. Stock from elsewhere
    (``_foreign``) records nothing and waits for the user. A period lock that forbids the
    entry writes nothing and leaves the company unmarked, to retry on a later start.
    Returns whether the company was marked."""
    from celerp.services.auto_je import upsert_opening_inventory_je

    async with session.begin_nested():
        opening, retained = AccountRole.INVENTORY_OPENING.value, AccountRole.RETAINED_EARNINGS.value
        settings, codes = await _locked(session, company_id, [opening, retained])
        pending = unrecorded(await _items(session, company_id))
        if codes is None or not pending or await _foreign(session, company_id, settings):
            await _mark(session, company_id)
            return True
        if not await _period_open(session, company_id, str(date.today())):
            return False
        for row in sorted(pending, key=lambda r: r.entity_id):
            await _record(session, company_id, row.entity_id, codes[opening], "accounting turned on", None)
        await upsert_opening_inventory_je(session, company_id=company_id, user_id=user_id)
        await _mark(session, company_id)
    return True


async def choose_lot_account(session: AsyncSession, company_id, item_id: str, code: str, actor_id) -> None:
    """Record the inventory account the user picked for older stock left unplaced (module
    docstring). It must be an account that has held purchased or opening inventory, and
    it must hold the lot's value beyond the stock already recorded on it; otherwise the
    books themselves need reconciling, and nothing is moved to make the pick fit."""
    from celerp.services.company_lock import lock_chart, locked_company
    from celerp.services.journal_accounts import lock_accounts

    code = (code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="Choose an account.")
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    await lock_chart(session, company_id)
    settings = dict(company.settings or {})
    if not any(code in scope_codes(settings, role) for role in _INVENTORY):
        raise HTTPException(status_code=422, detail=(
            f"Account {code} has never held inventory, so older stock cannot be in it."))
    accounts = await lock_accounts(session, company_id, {code})
    if accounts is None:
        raise HTTPException(status_code=409, detail="Posting accounts need the accounting module.")
    if code not in accounts:
        raise HTTPException(status_code=422, detail=f"Account {code} is not in the chart of accounts.")
    row = await session.get(Projection, {"company_id": company_id, "entity_id": item_id},
                            with_for_update=True, populate_existing=True)
    if row is None or row.entity_type != "item":
        raise HTTPException(status_code=404, detail="Item not found.")
    if (row.state or {}).get(LOT_ACCOUNT_FIELD):
        raise HTTPException(status_code=409, detail="This stock already records its inventory account.")
    currency = settings.get("currency", "USD")
    value = round_money(held_value(row) or Decimal("0"), currency)
    room = round_money(_room(await _posted_entries(session, company_id), await _items(session, company_id), code),
                       currency)
    if value > room:
        raise HTTPException(status_code=422, detail=(
            f"Account {code} does not hold this stock's value of {value}: beyond the stock already "
            f"recorded on it, it holds {max(room, Decimal('0'))}. If no inventory account holds it, "
            f"the books need reconciling before this stock can be placed."))
    await _record(session, company_id, item_id, code, "chosen", actor_id)
