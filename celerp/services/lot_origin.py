# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Inventory accounts for stock from before lots recorded their own.

A lot records the inventory account its value is booked into when it first takes on
stock (events.engine._record_lot_account). Stock from an older release recorded none.
Those releases booked pre-system stock to the opening inventory account and every later
goods movement, including the cost of opening stock sold, to the purchased inventory
account, so neither account alone says where a lot's value sits; together they hold all
of it. On upgrade, a company Celerp built itself has its opening inventory balance moved
into purchased inventory by one entry, and every older lot that has held stock, on hand
or not (sold, merged, archived and the rest), records purchased inventory, so a lot
brought back into stock later (a sale undone, a merge split) still records its account
(normalize_legacy_inventory_origins). Stock entered before Accounting was turned on is
opening stock, booked by the opening inventory entry; where an older release already
posted that company's goods movements, its books are upgraded the same way instead
(open_inventory_origins).

A draft is not stock: it records no inventory account and nothing is booked for it,
whatever release wrote it. Making it available records the opening inventory account in
use at that moment and books its value there against retained earnings in the same
operation; returning it to draft takes that value off again (draft_boundary). With
Accounting off the move books nothing, and the stock is opening stock when Accounting is
turned on.

Nothing else is assumed. Stock in a company whose books came from elsewhere (a
migration, a bundle import, a restored backup), or whose two accounts do not add up to
the stock on hand, records no account and refuses to move its cost
(account_roles.lot_account) until the user picks the account that carries it here
(choose_lot_account). A pick is accepted only when that account holds the lot's value
beyond the stock already recorded on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import (
    INVENTORY_ORIGIN_KEY,
    INVENTORY_ORIGIN_SCHEMA,
    KEPT_STOCK_KEY,
    LOT_ACCOUNT_FIELD,
    ON_BOOKS_FIELD,
    SCHEMA_KEY,
    SOURCE_CONTROLS_KEY,
    AccountRole,
)
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import Transition
from celerp.services.account_roles import (
    current_settings,
    lot_account,
    resolve_many,
    role_map,
    scope_codes,
    target_problems,
)
from celerp.services.business_time import business_date_of
from celerp.services.money import round_money

RECORDED = "item.inventory_account.recorded"
KEPT = "item.inventory_on_books.recorded"

# Statuses of lots whose stock is not on the books: no longer owned, or (draft) not yet
# committed.
OFF_BOOKS = frozenset({"deleted", "void", "sold", "fulfilled", "merged", "draft", "disposed"})
# Statuses that retire a lot from the catalog. Archive and Expire keep the stock on the
# books (ON_BOOKS_FIELD); a lot a split, transform, merge or undone receipt or return
# left archived gave its value up and holds none.
RETIRED = frozenset({"archived", "expired"})

_INVENTORY = (AccountRole.INVENTORY_PURCHASED.value, AccountRole.INVENTORY_OPENING.value)


def _owned_stock(row: Projection) -> bool:
    """Whether a lot is stocked goods of the company's own, whatever its status."""
    s = row.state or {}
    return (s.get("consignment_flag") != "in" and row.consignment_flag != "in"
            and (s.get("inventory_type") or "stocked") == "stocked")


def in_stock(state: dict | None) -> bool:
    """Whether a lot's status keeps its stock on the books (the ownership test is
    ``_owned_stock``)."""
    s = state or {}
    status = str(s.get("status") or "").lower()
    return status not in OFF_BOOKS and (status not in RETIRED or s.get(ON_BOOKS_FIELD) is True)


def held_value(row: Projection) -> Decimal | None:
    """The value of the goods a lot holds on the books, or None when it holds none on
    them (not owned, not committed, retired with its value given up, consigned in, or not
    stocked). The one definition the books, the dashboard and the opening entry use."""
    s = row.state or {}
    if not in_stock(s) or not _owned_stock(row):
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


def _draft(state: dict) -> bool:
    return str(state.get("status") or "").lower() == "draft"


