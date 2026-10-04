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
    PostingRoleError,
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

# Inventory types that are goods the company holds: finished stock and the components
# it makes things from, both bought onto and used off the inventory accounts.
STOCK_TYPES = frozenset({"stocked", "component"})


def is_stock_type(state: dict | None) -> bool:
    """Whether an item is of a STOCK_TYPES type (an item that names none is stocked)."""
    return ((state or {}).get("inventory_type") or "stocked") in STOCK_TYPES


def _owned_stock(row: Projection) -> bool:
    """Whether a lot is goods of the company's own (STOCK_TYPES), whatever its status."""
    s = row.state or {}
    return (s.get("consignment_flag") != "in" and row.consignment_flag != "in"
            and is_stock_type(s))


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
    return recorded_value(s)


def recorded_value(state: dict) -> Decimal:
    """The value a lot's state records: its cost total, else its unit cost times its
    quantity, else nothing (0)."""
    s = state or {}
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


def _balance(entries: list[tuple[str, dict]], code: str) -> Decimal:
    return sum((Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
                for _, e in entries if e.get("account") == code), Decimal("0"))


async def _balances(session: AsyncSession, company_id, codes) -> dict[str, Decimal]:
    entries = await _posted_entries(session, company_id)
    return {code: _balance(entries, code) for code in codes}


def _room(entries: list[tuple[str, dict]], items: list[Projection], code: str) -> Decimal:
    """What ``code`` holds beyond the value of the lots on hand that record it."""
    balance = _balance(entries, code)
    recorded = sum((held_value(r) or Decimal("0") for r in items if (r.state or {}).get(LOT_ACCOUNT_FIELD) == code),
                   Decimal("0"))
    return balance - recorded


async def account_room(session: AsyncSession, company_id, code: str) -> Decimal:
    """What account ``code`` holds beyond the stock on hand recorded on it, in money."""
    return (await account_rooms(session, company_id, {code}))[code]


async def account_rooms(session: AsyncSession, company_id, codes) -> dict[str, Decimal]:
    """account_room for each of ``codes``, read once."""
    currency = (await current_settings(session, company_id)).get("currency", "USD")
    entries, items = await _posted_entries(session, company_id), await _items(session, company_id)
    return {code: round_money(_room(entries, items, code), currency) for code in codes}


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


def ever_became_stock(events) -> bool:
    """Whether an item's history, as (event_type, data) pairs, shows it was ever stock:
    any status other than draft it was created in, set to or edited to, its books
    recorded, or any event beyond authoring (a movement, a document, a count)."""
    for event_type, data in events:
        data = data or {}
        if event_type == RECORDED or not is_authoring_event(event_type):
            return True
        if event_type == "item.status.set":
            status = data.get("new_status")
        else:
            change = (data.get("fields_changed") or {}).get("status")
            status = change.get("new") if isinstance(change, dict) else data.get("status")
        if status and str(status).lower() != "draft":
            return True
    return False


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


async def books_from_elsewhere(session: AsyncSession, company_id, settings: dict) -> bool:
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


async def period_open(session: AsyncSession, company_id, day: str) -> bool:
    from celerp.events.engine import _check_period_lock

    try:
        await _check_period_lock(session, company_id, {"ts": day})
    except HTTPException as exc:
        if exc.status_code == 422:
            return False
        raise
    return True


def _retired_by_user(entry: LedgerEntry) -> bool:
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


async def _older_retired_stock(session: AsyncSession, company_id,
                               pending: list[Projection]) -> list[tuple[Projection, Decimal]]:
    """The lots among ``pending`` an older release archived or expired at the user's
    request (``_retired_by_user``) with nothing moving them since, each with the value it
    would hold were it still the company's stock. Older releases never recorded whether
    the company kept such stock, so these are only candidates: the books decide
    (normalize_legacy_inventory_origins). Read only; replays each lot's own events."""
    from celerp.projections.engine import ProjectionEngine

    found = []
    for row in sorted(pending, key=lambda r: r.entity_id):
        if str((row.state or {}).get("status") or "").lower() not in RETIRED:
            continue
        entries = (await session.execute(select(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "item",
            LedgerEntry.entity_id == row.entity_id).order_by(LedgerEntry.id))).scalars().all()
        state: dict = {}
        for e in entries:
            state = ProjectionEngine._apply(state, e.event_type,
                                            {**e.data, ON_BOOKS_FIELD: True} if _retired_by_user(e) else e.data)
        value = held_value(SimpleNamespace(state=state, consignment_flag=row.consignment_flag))
        if state.get(ON_BOOKS_FIELD) and value:
            found.append((row, value))
    return found