# Item events that author a lot without moving it: none of them means the item has
# circulated. item.file.* events are authoring too (is_authoring_event).
AUTHORING_EVENT_TYPES: frozenset[str] = frozenset({
    "item.created", "item.updated", "item.patched", "item.pricing.set",
    "item.status.set", "item.recipe.set", "item.workflow.set",
    RECORDED,
    "shop.sync.enabled", "shop.sync.disabled",
})


def is_authoring_event(event_type: str) -> bool:
    return event_type in AUTHORING_EVENT_TYPES or event_type.startswith("item.file.")


def assert_draft_not_circulated(event_type: str, transition: Transition) -> None:
    """A draft is not stock: only authoring may touch it, and it leaves draft only by
    becoming available. Checked on the state the row lock applied the event to, so a
    reservation, fulfilment or any other movement that read the lot as available before
    it was returned to draft is refused, not applied to the draft."""
    before = transition.before
    if before is None or not _draft(before):
        return
    if is_authoring_event(event_type) and str(transition.after.get("status") or "").lower() in ("draft", "available"):
        return
    raise HTTPException(status_code=409, detail="This item is a draft, not stock yet: make it available first.")


def _legacy(items: list[Projection]) -> list[Projection]:
    """Every lot of the company's own stock that has held stock and records no inventory
    account, on hand or not: a sold, merged or archived lot can come back into stock and
    must then know its account. A draft has never held stock and records its account
    when it is made available (draft_boundary)."""
    return [r for r in items
            if not (r.state or {}).get(LOT_ACCOUNT_FIELD) and _owned_stock(r) and not _draft(r.state or {})]


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
    """Mark the company upgraded."""
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), INVENTORY_ORIGIN_KEY: INVENTORY_ORIGIN_SCHEMA}
    await session.flush()


async def _period_open(session: AsyncSession, company_id, day: str) -> bool:
    from celerp.events.engine import _check_period_lock

    try:
        await _check_period_lock(session, company_id, {"ts": day})
    except HTTPException as exc:
        if exc.status_code == 422:
            return False
        raise
    return True


def _kept_by_user(entry: LedgerEntry) -> bool:
    """Whether an older release's event retired a lot at the user's request: Archive or
    Expire, or an edit to either status. Every other writer that leaves a lot archived
    says why (a split, transform or undone receipt or return), merges and write-offs have
    their own events, and an import or snapshot authors the lot rather than retiring it."""
    data, why = entry.data or {}, (entry.metadata_ or {}).get("reason")
    if entry.event_type == "item.expired":
        return True
    if entry.event_type == "item.status.set":
        return str(data.get("new_status") or "").lower() in RETIRED and not data.get("reason") and not why
    if entry.event_type == "item.updated":
        change = (data.get("fields_changed") or {}).get("status")
        return isinstance(change, dict) and str(change.get("new") or "").lower() in RETIRED
    return False


async def record_kept_stock(session: AsyncSession, company_id) -> None:
    """Older releases archived and expired lots without saying whether the stock stayed
    the company's. Each archived or expired lot is replayed from its own events, reading
    every Archive and Expire the user made as keeping the stock (``_kept_by_user``), and a
    lot that ends up holding its stock records so in an event, which a rebuild replays.
    Runs once per company; the company is marked when done."""
    from celerp.events.engine import emit_event
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import locked_company

    async with session.begin_nested():
        company = await locked_company(session, company_id)
        if company is None or KEPT_STOCK_KEY in (company.settings or {}):
            return
        retired = sorted((r for r in await _items(session, company_id)
                          if str((r.state or {}).get("status") or "").lower() in RETIRED
                          and not (r.state or {}).get(ON_BOOKS_FIELD)), key=lambda r: r.entity_id)
        for row in retired:
            entries = (await session.execute(select(LedgerEntry).where(
                LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "item",
                LedgerEntry.entity_id == row.entity_id)
                .order_by(LedgerEntry.id))).scalars().all()
            state: dict = {}
            for e in entries:
                data = {**e.data, ON_BOOKS_FIELD: True} if _kept_by_user(e) else e.data
                state = ProjectionEngine._apply(state, e.event_type, data)
            if state.get(ON_BOOKS_FIELD):
                await emit_event(session, company_id=company_id, entity_id=row.entity_id, entity_type="item",
                                 event_type=KEPT, data={}, actor_id=None, location_id=None, source="system",
                                 idempotency_key=f"kept-stock:{row.entity_id}", metadata_={})
        company.settings = {**(company.settings or {}), KEPT_STOCK_KEY: 1}
        await session.flush()