async def in_production(session: AsyncSession, company_id) -> Decimal:
    """Stock an older release issued to production runs that are still open: it has left
    the shelf, but those releases booked its value off the inventory accounts only when the
    run completed, so the books still carry it (each module's inventory_in_production slot)."""
    from celerp.modules.slots import get, resolve_handler

    total = Decimal("0")
    for handler in sorted({c["handler"] for c in get("inventory_in_production")}):  # each module counted once
        total += await resolve_handler(handler)(session=session, company_id=company_id)
    return total


async def consumed_facts(session: AsyncSession, company_id, marker: str,
                         owners: set[str]) -> dict[str, dict[str, tuple[float, Decimal]]]:
    """Per owner, what each lot gave up to the item.consumed events marked ``marker`` ==
    owner: the quantity that left it and the value, in money, that left with it. Each is what
    the lot held just before such an event less what it held just after, replayed from the
    lot's own events, so a request beyond the stock on hand counts only what was there.
    History, never today's stock or costs."""
    from celerp.projections.engine import ProjectionEngine

    currency = (await current_settings(session, company_id)).get("currency", "USD")
    consumed = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "item",
        LedgerEntry.event_type == "item.consumed"))).scalars().all()
    lots = sorted({e.entity_id for e in consumed if (e.metadata_ or {}).get(marker) in owners})
    found: dict[str, dict[str, tuple[float, Decimal]]] = {o: {} for o in owners}
    for lot in lots:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": lot})
        flag = row.consignment_flag if row is not None else None
        state: dict = {}
        for e in (await session.execute(select(LedgerEntry).where(
                LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "item",
                LedgerEntry.entity_id == lot).order_by(LedgerEntry.id))).scalars():
            before = state
            state = ProjectionEngine._apply(state, e.event_type, e.data)
            owner = (e.metadata_ or {}).get(marker)
            if e.event_type == "item.consumed" and owner in owners:
                held = [held_value(SimpleNamespace(state=s, consignment_flag=flag)) for s in (before, state)]
                left = float(before.get("quantity") or 0) - float(state.get("quantity") or 0)
                moved = round_money(held[0] or 0, currency) - round_money(held[1] or 0, currency)
                qty, value = found[owner].get(lot, (0.0, Decimal("0")))
                found[owner][lot] = (round(qty + left, 9), value + moved)
    return found


class _Retry(Exception):
    """A period lock forbids the upgrade's writes: roll the company's savepoint back and
    retry on a later start."""


async def normalize_legacy_inventory_origins(session: AsyncSession, company_id) -> bool:
    """Give the older stock of a company Celerp built itself the inventory account it sits
    in (module docstring), all in one savepoint. When P and OB together hold less than V
    (below), the opening inventory entry is first brought current, as an older release did
    only when the balance sheet was opened. The purchased (P) and opening (OB)
    inventory accounts must both take entries, and together hold exactly the stock on
    hand plus what older releases issued to production runs still open (V,
    ``in_production``); then one entry dated the company's business day moves OB, beyond the stock
    recording OB, into P, every older lot that has held stock records P, on hand or not,
    and the company is marked upgraded. An older draft holds no stock, so it counts
    toward neither V nor the proof and records nothing. Retained earnings, cost
    of sales, total inventory and older documents are untouched.

    Older releases archived and expired lots without recording whether the company kept
    the stock. Such a lot (``_older_retired_stock``) is recognized as still holding its
    stock only when P and OB carry exactly V plus its value, in an event a rebuild
    replays; when they carry V alone it stays off the books. No entry puts value back.

    When the books cannot vouch for the stock, nothing moves and the company is still
    marked, leaving each older lot that has held stock for the user to place. A period
    lock, or a posting account the opening entry cannot use, that forbids a write rolls the whole savepoint back and leaves the company
    unmarked, to retry on a later start. Running it again changes nothing. Returns
    whether the company was marked."""
    try:
        async with session.begin_nested():
            return await _normalize(session, company_id, None)
    except _Retry:
        return False