def _kept_value(items: list[Projection]) -> Decimal:
    """The value of the archived and expired stock the company keeps."""
    return sum((held_value(r) or Decimal("0") for r in items
                if str((r.state or {}).get("status") or "").lower() in RETIRED), Decimal("0"))


async def normalize_legacy_inventory_origins(session: AsyncSession, company_id) -> bool:
    """Give the older stock of a company Celerp built itself the inventory account it sits
    in (module docstring), all in one savepoint. The purchased (P) and opening (OB)
    inventory accounts must both take entries, and together hold exactly the stock on
    hand (V); then one entry dated the company's business day moves OB, beyond the stock
    recording OB, into P, every older lot that has held stock records P, on hand or not,
    and the company is marked upgraded. An older draft holds no stock, so it counts
    toward neither V nor the proof and records nothing. Retained earnings, cost
    of sales, total inventory and older documents are untouched. When the books cannot
    vouch for the stock, nothing moves and the company is still marked, leaving each older
    lot that has held stock for the user to place. A period lock that forbids the entry writes nothing and
    leaves the company unmarked, to retry on a later start. Running it again changes
    nothing. Returns whether the company was marked."""
    from celerp.services.auto_je import _emit_auto_posted_je, _line

    async with session.begin_nested():
        purchased, opening = AccountRole.INVENTORY_PURCHASED.value, AccountRole.INVENTORY_OPENING.value
        settings, codes = await _locked(session, company_id, [purchased, opening])
        items = await _items(session, company_id)
        pending = _legacy(items)
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
        day = business_date_of(None, settings.get("timezone"))
        if round_money(balance[p] + balance[ob], currency) != round_money(value, currency):
            kept = await _put_back_kept_stock(session, company_id, settings, items, balance[p] + balance[ob], value,
                                              ob, day)
            if kept is None:
                await _mark(session, company_id)
                return True
            if kept is False:
                return False
            balance[ob] += kept
        on_opening = sum((v for r, v in held if (r.state or {}).get(LOT_ACCOUNT_FIELD) == ob), Decimal("0"))
        moved = round_money(balance[ob] - on_opening, currency)
        je_id = f"je:auto:inventory-origin:{company_id}"
        if moved:
            if await session.get(Projection, {"company_id": company_id, "entity_id": je_id}) is not None:
                await _mark(session, company_id)  # moved once already; the books have changed since
                return True
            if not await _period_open(session, company_id, day):
                return False
            amount, (debit, credit) = abs(moved), ((p, purchased), (ob, opening)) if moved > 0 else ((ob, opening), (p, purchased))
            await _emit_auto_posted_je(
                session, company_id=company_id, user_id=None, je_id=je_id,
                idem_create=f"inventory-origin:{company_id}:c", idem_posted=f"inventory-origin:{company_id}:p",
                memo="Opening inventory moved into purchased inventory, where older releases booked its sales",
                entries=[_line(debit[0], debit[1], debit=float(amount)), _line(credit[0], credit[1], credit=float(amount))],
                metadata_={"trigger": "inventory_origin.normalized"}, ts=day)
        for row in sorted(pending, key=lambda r: r.entity_id):
            await _record(session, company_id, row.entity_id, p, "normalized", None)
        await _mark(session, company_id)
        if moved:
            await _notify_moved(session, company_id, p, ob, abs(moved), currency, day)
    return True