async def _normalize(session: AsyncSession, company_id, user_id) -> bool:
    from celerp.events.engine import emit_event
    from celerp.services.auto_je import _emit_auto_posted_je, _line, book_opening_inventory

    purchased, opening = AccountRole.INVENTORY_PURCHASED.value, AccountRole.INVENTORY_OPENING.value
    settings, codes = await _locked(session, company_id, [purchased, opening])
    if INVENTORY_ORIGIN_KEY in settings:
        return True  # another start upgraded the company while this one waited for the lock
    items = await _items(session, company_id)
    pending = _legacy(items)
    if codes is None or not pending or await books_from_elsewhere(session, company_id, settings):
        await _mark(session, company_id)
        return True
    p, ob = codes[purchased], codes[opening]
    currency = settings.get("currency", "USD")
    held = [(r, held_value(r)) for r in items if held_value(r) is not None]
    if any((r.state or {}).get(LOT_ACCOUNT_FIELD) not in (None, "", p, ob) for r, _ in held):
        await _mark(session, company_id)
        return True
    production = await in_production(session, company_id)
    value = round_money(sum((v for _, v in held), Decimal("0")) + production, currency)
    balance = await _balances(session, company_id, (p, ob))
    if round_money(balance[p] + balance[ob], currency) < value:
        # an older release brought its opening inventory entry current only when the
        # balance sheet was opened: book what it would have, then compare
        try:
            await book_opening_inventory(session, company_id=company_id, user_id=user_id, in_production=production)
        except PostingRoleError as exc:
            raise _Retry from exc
        except HTTPException as exc:
            if exc.status_code == 422:
                raise _Retry from exc
            raise
        balance = await _balances(session, company_id, (p, ob))
    books = round_money(balance[p] + balance[ob], currency)
    day = business_date_of(None, settings.get("timezone"))
    kept: list[Projection] = []
    if books != value:
        retired = await _older_retired_stock(session, company_id, pending)
        if not retired or books != round_money(value + sum((v for _, v in retired), Decimal("0")), currency):
            await _mark(session, company_id)
            return True
        kept = [r for r, _ in retired]
    on_opening = sum((v for r, v in held if (r.state or {}).get(LOT_ACCOUNT_FIELD) == ob), Decimal("0"))
    moved = round_money(balance[ob] - on_opening, currency)
    je_id = f"je:auto:inventory-origin:{company_id}"
    if moved and await session.get(Projection, {"company_id": company_id, "entity_id": je_id}) is not None:
        await _mark(session, company_id)  # moved once already; the books have changed since
        return True
    if (moved or kept) and not await period_open(session, company_id, day):
        raise _Retry
    for row in kept:
        await emit_event(session, company_id=company_id, entity_id=row.entity_id, entity_type="item",
                         event_type=KEPT, data={}, actor_id=None, location_id=None, source="system",
                         idempotency_key=f"kept-stock:{row.entity_id}", metadata_={})
    if moved:
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
    one account. Stock from elsewhere (``books_from_elsewhere``) records nothing and waits for the
    user. A period lock that forbids the entry writes nothing and leaves the company
    unmarked, to retry on a later start. Returns whether the company was marked."""
    try:
        async with session.begin_nested():
            return await _open(session, company_id, user_id)
    except _Retry:
        return False


async def _open(session: AsyncSession, company_id, user_id) -> bool:
    from celerp.services.auto_je import book_opening_inventory

    opening, retained = AccountRole.INVENTORY_OPENING.value, AccountRole.RETAINED_EARNINGS.value
    settings, codes = await _locked(session, company_id, [opening, retained])
    if INVENTORY_ORIGIN_KEY in settings:
        return True  # another start opened the company while this one waited for the lock
    pending = _legacy(await _items(session, company_id))
    if codes is None or not pending or await books_from_elsewhere(session, company_id, settings):
        await _mark(session, company_id)
        return True
    if not await period_open(session, company_id, business_date_of(None, settings.get("timezone"))):
        raise _Retry
    inventory = {code for role in _INVENTORY for code in scope_codes(settings, role)}
    if any(e.get("account") in inventory for _, e in await _posted_entries(session, company_id)):
        await _normalize(session, company_id, user_id)
        return True
    for row in sorted(pending, key=lambda r: r.entity_id):
        await _record(session, company_id, row.entity_id, codes[opening], "accounting turned on", None)
    await book_opening_inventory(session, company_id=company_id, user_id=user_id, in_production=Decimal("0"))
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
    code = before.get(LOT_ACCOUNT_FIELD) if made_available else lot_account(before)
    if not code:
        opening = AccountRole.INVENTORY_OPENING.value
        code = (await resolve_many(session, entry.company_id, [opening]))[opening]
    return DraftBoundary(made_available=made_available, value=value, code=code,
                         record=not before.get(LOT_ACCOUNT_FIELD) and made_available,
                         day=await entry_day(session, entry.company_id, (entry.data or {}).get("ts")))


async def book_draft_boundary(session: AsyncSession, entry: LedgerEntry, move: DraftBoundary) -> None:
    """Record and book a lot's move between draft and stock (draft_boundary), in the
    same transaction as the event that moved it."""
    if move.record:
        await _record(session, entry.company_id, entry.entity_id, move.code, "made available", entry.actor_id)
    kind = "made-available" if move.made_available else "returned-to-draft"
    await post_opening_stock_delta(
        session, entry.company_id, {move.code: move.value}, onto_books=move.made_available,
        je_id=f"je:auto:{entry.entity_id}:{kind}:{entry.id}", idem=f"draft-stock:{entry.id}",
        memo="Opening stock made available" if move.made_available else "Opening stock returned to draft",
        metadata={"trigger": f"item.{kind}"}, actor_id=entry.actor_id, day=move.day)


async def post_opening_stock_delta(session: AsyncSession, company_id, values: dict[str, Decimal], *,
                                   onto_books: bool, je_id: str, idem: str, memo: str, metadata: dict,
                                   actor_id, day: str) -> None:
    """Book opening stock onto the books (``onto_books``) or take it off them: the value
    on each inventory account in ``values`` against retained earnings, in one entry dated
    ``day`` and keyed by ``idem``, so a retried operation books nothing more. Each value
    is rounded to the company currency; nothing is posted when they all round to zero.
    The retained earnings account is checked as any new entry's is
    (account_roles.resolve_many)."""
    from celerp.services.auto_je import _emit_auto_posted_je, _line, _lot_line

    settings = await current_settings(session, company_id)
    currency = settings.get("currency", "USD")
    rounded = {code: round_money(v, currency) for code, v in sorted(values.items())}
    values = {code: v for code, v in rounded.items() if v}
    if not values:
        return
    retained = AccountRole.RETAINED_EARNINGS.value
    total = float(sum(values.values()))
    re_code = (await resolve_many(session, company_id, [retained]))[retained]
    if onto_books:
        lines = [*(_lot_line(settings, code, debit=float(v)) for code, v in values.items()),
                 _line(re_code, retained, credit=total)]
    else:
        lines = [_line(re_code, retained, debit=total),
                 *(_lot_line(settings, code, credit=float(v)) for code, v in values.items())]
    await _emit_auto_posted_je(
        session, company_id=company_id, user_id=actor_id, je_id=je_id,
        idem_create=f"{idem}:c", idem_posted=f"{idem}:p", memo=memo, entries=lines, metadata_=metadata, ts=day)


# Writers that book or move a lot's value themselves, read off the event: a merge,
# split or transform moving value between lots on the same account, a document's goods
# movement, a count and its undo, a production run, and a migration carrying its source
# books. Their own entries carry the value; every other change to it, a consumption no
# production run marks included, is booked by value_boundary.
_SELF_BOOKED_REASONS = frozenset({"from_merge", "from_split", "from_transform", "split_parent", "audit", "audit_undo"})
_SELF_BOOKED_SOURCES = frozenset({"migration", "audit", "fulfillment", "fulfill_split", "receive_undo"})
_SELF_BOOKED_MARKERS = frozenset({"source_doc", "source_return", "source_receive_undo", "split_for_fulfillment",
                                  "audit_id", "manufacturing_order_id"})


def _self_booked(entry: LedgerEntry) -> bool:
    if entry.source in _SELF_BOOKED_SOURCES:
        return True
    for marks in (entry.data or {}, entry.metadata_ or {}):
        if marks.get("reason") in _SELF_BOOKED_REASONS or _SELF_BOOKED_MARKERS & marks.keys():
            return True
    return False


def _held(state: dict) -> Decimal | None:
    return held_value(SimpleNamespace(state=state, consignment_flag=state.get("consignment_flag")))


@dataclass(frozen=True)
class ValueChange:
    """A change in the value a lot holds on its inventory account (value_boundary)."""

    delta: Decimal
    code: str
    day: str


async def value_boundary(session: AsyncSession, entry: LedgerEntry, transition: Transition) -> ValueChange | None:
    """Whether an applied item event changed the value a lot on hand holds on the
    inventory account it recorded, read from the transition its row lock applied
    (ProjectionEngine.apply_event). Every writer passes here, so a cost edit, a quantity
    change, a price set, a restated cost carried into a merge result or a store re-import
    is booked in the same transaction; writers that book or move the value themselves
    (_self_booked) are left to their own entries. While the lot holds booked stock,
    nothing may change whether it is the company's own (its inventory type, a
    consignment): that is refused, never booked. With Accounting off, nothing is booked."""
    from celerp.services.auto_je import entry_day

    before, after = transition.before, transition.after
    code = (before or {}).get(LOT_ACCOUNT_FIELD)
    if not code or not in_stock(before) or not in_stock(after):
        return None
    if _owned_stock(SimpleNamespace(state=before, consignment_flag=None)) != _owned_stock(
            SimpleNamespace(state=after, consignment_flag=None)):
        raise HTTPException(
            status_code=422,
            detail=f"This item holds stock booked to inventory account {code}, so its inventory type and "
                   "consignment cannot change. Sell, write off or return the stock to draft first.")
    if _self_booked(entry):
        return None
    hb, ha = _held(before), _held(after)
    if hb is None or ha is None:
        return None
    settings = await current_settings(session, entry.company_id)
    if SCHEMA_KEY not in settings:
        return None
    currency = settings.get("currency", "USD")
    delta = round_money(ha, currency) - round_money(hb, currency)
    if not delta:
        return None
    return ValueChange(delta=delta, code=code,
                       day=await entry_day(session, entry.company_id, (entry.data or {}).get("ts")))


async def book_value_change(session: AsyncSession, entry: LedgerEntry, change: ValueChange) -> None:
    """Book a lot's change in value (value_boundary) on its inventory account: an
    increase against stock gains, a decrease against stock shrinkage, keyed by the event
    so a retry books nothing more. The account the other side posts to is checked as any
    new entry's is (account_roles.resolve_many); a refusal rolls the event back."""
    from celerp.services.auto_je import _emit_auto_posted_je, _line, _lot_line

    settings = await current_settings(session, entry.company_id)
    amount = float(abs(change.delta))
    if change.delta > 0:
        role = AccountRole.STOCK_GAIN.value
        other = (await resolve_many(session, entry.company_id, [role]))[role]
        lines = [_lot_line(settings, change.code, debit=amount), _line(other, role, credit=amount)]
        memo = "Stock value increased"
    else:
        role = AccountRole.STOCK_SHRINKAGE.value
        other = (await resolve_many(session, entry.company_id, [role]))[role]
        lines = [_line(other, role, debit=amount), _lot_line(settings, change.code, credit=amount)]
        memo = "Stock value decreased"
    await _emit_auto_posted_je(
        session, company_id=entry.company_id, user_id=entry.actor_id,
        je_id=f"je:auto:{entry.entity_id}:value-changed:{entry.id}",
        idem_create=f"lot-value:{entry.id}:c", idem_posted=f"lot-value:{entry.id}:p", memo=memo, entries=lines,
        metadata_={"trigger": "item.value-changed", "event": entry.event_type}, ts=change.day)