async def _put_back_kept_stock(session: AsyncSession, company_id, settings: dict, items: list[Projection],
                               books: Decimal, value: Decimal, ob: str, day: str) -> Decimal | bool | None:
    """Older releases recomputed the opening inventory entry without archived and expired
    stock, so archiving or expiring a lot took its value off the books though the company
    still owned it (record_kept_stock). When the purchased and opening inventory accounts
    (``books``) fall short of the stock on hand (``value``) by exactly the archived and
    expired stock the company keeps, one entry dated ``day`` puts that value back on the
    opening inventory account against retained earnings, where the opening entry had
    taken it from. Returns the amount put back; None when the books cannot vouch for it
    (the shortfall is anything else, or retained earnings cannot take the entry), so
    nothing is posted and the stock is left for the user to place; False when a period
    lock forbids the entry, to retry on a later start."""
    from celerp.services.account_roles import PostingRoleError
    from celerp.services.auto_je import _emit_auto_posted_je, _line

    currency = settings.get("currency", "USD")
    kept = round_money(_kept_value(items), currency)
    if not kept or round_money(books + kept, currency) != round_money(value, currency):
        return None
    je_id = f"je:auto:kept-stock:{company_id}"
    if await session.get(Projection, {"company_id": company_id, "entity_id": je_id}) is not None:
        return None  # put back once already; the books have changed since
    retained = AccountRole.RETAINED_EARNINGS.value
    try:
        code = (await resolve_many(session, company_id, [retained]))[retained]
    except PostingRoleError:
        return None
    if not await _period_open(session, company_id, day):
        return False
    await _emit_auto_posted_je(
        session, company_id=company_id, user_id=None, je_id=je_id,
        idem_create=f"kept-stock:{company_id}:c", idem_posted=f"kept-stock:{company_id}:p",
        memo="Archived and expired stock the company still owns, put back on the books",
        entries=[_line(ob, AccountRole.INVENTORY_OPENING.value, debit=float(kept)),
                 _line(code, retained, credit=float(kept))],
        metadata_={"trigger": "inventory_origin.kept_stock"}, ts=day)
    from celerp.notifications import service as notification_service

    await notification_service.create(
        session, company_id, "accounting", "Archived stock put back on the books",
        f"Earlier releases left archived and expired stock out of opening inventory, though it is still yours. "
        f"One entry dated {day} put {kept} {currency} back on {ob} against {code}. Archive and Expire now keep "
        f"stock on the books; use Write off stock to take it off.")
    return kept


async def _notify_moved(session: AsyncSession, company_id, p: str, ob: str, amount: Decimal, currency: str,
                        day: str) -> None:
    from celerp.notifications import service as notification_service

    await notification_service.create(
        session, company_id, "accounting", "Older stock moved to purchased inventory",
        f"Older releases booked the cost of opening stock sold to {p}, so {p} and {ob} only matched your stock "
        f"together. One entry dated {day} moved {amount} {currency} from {ob} to {p}, and your older stock now "
        f"sits on {p}. Total inventory and retained earnings are unchanged.")


async def open_inventory_origins(session: AsyncSession, company_id, user_id=None) -> bool:
    """When Accounting is first turned on for a company Celerp built itself, its older
    stock is opening stock: each such lot, on hand or not, records the opening inventory
    account and the opening inventory entry books the stock on hand, in one savepoint.
    Anything that stops the entry (posting accounts, a period lock on an earlier opening
    entry) fails the whole savepoint, so no lot records an account its value never reached.
    Older releases posted goods movements to purchased inventory even without a chart of
    accounts; a company whose books already carry them gets the opening entry for the
    stock those books never booked, and is then upgraded like any older company
    (normalize_legacy_inventory_origins), so its earlier documents and its lots agree on
    one account. Stock from elsewhere (``_foreign``) records nothing and waits for the
    user. A period lock that forbids the entry writes nothing and leaves the company
    unmarked, to retry on a later start. Returns whether the company was marked."""
    from celerp.services.auto_je import book_opening_inventory

    async with session.begin_nested():
        opening, retained = AccountRole.INVENTORY_OPENING.value, AccountRole.RETAINED_EARNINGS.value
        settings, codes = await _locked(session, company_id, [opening, retained])
        pending = _legacy(await _items(session, company_id))
        if codes is None or not pending or await _foreign(session, company_id, settings):
            await _mark(session, company_id)
            return True
        if not await _period_open(session, company_id, business_date_of(None, settings.get("timezone"))):
            return False
        inventory = {code for role in _INVENTORY for code in scope_codes(settings, role)}
        if any(e.get("account") in inventory for _, e in await _posted_entries(session, company_id)):
            await book_opening_inventory(session, company_id=company_id, user_id=user_id)
            return await normalize_legacy_inventory_origins(session, company_id)
        for row in sorted(pending, key=lambda r: r.entity_id):
            await _record(session, company_id, row.entity_id, codes[opening], "accounting turned on", None)
        await book_opening_inventory(session, company_id=company_id, user_id=user_id)
        await _mark(session, company_id)
    return True


@dataclass(frozen=True)
class DraftBoundary:
    """A lot moving between draft and stock: the value it brings onto the books (made
    available) or takes off them (returned to draft), on its inventory account."""

    made_available: bool
    value: Decimal
    code: str
    record: bool  # the lot records ``code`` as it is made available
    retained: str | None
    settings: dict
    day: str


async def draft_boundary(session: AsyncSession, entry: LedgerEntry, transition: Transition) -> DraftBoundary | None:
    """Whether an applied item event moved a lot across the line between draft and stock,
    read from the transition the row lock applied it under (ProjectionEngine.apply_event),
    so two requests racing to move the same lot see each other's move and only one books
    it. Made available, the lot keeps the account it recorded before or takes the opening
    inventory account in use now, and its value is booked there against retained
    earnings; returned to draft, its value comes off the account it recorded. Every
    account is checked as any new entry's is (account_roles.resolve_many), and the entry
    is dated the business day the operation recorded (``ts``) or today. The period lock is
    the event's own (events.engine). With Accounting off, nothing is booked. Anything
    refused here rolls the event back with it."""
    from celerp.services.auto_je import entry_day

    before, after = transition.before, transition.after
    if before is None or _draft(before) == _draft(after):
        return None
    settings = await current_settings(session, entry.company_id)
    if SCHEMA_KEY not in settings:
        return None
    made_available = _draft(before)
    state = after if made_available else before
    value = held_value(SimpleNamespace(state=state, consignment_flag=state.get("consignment_flag")))
    if value is None:
        return None
    value = round_money(value, settings.get("currency", "USD"))
    code = before.get(LOT_ACCOUNT_FIELD) if made_available else lot_account(before)
    opening, retained = AccountRole.INVENTORY_OPENING.value, AccountRole.RETAINED_EARNINGS.value
    roles = ([] if code else [opening]) + ([retained] if value else [])
    accounts = await resolve_many(session, entry.company_id, roles) if roles else {}
    return DraftBoundary(made_available=made_available, value=value, code=code or accounts[opening],
                         record=not code, retained=accounts.get(retained), settings=settings,
                         day=await entry_day(session, entry.company_id, (entry.data or {}).get("ts")))


async def book_draft_boundary(session: AsyncSession, entry: LedgerEntry, move: DraftBoundary) -> None:
    """Record and book a lot's move between draft and stock (draft_boundary), in the
    same transaction as the event that moved it."""
    from celerp.services.auto_je import _emit_auto_posted_je, _line, _lot_line

    if move.record:
        await _record(session, entry.company_id, entry.entity_id, move.code, "made available", entry.actor_id)
    if not move.value:
        return
    amount, retained = float(move.value), AccountRole.RETAINED_EARNINGS.value
    if move.made_available:
        kind, memo = "made-available", "Opening stock made available"
        lines = [_lot_line(move.settings, move.code, debit=amount), _line(move.retained, retained, credit=amount)]
    else:
        kind, memo = "returned-to-draft", "Opening stock returned to draft"
        lines = [_line(move.retained, retained, debit=amount), _lot_line(move.settings, move.code, credit=amount)]
    await _emit_auto_posted_je(
        session, company_id=entry.company_id, user_id=entry.actor_id,
        je_id=f"je:auto:{entry.entity_id}:{kind}:{entry.id}",
        idem_create=f"draft-stock:{entry.id}:c", idem_posted=f"draft-stock:{entry.id}:p",
        memo=memo, entries=lines, metadata_={"trigger": f"item.{kind}"}, ts=move.day)


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