async def recognize_opening_lots(session: AsyncSession, company_id, item_ids, actor_id, operation_id: str,
                                 at=None) -> None:
    """Book the stock one operation brought in with no purchase behind it (an import, a
    store sync, the sample items) as opening stock, in the operation's own transaction.
    Each of the named lots that is the company's own, holds stock and records no account
    yet records the opening inventory account in use now; their value is booked there
    against retained earnings in one entry for the operation, dated the business day of
    ``at`` or today. A lot with no cost records its account and books no money. Both
    accounts are checked before anything is written, so a refusal leaves the operation
    to roll back whole. With Accounting off nothing is recorded or booked: the stock is
    opening stock when Accounting is turned on (open_inventory_origins). The entry is
    keyed by the operation and the lots it booked, so a retry books nothing more."""
    import hashlib

    from celerp.services.auto_je import entry_day
    from celerp.services.company_lock import locked_company

    ids = sorted(set(item_ids))
    if not ids:
        return
    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    if SCHEMA_KEY not in settings:
        return
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item", Projection.entity_id.in_(ids))
        .order_by(Projection.entity_id).with_for_update().execution_options(populate_existing=True))).scalars()
    lots = [r for r in rows if held_value(r) is not None and not (r.state or {}).get(LOT_ACCOUNT_FIELD)]
    if not lots:
        return
    value = sum((held_value(r) for r in lots), Decimal("0"))
    opening, retained = AccountRole.INVENTORY_OPENING.value, AccountRole.RETAINED_EARNINGS.value
    accounts = await resolve_many(session, company_id, [opening, retained])
    day = await entry_day(session, company_id, at)
    for lot in lots:
        await _record(session, company_id, lot.entity_id, accounts[opening], "opening stock", actor_id)
    digest = hashlib.sha256("\n".join(lot.entity_id for lot in lots).encode()).hexdigest()[:16]
    key = f"opening-stock:{operation_id}:{digest}"
    await post_opening_stock_delta(
        session, company_id, {accounts[opening]: value}, onto_books=True, je_id=f"je:auto:{key}", idem=key,
        memo="Opening stock brought in", metadata={"trigger": "item.opening-stock", "operation": operation_id},
        actor_id=actor_id, day=day)


async def remove_opening_lots(session: AsyncSession, company_id, item_ids, actor_id, operation_id: str) -> None:
    """Take the stock of lots about to be removed without a trace (sample items nobody
    used) off the books, in the removal's own transaction: the value each one holds
    comes off the account it recorded against retained earnings, in one entry for the
    operation, so the books still equal the stock left. A lot that records no account
    has nothing booked to take off."""
    import hashlib

    from celerp.services.auto_je import entry_day
    from celerp.services.company_lock import locked_company

    ids = sorted(set(item_ids))
    if not ids:
        return
    company = await locked_company(session, company_id)
    if SCHEMA_KEY not in (company.settings or {}):
        return
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item", Projection.entity_id.in_(ids))
        .order_by(Projection.entity_id).with_for_update().execution_options(populate_existing=True))).scalars()
    values: dict[str, Decimal] = {}
    for row in rows:
        value, code = held_value(row), (row.state or {}).get(LOT_ACCOUNT_FIELD)
        if value is not None and code:
            values[code] = values.get(code, Decimal("0")) + value
    digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16]
    key = f"opening-stock-removed:{operation_id}:{digest}"
    await post_opening_stock_delta(
        session, company_id, values, onto_books=False, je_id=f"je:auto:{key}", idem=key,
        memo="Opening stock removed", metadata={"trigger": "item.opening-stock-removed", "operation": operation_id},
        actor_id=actor_id, day=await entry_day(session, company_id))


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
    room = await account_room(session, company_id, code)
    if value > room:
        raise HTTPException(status_code=422, detail=(
            f"Account {code} does not hold this stock's value of {value}: beyond the stock already "
            f"recorded on it, it holds {max(room, Decimal('0'))}. If no inventory account holds it, "
            f"the books need reconciling before this stock can be placed."))
    await _record(session, company_id, item_id, code, "chosen", actor_id)
