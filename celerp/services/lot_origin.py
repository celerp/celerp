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

Stock the books cannot vouch for this way (a company whose books came from elsewhere,
such as a migration, a bundle import or a restored backup, or whose two accounts do not
add up to the stock) is placed as it arrives, every lot of it, on one default inventory
account, against what the inventory accounts hold together: value split across them is
moved onto the default account, value they fall short of is recognized as opening
stock, and value they hold beyond the stock is recorded, unresolved, for the
accountant; one placement record keeps all of it and the company is told
(place_older_stock). Every inventory account then holds the lots recorded on it, less
the cost invoices recognized and have not shipped, plus the difference recorded on it.
Every start and every writer that brings such stock in while Celerp runs (a batch
import of another system's items, a restored backup, a finalized migration) places it
the same way (place_company_stock), as does turning Accounting on and any request
refused only because the stock it needs is not placed yet, which then runs again
(place_unplaced_stock). The user can move placed lots to another inventory
account in bulk, each move booking its value across (move_lot_accounts), undo a move
(undo_lot_move), and correct how a placement treated its difference (correct_placements).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import (
    ACCOUNTING_SETTINGS_PATH,
    INVENTORY_ORIGIN_KEY,
    INVENTORY_ORIGIN_SCHEMA,
    LOT_ACCOUNT_FIELD,
    ON_BOOKS_FIELD,
    PERIOD_LOCK_PATH,
    POSTING_ACCOUNTS_PATH,
    SCHEMA_KEY,
    SOURCE_CONTROLS_KEY,
    AccountRole,
    needs_accounting,
    no_account_chosen,
    refusal,
)
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import Transition
from celerp.services.account_roles import (
    PostingRoleError,
    continue_role,
    current_settings,
    lot_account,
    resolve_many,
    role_map,
    scope_codes,
    scope_list,
    stock_line,
    target_problems,
    uncertain_stock_line,
)
from celerp.services.business_time import business_date_at, business_date_of
from celerp.services.money import round_money

logger = logging.getLogger(__name__)

RECORDED = "item.inventory_account.recorded"
MOVED = "item.inventory_account.moved"
KEPT = "item.inventory_on_books.recorded"

OLDER_STOCK_PATH = f"{ACCOUNTING_SETTINGS_PATH}?tab=older-stock"
# The top of the Older stock tab, where it says whether the stock is reconciled.
RECONCILIATION_PATH = f"{OLDER_STOCK_PATH}#older-stock-reconciliation"
PLACED_BY = "placed automatically"
MAX_MOVE = 10000

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


def ready_to_ship(state: dict | None) -> bool:
    """Whether a lot's goods are in stock and free to ship: goods of a stock type
    (is_stock_type), on the books, and neither archived, expired, nor out with a customer
    on memo."""
    s = state or {}
    status = str(s.get("status") or "").lower()
    return is_stock_type(s) and in_stock(s) and status not in RETIRED and status != "memo_out"


def units_leaving(before: dict | None, after: dict | None) -> float:
    """How many of a lot's units a change takes out of what is ready to ship (ready_to_ship):
    all of them when the lot stops being ready, else what its quantity falls by."""
    if not before or not ready_to_ship(before):
        return 0.0
    held = float(before.get("quantity") or 0)
    if not ready_to_ship(after):
        return held
    return max(0.0, held - float((after or {}).get("quantity") or 0))


def held_value(row: Projection) -> Decimal | None:
    """The value of the goods a lot holds on the books, or None when it holds none on
    them (not owned, not committed, retired with its value given up, consigned in, or not
    stocked). The one definition the books, the dashboard and the opening entry use."""
    s = row.state or {}
    if not in_stock(s) or not _owned_stock(row):
        return None
    return recorded_value(s)


def booked_value(row: Projection, currency: str) -> Decimal:
    """What a lot's account carries for it: its held value rounded to the currency, the
    amount every posting moves per lot. Totals over lots sum these, never the raw values,
    so the stock recorded on an account matches its balance to the cent."""
    return round_money(held_value(row) or Decimal("0"), currency)


def recorded_value(state: dict) -> Decimal:
    """The value a lot's state records: its cost total, else its unit cost times its
    quantity, else nothing (0)."""
    s = state or {}
    if float(s.get("cost_total") or 0):
        return Decimal(str(s["cost_total"]))
    cost = s.get("cost_price") or s.get("cost price")
    return Decimal(str(cost)) * Decimal(str(s.get("quantity") or 0)) if cost is not None else Decimal("0")


def value_moved(before: dict, after: dict | None, currency: str) -> Decimal:
    """What a lot's account gives up when the lot goes from ``before`` to ``after`` (None when
    the lot leaves whole): the value it recorded less what it records now, each rounded to the
    cent as the books carry every lot. Removals that book their own entry (a write-off, an
    audit count) post this, so the books still carry the lot to the cent; negative when the
    lot gains value."""
    left = recorded_value(after) if after is not None else Decimal("0")
    return round_money(recorded_value(before), currency) - round_money(left, currency)


def _in_stock_row():
    """in_stock as a condition on an item row, from the same statuses, so a read skips
    every lot whose stock left the books (sold, merged, retired with its value given up)."""
    status = func.lower(func.coalesce(Projection.state["status"].as_string(), ""))
    return and_(status.not_in(OFF_BOOKS),
                or_(status.not_in(RETIRED), Projection.state[ON_BOOKS_FIELD].as_string() == "true"))


async def _items(session: AsyncSession, company_id, *where) -> list[Projection]:
    """Every item row (meeting ``where``) as the database holds it now, never as an
    earlier read loaded it."""
    return list((await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item", *where)
        .execution_options(populate_existing=True))).scalars())


async def _held_items(session: AsyncSession, company_id) -> list[Projection]:
    """The item rows whose stock is on the books (in_stock), filtered in the read: every
    lot holding value (held_value) is among them, and a lot sold long ago is never read."""
    return [r for r in await _items(session, company_id, _in_stock_row()) if in_stock(r.state)]


async def _posted_entries(session: AsyncSession, company_id, ids=None) -> list[tuple[str, dict]]:
    """The lines of every posted journal entry, or of those among ``ids``."""
    query = select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "journal_entry")
    if ids is not None:
        query = query.where(Projection.entity_id.in_(list(ids)))
    rows = (await session.execute(query)).scalars()
    return [(row.entity_id, entry) for row in rows if (row.state or {}).get("status") == "posted"
            for entry in row.state.get("entries") or []]


def _balance(entries: list[tuple[str, dict]], code: str) -> Decimal:
    return sum((Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
                for _, e in entries if e.get("account") == code), Decimal("0"))


async def account_balances(session: AsyncSession, company_id, codes) -> dict[str, Decimal]:
    """The posted balance (debit less credit) of each of ``codes``, read once."""
    entries = await _posted_entries(session, company_id)
    return {code: _balance(entries, code) for code in codes}


def _stock_balance(settings: dict, entries: list[tuple[str, dict]], code: str) -> Decimal:
    """The balance of ``code`` from the lines posted to it as stock (account_roles.
    stock_line) only: cash, deposits, input tax or anything else on the same account is
    not stock value and never counts towards it."""
    return _balance([(je, e) for je, e in entries if stock_line(settings, e)], code)


def _uncertain_lines(settings: dict, entries: list[tuple[str, dict]], codes) -> list[tuple[str, dict]]:
    """The lines on ``codes`` that may have been stock but cannot be shown to
    (account_roles.uncertain_stock_line)."""
    return [(je, e) for je, e in entries if e.get("account") in codes and uncertain_stock_line(settings, e)]


def uncertain_evidence(settings: dict, entries: list[tuple[str, dict]], codes, currency: str) -> list[dict]:
    """The uncertain lines (_uncertain_lines) on each of ``codes``, in that order: the
    account, their balance and the entries holding them. Evidence for the accountant to
    review: what they were is not known, so they prove nothing about the stock either way."""
    found = []
    for code in codes:
        lines = _uncertain_lines(settings, entries, {code})
        if lines:
            found.append({"account": code, "amount": float(round_money(_balance(lines, code), currency)),
                          "entries": sorted({je for je, _ in lines})})
    return found


# The postings that record a payment, by the trigger kept on the entry when it was
# posted: their lines moved cash, whatever roles their accounts served.
PAYMENT_TRIGGER = "doc.payment."


async def evidence_entries(session: AsyncSession, company_id, settings: dict,
                           entries: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """``entries`` less every entry posted as a payment (PAYMENT_TRIGGER), read in one
    query: what each was posted for is recorded with it, so a payment's lines are never
    read as possible stock (_uncertain_lines) or explain a difference. An account being a
    bank account today proves nothing of what an older line on it was for: any other
    line that may have been stock stays evidence to review."""
    ids = sorted({je for je, e in entries if uncertain_stock_line(settings, e)})
    if not ids:
        return entries
    paid = set((await session.execute(select(LedgerEntry.entity_id).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == "acc.journal_entry.created",
        LedgerEntry.entity_id.in_(ids),
        LedgerEntry.metadata_["trigger"].as_string().startswith(PAYMENT_TRIGGER)))).scalars())
    return [(je, e) for je, e in entries if je not in paid]


def unexplained(settings: dict, entries: list[tuple[str, dict]], codes, gap: Decimal) -> Decimal:
    """The part of ``gap`` (what ``codes`` hold as stock beyond the stock recorded on them,
    negative when short) that no reading of their uncertain lines (_uncertain_lines) can
    explain. Each such line may or may not have been stock, so the true gap lies anywhere
    from ``gap`` plus every credit among them to ``gap`` plus every debit: a range reaching
    zero leaves no difference, and otherwise only the end nearest zero is one. The one rule
    placing (_place) and the books check (stock_off_books) both read a difference by."""
    moves = [Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
             for _, e in _uncertain_lines(settings, entries, set(codes))]
    low = gap + sum((m for m in moves if m < 0), Decimal("0"))
    high = gap + sum((m for m in moves if m > 0), Decimal("0"))
    return high if high < 0 else low if low > 0 else Decimal("0")


def stock_accounts(settings: dict, entries: list[tuple[str, dict]]) -> list[str]:
    """Every account a line was posted to as stock (account_roles.stock_line), whatever
    role it serves now or whether it can still take entries, in first-posted order."""
    return list(dict.fromkeys(e["account"] for _, e in entries if e.get("account") and stock_line(settings, e)))


async def stock_balances(session: AsyncSession, company_id, settings: dict, codes) -> dict[str, Decimal]:
    """_stock_balance of each of ``codes``, read once."""
    entries = await _posted_entries(session, company_id)
    return {code: _stock_balance(settings, entries, code) for code in codes}


async def stock_holders(session: AsyncSession, company_id, settings: dict, codes: set[str]) -> set[str]:
    """Which of ``codes`` still hold stock: lines posted as stock leave a balance on the
    account (_stock_balance), or a lot holding value records it."""
    currency = settings.get("currency", "USD")
    entries = await _posted_entries(session, company_id)
    lots = {(r.state or {}).get(LOT_ACCOUNT_FIELD) for r in await _held_items(session, company_id) if held_value(r)}
    return {code for code in codes
            if code in lots or round_money(_stock_balance(settings, entries, code), currency)}


def _room(settings: dict, entries: list[tuple[str, dict]], items: list[Projection], code: str, currency: str,
          sold: dict[str, float], unresolved: dict[str, Decimal]) -> Decimal:
    """What ``code`` holds as stock (_stock_balance) beyond the value of the lots on hand
    that record it, less what finalized invoices already recognized for the ones they
    have not shipped (``sold``, from auto_je.recognized_unshipped) and the difference a
    placement left on it for the accountant (``unresolved``, from unresolved_differences)."""
    balance = _stock_balance(settings, entries, code)
    recorded = sum((booked_value(r, currency) for r in items if (r.state or {}).get(LOT_ACCOUNT_FIELD) == code),
                   Decimal("0"))
    return (balance - recorded + round_money(sold.get(code, 0), currency)
            - unresolved.get(code, Decimal("0")))


async def account_room(session: AsyncSession, company_id, code: str) -> Decimal:
    """What account ``code`` holds beyond the stock on hand recorded on it, in money."""
    return (await account_rooms(session, company_id, {code}))[code]


async def account_rooms(session: AsyncSession, company_id, codes) -> dict[str, Decimal]:
    """account_room for each of ``codes``, read once."""
    from celerp.services.auto_je import recognized_unshipped

    settings = await current_settings(session, company_id)
    currency = settings.get("currency", "USD")
    entries, items = await _posted_entries(session, company_id), await _held_items(session, company_id)
    sold, unresolved = await recognized_unshipped(session, company_id), await unresolved_differences(session, company_id)
    return {code: round_money(_room(settings, entries, items, code, currency, sold, unresolved), currency)
            for code in codes}


def awaits_account(row: Projection) -> bool:
    """Whether a lot is older stock waiting for its inventory account: on hand, holding
    value on the books, and recording no account. A lot holding nothing (a draft, sold,
    or an older lot used up in production) carries nothing to place."""
    return not (row.state or {}).get(LOT_ACCOUNT_FIELD) and bool(held_value(row))


def unrecorded(items: list[Projection]) -> list[Projection]:
    """The lots waiting for their inventory account (``awaits_account``)."""
    return [r for r in items if awaits_account(r)]


async def stock_off_books(session: AsyncSession, company_id, items: list[Projection] | None = None) -> list[dict]:
    """What the books check reports about stock: each lot on hand holding value that
    records no inventory account, each account lots are carried on whose balance differs
    from the stock on hand recorded on it beyond what a placement recorded there and what
    its uncertain lines could explain (unexplained), those uncertain lines as evidence to
    review (uncertain_evidence), and each difference a placement recorded, unresolved, for
    the accountant (_place). ``items`` are the lots in stock (_held_items) when the caller
    has already read them. Read only; nothing is booked to close a gap."""
    from celerp.services.auto_je import recognized_unshipped

    settings = await current_settings(session, company_id)
    if SCHEMA_KEY not in settings:
        return []
    currency = settings.get("currency", "USD")
    entries = await _posted_entries(session, company_id)
    items = await _held_items(session, company_id) if items is None else items
    sold, unresolved = await recognized_unshipped(session, company_id), await unresolved_differences(session, company_id)
    findings = [{"kind": "unplaced_lot", "entity_id": r.entity_id, "sku": (r.state or {}).get("sku")}
                for r in sorted(unrecorded(items), key=lambda r: r.entity_id)]
    codes = sorted({code for role in _INVENTORY for code in scope_codes(settings, role)})
    evidence = await evidence_entries(session, company_id, settings, entries)
    for code in codes:
        room = round_money(_room(settings, entries, items, code, currency, sold, unresolved), currency)
        if unexplained(settings, evidence, {code}, room):
            books = round_money(_stock_balance(settings, entries, code), currency)
            findings.append({"kind": "stock_gap", "account": code, "books": float(books),
                             "stock": float(books - room - unresolved.get(code, Decimal("0")))})
    findings += [{"kind": "uncertain_evidence", **found}
                 for found in uncertain_evidence(settings, evidence, codes, currency)]
    findings += [{"kind": "unresolved_difference", "account": code, "amount": float(amount)}
                 for code, amount in sorted(unresolved.items())]
    return findings


# A draft deleted while other records still name it: hidden, used by nothing, and
# brought back as a draft by Restore.
DELETED = "deleted"


def _draft(state: dict) -> bool:
    return str(state.get("status") or "").lower() == "draft"


def is_deleted(state: dict) -> bool:
    return str(state.get("status") or "").lower() == DELETED


def refuse_deleted(state: dict, ref: str = "") -> None:
    """The one refusal for using a deleted item (409), naming the fix. Names the item by
    its SKU, or by ``ref`` (its id) when it has none."""
    if is_deleted(state):
        sku = state.get("sku") or ref or state.get("entity_id") or ""
        raise HTTPException(status_code=409, detail=refusal(
            "item.deleted", f"{sku} was deleted: restore it from the Deleted list to use it.", sku=sku))


def refuse_draft(state: dict, ref: str = "") -> None:
    """The one refusal for moving a draft lot (409): a draft is not stock until it is made
    available. Names the lot by its SKU, or by ``ref`` (its id) when it has none. A
    deleted lot is refused as deleted (refuse_deleted)."""
    refuse_deleted(state, ref)
    if _draft(state):
        sku = state.get("sku") or ref or state.get("entity_id") or ""
        raise HTTPException(status_code=409, detail=refusal(
            "item.draft", f"{sku} is a draft, not stock yet: make it available first.", sku=sku))


# Item events that author a lot without moving it: none of them means the item has
# circulated. item.file.* events are authoring too (is_authoring_event).
AUTHORING_EVENT_TYPES: frozenset[str] = frozenset({
    "item.created", "item.updated", "item.patched", "item.pricing.set",
    "item.status.set", "item.recipe.set", "item.workflow.set",
    RECORDED, MOVED,
    "shop.sync.enabled", "shop.sync.disabled",
})


def is_authoring_event(event_type: str) -> bool:
    return event_type in AUTHORING_EVENT_TYPES or event_type.startswith("item.file.")


def ever_became_stock(events) -> bool:
    """Whether an item's history, as (event_type, data) pairs, shows it was ever stock:
    any status other than draft it was created in, set to or edited to, its books
    recorded, or any event beyond authoring (a movement, a document, a count). Deleted
    is a draft's own status, not stock."""
    for event_type, data in events:
        data = data or {}
        if event_type == RECORDED or not is_authoring_event(event_type):
            return True
        if event_type == "item.status.set":
            status = data.get("new_status")
        else:
            change = (data.get("fields_changed") or {}).get("status")
            status = change.get("new") if isinstance(change, dict) else data.get("status")
        if status and str(status).lower() not in ("draft", DELETED):
            return True
    return False


def assert_draft_not_circulated(event_type: str, transition: Transition) -> None:
    """A draft is not stock: only authoring may touch it, and it leaves draft only by
    becoming available. Checked on the state the row lock applied the event to, so a
    reservation, fulfilment or any other movement that read the lot as available before
    it was returned to draft is refused, not applied to the draft. A draft may also be
    deleted, and a deleted item takes nothing but its Restore, back to draft."""
    before = transition.before
    if before is None:
        return
    after = str(transition.after.get("status") or "").lower()
    if is_deleted(before):
        if event_type == "item.status.set" and after == "draft":
            return
        refuse_deleted(before)
    if not _draft(before):
        return
    if is_authoring_event(event_type) and after in ("draft", "available", DELETED):
        return
    refuse_draft(before)


def _legacy(items: list[Projection]) -> list[Projection]:
    """Every lot of the company's own stock that has held stock and records no inventory
    account, on hand or not: a sold, merged or archived lot can come back into stock and
    must then know its account. A draft has never held stock and records its account
    when it is made available (draft_boundary)."""
    return [r for r in items
            if not (r.state or {}).get(LOT_ACCOUNT_FIELD) and _owned_stock(r) and not _draft(r.state or {})]


def _record_key(item_id: str) -> str:
    """The idempotency key of the event recording a lot's inventory account: once per lot."""
    return f"lot-account:{item_id}"


async def _record(session: AsyncSession, company_id, item_id: str, code: str, why: str, actor_id,
                  metadata: dict | None = None) -> None:
    from celerp.events.engine import emit_event

    await emit_event(session, company_id=company_id, entity_id=item_id, entity_type="item", event_type=RECORDED,
                     data={LOT_ACCOUNT_FIELD: code}, actor_id=actor_id, location_id=None, source="system",
                     idempotency_key=_record_key(item_id), metadata_={"recorded_by": why, **(metadata or {})})


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
    """Mark the company upgraded, then place any older stock still recording no inventory
    account (_place)."""
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), INVENTORY_ORIGIN_KEY: INVENTORY_ORIGIN_SCHEMA}
    await session.flush()
    await _place(session, company_id)


def _notice(base: str, body: str = "body", **params) -> tuple[str, str, dict]:
    """A notice's English title and body from the catalog keys ``base.title`` and
    ``base.<body>``, and the i18n record each reader's language is shown from; a
    ``problems`` param (refusals) reads in English as their messages."""
    from ui.i18n import t

    keys = {"title": f"{base}.title", "body": f"{base}.{body}"}
    english = {name: " ".join(r["message"] for r in v) if name == "problems" else v for name, v in params.items()}
    return t(keys["title"], "en"), t(keys["body"], "en", **english), {**keys, "params": params}


UNMAPPED_NOTICE = "notice.older_stock_unmapped"


async def _notify_unmapped(session: AsyncSession, company_id, variant: str, problems: list[dict]) -> None:
    """Older stock waits because no inventory account can take it (``body_inventory``),
    an inventory account that cannot take entries still holds value placing must move
    off it (``body_held``), or retained earnings cannot take the adjustment it needs
    (``body_retained``);
    ``problems`` are the refusals (target_problem) naming each account and what is wrong
    with it. Told once while it stands, kept current, and removed once the stock is
    placed (_place)."""
    from celerp.notifications import service as notification_service

    title, body, i18n = _notice(UNMAPPED_NOTICE, variant, problems=problems)
    await notification_service.notify_standing(session, company_id, "accounting", title, body,
                                               action_url=POSTING_ACCOUNTS_PATH, i18n=i18n)


async def _notify_placed(session: AsyncSession, company_id, count: int) -> None:
    """One plain notice for the person selling. It links those who manage accounting to
    where the placement's reconciliation stands, with the accounts, amounts and journal
    entries below it (older_stock_view); anyone else is shown it with no link
    (notifications.service.link_for)."""
    from celerp.notifications import service as notification_service

    title, body, i18n = _notice("notice.older_stock_placed", "body", count=count)
    await notification_service.create(session, company_id, "accounting", title, body,
                                      action_url=RECONCILIATION_PATH, priority="high", i18n=i18n)


FAILED_NOTICE = "notice.older_stock_failed"


async def _notify_failed(session: AsyncSession, company_id, exc: Exception) -> None:
    """Placing older stock failed for a reason no setting explains. Nothing was written;
    the company is told, kept current while it stands, and the notice is removed once
    the stock is placed (_place). The notice names the kind of error for a report
    (failed_placement), never its text, which can carry company data. It is written in
    the caller's transaction, so it stands exactly when what asked for the placement
    commits."""
    from celerp.notifications import service as notification_service

    title, body, i18n = _notice(FAILED_NOTICE, error=type(exc).__name__)
    await notification_service.notify_standing(session, company_id, "accounting", title, body,
                                               action_url=OLDER_STOCK_PATH, i18n=i18n)


async def _standing_notice(session: AsyncSession, company_id, base: str) -> dict | None:
    """The i18n record of the company's standing notice ``base`` (notify_standing) with the
    page it links to (``action_url``), or None when none stands. A standing notice is
    removed once its condition ends, so one that stands, read or not, means the condition
    holds now."""
    from celerp.models.notification import Notification

    row = (await session.execute(select(Notification.i18n, Notification.action_url).where(
        Notification.company_id == company_id, Notification.category == "accounting",
        Notification.title == _notice(base)[0]).limit(1))).first()
    return None if row is None else {**(row.i18n or {}), "action_url": row.action_url}


async def failed_placement(session: AsyncSession, company_id) -> dict | None:
    """The standing failure to place older stock (_notify_failed): the kind of error, or
    None when none stands."""
    i18n = await _standing_notice(session, company_id, FAILED_NOTICE)
    return {"error": (i18n.get("params") or {}).get("error") or ""} if i18n is not None else None


async def _recorded(session: AsyncSession, company_id, item_ids: list[str]) -> set[str]:
    """Those of ``item_ids`` whose inventory account is already recorded by an event
    (_record), whatever their stored state shows: recording one again writes nothing."""
    if not item_ids:
        return set()
    return set((await session.execute(select(LedgerEntry.entity_id).where(
        LedgerEntry.company_id == company_id,
        LedgerEntry.idempotency_key.in_([_record_key(i) for i in item_ids])))).scalars())


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _placement_id(company_id, item_ids: list[str]) -> str:
    return _hash("\n".join([str(company_id), *item_ids]))


PLACEMENT = "older_stock_placement"
PLACED = "sys.older_stock.placed"
CORRECTED = "sys.older_stock.corrected"
# What a placement record holds per placement, summed per account in the Older stock summary.
_PLACED_AMOUNTS = ("transferred", "recognized", "unresolved")


def _placement_entity(pid: str) -> str:
    return f"older-stock-placement:{pid}"


def _placement_entry(pid: str, part: str) -> str:
    return f"je:auto:older-stock-placed:{pid}:{part}"


async def unresolved_differences(session: AsyncSession, company_id) -> dict[str, Decimal]:
    """What placements recorded, per inventory account, as held beyond the stock and
    left for the accountant to resolve (``_place``), summed in one grouped read."""
    code, amount = Projection.state["account"].as_string(), Projection.state["unresolved"].as_numeric(28, 10)
    rows = (await session.execute(select(code, func.sum(amount)).where(
        Projection.company_id == company_id, Projection.entity_type == PLACEMENT, amount != 0).group_by(code))).all()
    return {account: total.normalize() for account, total in rows if total}


def _classify(moved: bool, gap: Decimal, provisional: Decimal, uncertain: bool) -> str:
    """How a placement met the books. Never ``exact`` (nor ``transfer``) while cost an
    invoice recognized on the lots is provisional: no entry says which account it left,
    nor while lines that may have been stock (``uncertain``) leave the match unproven."""
    if gap < 0:
        return "opening_stock"
    if gap > 0:
        return "unresolved"
    if provisional:
        return "provisional"
    if uncertain:
        return "uncertain"
    return "transfer" if moved else "exact"


async def _place(session: AsyncSession, company_id) -> bool:
    """Place every older lot of the company's own stock that records no inventory account
    (``_legacy``) on one default account, under the caller's lock on the company, its
    chart and its lots, everything in the caller's transaction.

    The lots' value V is compared with what the inventory accounts hold together beyond
    the stock recorded on them (``_room``, which counts what invoices recognized and have
    not shipped): the usable current ones (purchased inventory, and opening inventory
    when it can take entries and is not the same account) and every account either role
    has served before (scope_list), which still holds what was booked to it. The
    default account is a current one: the one holding exactly V, else the one holding
    most, earlier in candidate order on a tie (opening inventory first when the stock is
    from elsewhere, books_from_elsewhere, or purchased inventory never took an entry),
    else, when none holds anything, purchased inventory. One entry moves what every
    other account, current or former, holds beyond its stock onto the default account,
    so no inventory is added and nothing on a former account is recognized again. Cost an
    invoice recognized on lots no allocation or entry ties to one account
    (auto_je.UNPLACED) was taken off the books already: it counts against V and is kept
    on the record as ``provisional``. What the
    accounts together hold short of V is recognized as opening stock, the way opening
    stock is booked (post_opening_stock_delta); what they hold beyond V is written off
    nowhere and is recorded on the default account as an unresolved difference. Lines
    on these accounts, other than a payment's (evidence_entries), that may have been
    stock but cannot be shown to (account_roles.uncertain_stock_line) prove nothing either
    way: only the part of the difference no reading of them can explain (unexplained) is
    recognized or left unresolved, nothing is ever booked for them, the default account is
    chosen counting them as held, and they are shown to the accountant as evidence read
    from the posted entries whenever asked (stock_off_books). A
    placement record (sys.older_stock.placed) keeps the accounts' rooms, the value
    transferred, recognized and left unresolved, and the entries, so each inventory
    account holds its lots, less unshipped recognized cost, plus its unresolved
    difference. The company is told (_notify_placed) and the dashboard shows it until
    dismissed (``older_stock_placed``).

    The default account is chosen only among the current inventory accounts that can
    take entries; every account lines were posted to as stock (stock_accounts) is
    counted, usable or not, so what it holds is never recognized again. Nothing is
    placed, and the company is told which account to fix and why (_notify_unmapped),
    when neither current inventory account can take entries, when one that cannot still
    holds value that must be moved off it, or when retained earnings cannot take entries
    while opening stock must be recognized. Placing records each lot on today's date, so a period-locked
    today raises _Locked. A lot whose placement is already recorded (_recorded) is not
    placed again. Returns whether lots were placed."""
    from celerp.events.engine import emit_event
    from celerp.notifications import service as notification_service
    from celerp.services.auto_je import UNPLACED, _emit_auto_posted_je, _lot_line, recognized_unshipped
    from celerp.services.company_lock import locked_company
    from celerp.services.journal_accounts import lock_accounts

    await notification_service.mark_done(session, company_id, PERIOD_LOCK_PATH)
    items = await _items(session, company_id)
    pending = sorted(_legacy(items), key=lambda r: r.entity_id)
    done = await _recorded(session, company_id, [r.entity_id for r in pending])
    pending = [r for r in pending if r.entity_id not in done]
    if not pending:
        return False
    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    purchased, opening = AccountRole.INVENTORY_PURCHASED.value, AccountRole.INVENTORY_OPENING.value
    retained = AccountRole.RETAINED_EARNINGS.value
    roles = [purchased, opening, retained]
    current = role_map(settings)
    scope = list(dict.fromkeys(c for role in _INVENTORY for c in scope_list(settings, role)))
    entries = await _posted_entries(session, company_id)
    held = list(dict.fromkeys([*scope, *stock_accounts(settings, entries)]))
    accounts = await lock_accounts(session, company_id, {current[r] for r in roles if current.get(r)} | set(held))
    if accounts is None:
        return False  # Accounting is not running: nothing can be booked or placed
    problems = target_problems(roles, current, accounts)
    p, ob = current.get(purchased), current.get(opening)
    usable = list(dict.fromkeys(current[r] for r in (purchased, opening) if r not in problems))
    if not usable:
        await _notify_unmapped(session, company_id, "body_inventory", [problems[purchased], problems[opening]])
        return False
    counted = [c for c in dict.fromkeys([p, ob, *held]) if c and c in accounts and c not in usable]
    opening_first = (await books_from_elsewhere(session, company_id, settings)
                     or not any(e.get("account") == p for _, e in entries))
    candidates = sorted(usable, key=lambda c: c != ob) if opening_first else usable
    currency = settings.get("currency", "USD")
    sold = await recognized_unshipped(session, company_id)
    unresolved = await unresolved_differences(session, company_id)
    room = {c: round_money(_room(settings, entries, items, c, currency, sold, unresolved), currency)
            for c in candidates + counted}
    readable = await evidence_entries(session, company_id, settings, entries)
    evidence = uncertain_evidence(settings, readable, list(room), currency)
    uncertain = {found["account"]: Decimal(str(found["amount"])) for found in evidence}
    provisional = round_money(sold.get(UNPLACED, 0), currency)
    value = round_money(sum((booked_value(r, currency) for r in pending), Decimal("0")), currency)
    owed = value - provisional  # cost already taken off the books by invoices whose entries name no one account
    most = max(candidates, key=lambda c: room[c] + uncertain.get(c, 0))
    target = (next((c for c in candidates if room[c] == owed), None)
              or (most if room[most] + uncertain.get(most, 0) > 0 else usable[0]))
    moved = {c: room[c] for c in room if c != target and room[c]}
    gap = round_money(unexplained(settings, readable, list(room), sum(room.values(), Decimal("0")) - owed), currency)
    stuck = [problems[r] for r in (purchased, opening) if r in problems and current.get(r) in moved]
    if stuck:
        await _notify_unmapped(session, company_id, "body_held", stuck)
        return False
    if gap < 0 and retained in problems:
        await _notify_unmapped(session, company_id, "body_retained", [problems[retained]])
        return False
    day = business_date_of(None, settings.get("timezone"))
    if not await period_open(session, company_id, day):
        raise _Locked
    ids = [r.entity_id for r in pending]
    pid = _placement_id(company_id, ids)
    for item_id in ids:
        await _record(session, company_id, item_id, target, PLACED_BY, None, {"placement": pid})
    journal = []
    if moved:
        transferred = sum(moved.values(), Decimal("0"))
        lines = ([_lot_line(settings, target, debit=float(transferred))] if transferred > 0 else [])
        lines += [_lot_line(settings, c, debit=float(-v)) for c, v in moved.items() if v < 0]
        lines += [_lot_line(settings, c, credit=float(v)) for c, v in moved.items() if v > 0]
        lines += [_lot_line(settings, target, credit=float(-transferred))] if transferred < 0 else []
        journal.append(_placement_entry(pid, "transfer"))
        await _emit_auto_posted_je(
            session, company_id=company_id, user_id=None, je_id=journal[-1],
            idem_create=f"older-stock-placed:{pid}:transfer:c", idem_posted=f"older-stock-placed:{pid}:transfer:p",
            memo="Older stock moved onto its inventory account", entries=lines,
            metadata_={"trigger": "older_stock.placed", "placement": pid}, ts=day)
    else:
        transferred = Decimal("0")
    if gap < 0:
        journal.append(_placement_entry(pid, "opening"))
        await post_opening_stock_delta(
            session, company_id, {target: -gap}, onto_books=True, je_id=journal[-1],
            idem=f"older-stock-placed:{pid}:opening", memo="Opening stock recognized for older stock",
            metadata={"trigger": "older_stock.placed", "placement": pid, "classification": "opening_stock"},
            actor_id=None, day=day)
    await emit_event(
        session, company_id=company_id, entity_id=_placement_entity(pid), entity_type=PLACEMENT,
        event_type=PLACED, actor_id=None, location_id=None, source="system",
        idempotency_key=_placement_entity(pid), metadata_={},
        data={"placement": pid, "day": day, "count": len(ids), "value": float(value), "account": target,
              "rooms": {c: float(v) for c, v in room.items()}, "transferred": float(transferred),
              "recognized": float(max(-gap, Decimal("0"))), "unresolved": float(max(gap, Decimal("0"))),
              "provisional": float(provisional),
              "classification": _classify(bool(moved), gap, provisional, bool(evidence)),
              "journal": journal})
    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), "older_stock_placed": pid}
    for notice in (UNMAPPED_NOTICE, FAILED_NOTICE):
        await notification_service.clear_standing(session, company_id, "accounting", _notice(notice)[0])
    await _notify_placed(session, company_id, len(ids))
    await session.flush()
    return True


async def place_older_stock(session: AsyncSession, company_id) -> bool:
    """Place a company's older stock that still records no inventory account (_place),
    on any start after the upgrade, whenever a writer brings such stock in
    (place_company_stock), and whenever a posting account or period lock it waited on
    changes.
    A company not upgraded yet is left to the upgrade (normalize_legacy_inventory_origins),
    and one with nothing to place is read once and never locked. Everything is written
    in one savepoint: a period lock holding back the adjustment rolls it back whole and
    tells the company (_notify_locked), to retry on a later start. Any other failure is
    rolled back whole, logged and told (_notify_failed), so it never fails the start or
    the posting-account change that asked for it. Returns whether lots were placed."""
    if INVENTORY_ORIGIN_KEY not in await current_settings(session, company_id):
        return False
    if not _legacy(await _items(session, company_id)):
        return False
    try:
        async with session.begin_nested():
            await _locked(session, company_id, [AccountRole.INVENTORY_PURCHASED.value])
            return await _place(session, company_id)
    except _Locked:
        await _notify_locked(session, company_id)
        return False
    except _Retry:
        return False
    except Exception as exc:
        logger.exception("Older stock of company %s was not placed; retrying on the next start", company_id)
        await _notify_failed(session, company_id, exc)
        return False


async def place_company_stock(session: AsyncSession, company_id, *, claim=frozenset(),
                              opening: bool = False) -> bool:
    """The one way a company's stock recording no inventory account is placed: at every
    start, and by every writer that brings such lots in while Celerp runs (a batch import
    of another system's items, a backup restored as a new company, a finalized
    migration). Its posting accounts are first reconciled with its chart
    (account_roles.reconcile_company, ``claim`` there), and a company left without an
    account its workflows need gets one notice pointing at the fix. Only a company
    running Accounting is placed: until then its books are not kept. An upgraded
    company has its unplaced stock placed (place_older_stock); one whose chart was
    seeded just now (``opening``) opens it (open_inventory_origins); any other is
    upgraded (normalize_legacy_inventory_origins). Period-lock and posting-account holds
    and their notices are theirs. Placing never fails the caller: a failure is rolled
    back whole, logged and told (_notify_failed), and retried on the next trigger or
    when the user tries again (place_unplaced_stock). Running it again changes nothing. Returns whether it
    placed or marked anything."""
    from celerp.modules.registry import uses_module
    from celerp.services.account_roles import reconcile_company
    from celerp.services.posting_readiness import notify_unmapped

    if await reconcile_company(session, company_id, claim):
        await notify_unmapped(session, company_id)
    settings = await current_settings(session, company_id)
    if not uses_module(settings, "celerp-accounting"):
        return False
    if INVENTORY_ORIGIN_KEY in settings:
        place = place_older_stock
    elif opening:
        place = open_inventory_origins
    else:
        place = normalize_legacy_inventory_origins
    try:
        return await place(session, company_id)
    except Exception as exc:
        logger.exception("Older stock of company %s was not placed; retrying on the next start", company_id)
        try:
            async with session.begin_nested():
                await _notify_failed(session, company_id, exc)
        except Exception:
            logger.exception("The failure to place older stock of company %s could not be told", company_id)
        return False


async def place_for_posting_account(session: AsyncSession, company_id) -> bool:
    """A posting account older stock may wait on was just set: the notice asking for it
    is withdrawn whatever placing then does (_place repeats it while an account is still
    missing), and the stock is placed (place_older_stock)."""
    from celerp.notifications import service as notification_service

    await notification_service.clear_standing(session, company_id, "accounting", _notice(UNMAPPED_NOTICE)[0])
    return await place_older_stock(session, company_id)


async def place_after_lock_change(session: AsyncSession, company_id) -> bool:
    """The period lock was just changed: once it no longer covers today the notice
    asking to move it is withdrawn, whatever placing then does (_notify_locked repeats
    it while it still covers today), and the stock is placed (place_older_stock)."""
    from celerp.notifications import service as notification_service

    tz = (await current_settings(session, company_id)).get("timezone")
    if await period_open(session, company_id, business_date_of(None, tz)):
        await notification_service.mark_done(session, company_id, PERIOD_LOCK_PATH)
    return await place_older_stock(session, company_id)


# The refusals a request meets only because older stock it needs is not placed yet:
# moving its cost (account_roles.LotOriginError) and moving it between accounts
# (move_lot_accounts).
PLACEABLE_REFUSALS = frozenset({"posting.older_stock.no_account", "older_stock.move.not_placed"})
RERUN_AFTER_PLACING = "rerun_after_placing"


def rerun_after_placing(endpoint):
    """Mark a writer that OlderStockMiddleware may run again once its stock is placed: one
    whose every effect up to such a refusal is a database write, which the refusal rolls
    back. A writer that may send a notice, write a file or call out first is not marked."""
    setattr(endpoint, RERUN_AFTER_PLACING, True)
    return endpoint


async def _unplaced(session: AsyncSession, company_id) -> list[Projection]:
    """The company's stock awaiting placement (_legacy), read as stored now and without
    loading the lots that record an account."""
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item",
        Projection.state[LOT_ACCOUNT_FIELD].as_string().is_(None)).execution_options(
            populate_existing=True))).scalars().all()
    return _legacy(list(rows))


async def place_unplaced_stock(session: AsyncSession, company_id) -> bool:
    """Place the company's stock awaiting placement the one way (place_company_stock),
    in the caller's transaction, when any still waits, and return whether none waits
    now. For a request refused only because older stock was not placed yet
    (PLACEABLE_REFUSALS, rolled back whole by celerp.middleware.OlderStockMiddleware,
    which then runs it again) and for the user trying again after a failure. The caller
    holds no lock, so placing takes the company, its accounts and its lots in that order,
    as every placement does. Another request placing the same stock at the same time
    waits on the company lock and then finds it placed (_recorded)."""
    if await _unplaced(session, company_id):
        await place_company_stock(session, company_id)
    return not await _unplaced(session, company_id)


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


LOCKED_NOTICE = "notice.older_stock_locked"


class _Retry(Exception):
    """A posting account the opening entry cannot use forbids the upgrade's writes: roll the
    company's savepoint back and retry on a later start."""


class _Locked(_Retry):
    """A period lock forbids the upgrade's writes: as _Retry, and the company is told (_notify_locked)."""


async def _notify_locked(session: AsyncSession, company_id) -> None:
    """Older stock left unplaced cannot be sold or moved until its account is known, so a
    company whose upgrade a period lock holds back is told which lock and how to go on:
    move the lock before today and the stock is placed at once (set_period_lock). One
    notice kept current with the lock date; removed when the upgrade runs (_place)."""
    from celerp.notifications import service as notification_service
    from celerp.services.company_lock import locked_company

    day = str(((await locked_company(session, company_id)).settings or {}).get("lock_date") or "")
    title, body, i18n = _notice(LOCKED_NOTICE, day=day)
    await notification_service.notify_standing(session, company_id, "accounting", title, body,
                                               action_url=PERIOD_LOCK_PATH, i18n=i18n)


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
    marked, and its older stock is placed on one default account (_place). A period
    lock, or a posting account the opening entry cannot use, that forbids a write rolls the whole savepoint back and leaves the company
    unmarked, to retry on a later start; a period lock is told to the company (_notify_locked). Running it again changes nothing. Returns
    whether the company was marked."""
    try:
        async with session.begin_nested():
            return await _normalize(session, company_id, None)
    except _Locked:
        await _notify_locked(session, company_id)
        return False
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
    held = [(r, booked_value(r, currency)) for r in items if held_value(r) is not None]
    if any((r.state or {}).get(LOT_ACCOUNT_FIELD) not in (None, "", p, ob) for r, _ in held):
        await _mark(session, company_id)
        return True
    production = await in_production(session, company_id)
    value = round_money(sum((v for _, v in held), Decimal("0")) + production, currency)
    balance = await stock_balances(session, company_id, settings, (p, ob))
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
        balance = await stock_balances(session, company_id, settings, (p, ob))
    books = round_money(balance[p] + balance[ob], currency)
    day = business_date_of(None, settings.get("timezone"))
    kept: list[Projection] = []
    if books != value:
        retired = await _older_retired_stock(session, company_id, pending)
        if not retired or books != round_money(value + sum((round_money(v, currency) for _, v in retired), Decimal("0")), currency):
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
        raise _Locked
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
        f"sits on {p}. Total inventory and retained earnings are unchanged.",
        i18n={"title": "notice.older_stock_moved.title", "body": "notice.older_stock_moved.body",
              "params": {"p": p, "ob": ob, "day": day, "amount": str(amount), "currency": currency}})


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
    unmarked and tells the company (_notify_locked), to retry on a later start. Returns whether the company was marked."""
    try:
        async with session.begin_nested():
            return await _open(session, company_id, user_id)
    except _Locked:
        await _notify_locked(session, company_id)
        return False
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
        raise _Locked
    inventory = {code for role in _INVENTORY for code in scope_codes(settings, role)}
    entries = await _posted_entries(session, company_id)
    if any(e.get("account") in inventory and stock_line(settings, e) for _, e in entries):
        await _normalize(session, company_id, user_id)
        return True
    for row in sorted(pending, key=lambda r: r.entity_id):
        await _record(session, company_id, row.entity_id, codes[opening], "accounting turned on", None)
    await book_opening_inventory(session, company_id=company_id, user_id=user_id, in_production=Decimal("0"))
    await _mark(session, company_id)
    return True


async def opening_lot_account(session: AsyncSession, company_id) -> str:
    """The inventory account a lot with no purchase behind it is booked to: the opening
    inventory account in use now, checked as any new entry's is (account_roles.resolve_many).
    The one source for opening stock, a lot made available, and an older lot that records
    no account when its value first changes."""
    opening = AccountRole.INVENTORY_OPENING.value
    return (await resolve_many(session, company_id, [opening]))[opening]


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
    it. Made available, the lot keeps the account it recorded before (which must still take
    postings, account_roles.continue_role) or takes the opening inventory account in use
    now, and its value is booked there against retained earnings; returned to draft, its value comes off the account it recorded. Every
    account is checked as any new entry's is (account_roles.resolve_many), and the entry
    is dated the business day the operation recorded (``ts``) or today. The period lock is
    the event's own (events.engine). With Accounting off, nothing is booked. Anything
    refused here rolls the event back with it. Deleting a draft and restoring it book
    nothing: neither side is stock."""
    from celerp.services.auto_je import entry_day

    before, after = transition.before, transition.after
    if before is None or _draft(before) == _draft(after) or is_deleted(before) or is_deleted(after):
        return None
    settings = await current_settings(session, entry.company_id)
    if SCHEMA_KEY not in settings:
        return None
    made_available = _draft(before)
    state = after if made_available else before
    value = held_value(SimpleNamespace(state=state, consignment_flag=state.get("consignment_flag")))
    if value is None:
        return None
    opening = AccountRole.INVENTORY_OPENING.value
    code = before.get(LOT_ACCOUNT_FIELD) if made_available else lot_account(before)
    if not code:
        code = await opening_lot_account(session, entry.company_id)
    elif made_available:
        code = await continue_role(session, entry.company_id, opening, code)
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


def self_booked(entry: LedgerEntry) -> bool:
    """Whether the writer of ``entry`` books or moves the lot's value with its own entries."""
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
    record: bool = False  # an older lot records ``code`` as its value first changes


async def value_boundary(session: AsyncSession, entry: LedgerEntry, transition: Transition) -> ValueChange | None:
    """Whether an applied item event changed the value a lot on hand holds on the
    inventory account it recorded, read from the transition its row lock applied
    (ProjectionEngine.apply_event). Every writer passes here, so a cost edit, a quantity
    change, a price set, a restated cost carried into a merge result or a store re-import
    is booked in the same transaction; writers that book or move the value themselves
    (self_booked) are left to their own entries. While the lot holds booked stock,
    nothing may change whether it is the company's own (its inventory type, a
    consignment): that is refused, never booked. An older lot that records no account
    and stays the company's own stock is booked on the account it would have been
    booked to (opening_lot_account), which it records from then on. With Accounting
    off, nothing is booked."""
    from celerp.services.auto_je import entry_day

    before, after = transition.before, transition.after
    code = (before or {}).get(LOT_ACCOUNT_FIELD)
    if before is None or not in_stock(before) or not in_stock(after):
        return None
    owned_before = _owned_stock(SimpleNamespace(state=before, consignment_flag=None))
    owned_after = _owned_stock(SimpleNamespace(state=after, consignment_flag=None))
    if not code and not (owned_before and owned_after):
        return None
    if owned_before != owned_after:
        raise HTTPException(
            status_code=422,
            detail=refusal(
                "inventory.booked_account_locks_type",
                f"This item holds stock booked to inventory account {code}, so its inventory type and "
                "consignment cannot change. Sell, write off or return the stock to draft first.",
                code=code))
    if self_booked(entry):
        return None
    if _held(before) is None or _held(after) is None:
        return None
    settings = await current_settings(session, entry.company_id)
    if SCHEMA_KEY not in settings:
        return None
    delta = -value_moved(before, after, settings.get("currency", "USD"))
    if not delta:
        return None
    return ValueChange(delta=delta, code=code or await opening_lot_account(session, entry.company_id),
                       day=await entry_day(session, entry.company_id, (entry.data or {}).get("ts")),
                       record=not code)


async def book_value_change(session: AsyncSession, entry: LedgerEntry, change: ValueChange) -> None:
    """Book a lot's change in value (value_boundary) on its inventory account, keyed by
    the event so a retry books nothing more (book_lot_value). An older lot records the
    account first."""
    if change.record:
        await _record(session, entry.company_id, entry.entity_id, change.code, "value changed", entry.actor_id)
    await book_lot_value(
        session, entry.company_id, entry.actor_id, change.code, change.delta,
        je_id=f"je:auto:{entry.entity_id}:value-changed:{entry.id}", idem=f"lot-value:{entry.id}",
        day=change.day, metadata={"trigger": "item.value-changed", "event": entry.event_type})


async def book_lot_value(session: AsyncSession, company_id, actor_id, code: str, delta: Decimal, *,
                         je_id: str, idem: str, day: str | None, metadata: dict) -> None:
    """Book ``delta`` of value on inventory account ``code``: an increase against stock
    gains, a decrease against stock shrinkage. The account the other side posts to is
    checked as any new entry's is (account_roles.resolve_many); a refusal rolls the
    caller back. With Accounting off, or a delta that rounds to nothing, nothing is booked."""
    from celerp.services.auto_je import _emit_auto_posted_je, _line, _lot_line

    settings = await current_settings(session, company_id)
    if SCHEMA_KEY not in settings:
        return
    delta = round_money(delta, settings.get("currency", "USD"))
    if not delta:
        return
    amount = float(abs(delta))
    if delta > 0:
        role = AccountRole.STOCK_GAIN.value
        other = (await resolve_many(session, company_id, [role]))[role]
        lines = [_lot_line(settings, code, debit=amount), _line(other, role, credit=amount)]
        memo = "Stock value increased"
    else:
        role = AccountRole.STOCK_SHRINKAGE.value
        other = (await resolve_many(session, company_id, [role]))[role]
        lines = [_line(other, role, debit=amount), _lot_line(settings, code, credit=amount)]
        memo = "Stock value decreased"
    await _emit_auto_posted_je(
        session, company_id=company_id, user_id=actor_id, je_id=je_id,
        idem_create=f"{idem}:c", idem_posted=f"{idem}:p", memo=memo, entries=lines, metadata_=metadata, ts=day)


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
    value = sum((booked_value(r, settings.get("currency", "USD")) for r in lots), Decimal("0"))
    code = await opening_lot_account(session, company_id)
    await resolve_many(session, company_id, [AccountRole.RETAINED_EARNINGS.value])
    day = await entry_day(session, company_id, at)
    for lot in lots:
        await _record(session, company_id, lot.entity_id, code, "opening stock", actor_id)
    digest = _hash("\n".join(lot.entity_id for lot in lots))
    key = f"opening-stock:{operation_id}:{digest}"
    await post_opening_stock_delta(
        session, company_id, {code: value}, onto_books=True, je_id=f"je:auto:{key}", idem=key,
        memo="Opening stock brought in", metadata={"trigger": "item.opening-stock", "operation": operation_id},
        actor_id=actor_id, day=day)


async def remove_opening_lots(session: AsyncSession, company_id, item_ids, actor_id, operation_id: str) -> None:
    """Take the stock of lots about to be removed without a trace (sample items nobody
    used) off the books, in the removal's own transaction: the value each one holds
    comes off the account it recorded against retained earnings, in one entry for the
    operation, so the books still equal the stock left. A lot that records no account
    has nothing booked to take off."""
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
    digest = _hash("\n".join(ids))
    key = f"opening-stock-removed:{operation_id}:{digest}"
    await post_opening_stock_delta(
        session, company_id, values, onto_books=False, je_id=f"je:auto:{key}", idem=key,
        memo="Opening stock removed", metadata={"trigger": "item.opening-stock-removed", "operation": operation_id},
        actor_id=actor_id, day=await entry_day(session, company_id))




# --- Older stock placed automatically: moved in bulk, undone, its adjustment toggled ---


def _refused(status: int, key: str, message: str, **params) -> HTTPException:
    return HTTPException(status_code=status, detail=refusal(key, message, **params))


def _sku(row: Projection) -> str:
    return (row.state or {}).get("sku") or row.entity_id


def _move_id(company_id, key: str) -> str:
    return _hash(f"{company_id}:{key}")


def _request_id(item_ids: list[str], code: str) -> str:
    """What a move asks for: its account and its lots, in any order."""
    return _hash(":".join([code, *sorted(set(item_ids))]))


def _move_je(move_id: str) -> str:
    return f"je:auto:older-stock-moved:{move_id}"


async def _move_events(session: AsyncSession, company_id, move_id: str) -> list[LedgerEntry]:
    """The lot events of move ``move_id``, one per lot it moved."""
    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == MOVED,
        LedgerEntry.idempotency_key.startswith(f"lot-move:{move_id}:", autoescape=True)).order_by(LedgerEntry.id))).scalars()
    return [r for r in rows if (r.metadata_ or {}).get("move") == move_id]


async def _je_state(session: AsyncSession, company_id, je_id: str) -> dict | None:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
    return _posted(row.state if row is not None else None)


def _posted(state: dict | None) -> dict | None:
    """A journal entry's state when it is posted, else None."""
    return state if (state or {}).get("status") == "posted" else None


def _debits(state: dict | None) -> float:
    return round(sum(float(e.get("debit") or 0) for e in (state or {}).get("entries") or []), 2)


async def _locked_lots(session: AsyncSession, company_id, item_ids) -> list[Projection]:
    """The lots ``item_ids`` locked for update in id order, as the database holds them
    now. A lot the company does not hold is refused (404)."""
    ids = sorted(set(item_ids))
    rows = list((await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item", Projection.entity_id.in_(ids))
        .order_by(Projection.entity_id).with_for_update().execution_options(populate_existing=True))).scalars())
    if len(rows) != len(ids):
        raise _refused(404, "posting.older_stock.no_item", "Item not found.")
    return rows


async def _check_target(session: AsyncSession, company_id, code: str) -> None:
    """Refuse (422) an account older stock cannot be moved onto, by the rule its account
    picker lists by (posting_readiness.older_stock_move_problem)."""
    from celerp.services.posting_readiness import older_stock_move_problem

    problem = await older_stock_move_problem(session, company_id, code)
    if problem == "not_in_chart":
        raise _refused(422, "posting.older_stock.not_in_chart", f"Account {code} is not in the chart of accounts.",
                       code=code)
    if problem == "never_inventory":
        raise _refused(422, "posting.older_stock.never_inventory",
                       f"Account {code} is not an inventory account, so stock cannot be moved onto it.", code=code)
    if problem == "cannot_take":
        raise _refused(422, "older_stock.move.cannot_take",
                       f"Account {code} is inactive or has accounts under it, so it cannot take entries.", code=code)


async def _refuse_invoiced(session: AsyncSession, company_id, rows: list[Projection]) -> None:
    """A lot a finalized invoice holds and has not shipped keeps its account: the invoice
    recognized its cost there, and shipping relieves it from there."""
    from celerp.services.auto_je import unshipped_claims

    held = {r.entity_id: r for r in rows}
    for claim in await unshipped_claims(session, company_id, costed=False, lots=sorted(held)):
        if claim.lot_id in held:
            sku = _sku(held[claim.lot_id])
            raise _refused(409, "older_stock.move.invoiced",
                           f"{sku} is on an invoice not yet shipped ({claim.doc_id}). Ship or void that invoice, "
                           "then move the lot.", sku=sku, doc=claim.doc_id)


async def _open_day(session: AsyncSession, company_id) -> str:
    """Today's business day, refused (422) while the period holding it is locked."""
    from celerp.events.engine import _check_period_lock
    from celerp.services.auto_je import entry_day

    day = await entry_day(session, company_id)
    await _check_period_lock(session, company_id, {"ts": day})
    return day


async def _move(session: AsyncSession, company_id, moves: list[tuple[Projection, str, str]], move_id: str,
                actor_id, day: str, undo_of: str | None = None, request: str | None = None) -> Decimal:
    """Move each lot (row, from account, to account) and its value: one event per lot
    and one entry, dated ``day``, debiting each account by what arrives and crediting
    each by what leaves. ``request`` (_request_id) is kept on each event. Returns the
    value moved."""
    from celerp.events.engine import emit_event
    from celerp.services.auto_je import _emit_auto_posted_je, _lot_line

    settings = await current_settings(session, company_id)
    currency = settings.get("currency", "USD")
    net: dict[str, Decimal] = {}
    total = Decimal("0")
    for row, old, new in moves:
        value = booked_value(row, currency)
        await emit_event(session, company_id=company_id, entity_id=row.entity_id, entity_type="item",
                         event_type=MOVED, data={LOT_ACCOUNT_FIELD: new, "from_account": old}, actor_id=actor_id,
                         location_id=None, source="system", idempotency_key=f"lot-move:{move_id}:{row.entity_id}",
                         metadata_={"move": move_id, **({"undo_of": undo_of} if undo_of else {}),
                                    **({"request": request} if request else {})})
        net[new], net[old] = net.get(new, Decimal("0")) + value, net.get(old, Decimal("0")) - value
        total += value
    lines = ([_lot_line(settings, c, debit=float(v)) for c, v in sorted(net.items()) if v > 0]
             + [_lot_line(settings, c, credit=float(-v)) for c, v in sorted(net.items()) if v < 0])
    if lines:
        await _emit_auto_posted_je(
            session, company_id=company_id, user_id=actor_id, je_id=_move_je(move_id),
            idem_create=f"older-stock-moved:{move_id}:c", idem_posted=f"older-stock-moved:{move_id}:p",
            memo="Older stock moved to another inventory account", entries=lines,
            metadata_={"trigger": "older_stock.moved", "move": move_id}, ts=day)
    return total


async def move_lot_accounts(session: AsyncSession, company_id, item_ids: list[str], code: str, key: str | None,
                            actor_id) -> dict:
    """Move older lots ``item_ids`` placed automatically (_place) onto inventory account
    ``code`` (_move_lots). Refused, writing nothing: no lots or too many."""
    if not item_ids:
        raise _refused(422, "older_stock.move.nothing_selected", "Select the lots to move.")
    if len(item_ids) > MAX_MOVE:
        raise _refused(422, "older_stock.move.too_many", f"Move at most {MAX_MOVE} lots at a time.", limit=MAX_MOVE)

    async def chosen() -> list[str]:
        return item_ids
    return await _move_lots(session, company_id, chosen, _request_id(item_ids, code), code, key, actor_id)


def _matching_summary(lots: list[dict]) -> dict:
    """How many lots match, their value, and the selection: a fingerprint of each lot and
    its version with the total value, which changes when any lot leaves, joins or changes."""
    value = round(sum(lot["value"] for lot in lots), 2)
    selection = _hash("\n".join([*sorted(f"{lot['item_id']}:{lot['version']}" for lot in lots), f"{value:.2f}"]))
    return {"count": len(lots), "value": value, "selection": selection}


async def move_matching_lots(session: AsyncSession, company_id, account: str | None, q: str | None,
                             selection: str, code: str, key: str | None, actor_id) -> dict:
    """Move every older lot the Older stock list shows for ``account`` and ``q``
    (older_stock_lots) onto inventory account ``code`` as one move (_move_lots), however
    many pages it spans. ``selection`` is the fingerprint of the lots the user saw
    matching (_matching_summary): when the lots matching under the company lock differ,
    refused (409) so nothing moves that was not confirmed. Refused, writing nothing: none
    shown or too many."""
    selection = (selection or "").strip()
    if not selection:
        raise _refused(422, "older_stock.move.nothing_selected", "Select the lots to move.")

    async def matching() -> list[str]:
        settings = await current_settings(session, company_id)
        lots = await older_stock_lots(session, company_id, settings.get("currency", "USD"), account, q)
        if len(lots) > MAX_MOVE:
            raise _refused(422, "older_stock.move.too_many", f"Move at most {MAX_MOVE} lots at a time.",
                           limit=MAX_MOVE)
        now = _matching_summary(lots)
        if now["selection"] != selection:
            raise _refused(409, "older_stock.move.changed",
                           f"The matching lots changed since the list was shown, so nothing was moved. "
                           f"{now['count']} lots match now. Check them and confirm again.", count=now["count"])
        return [lot["item_id"] for lot in lots]
    request = _hash(":".join(["matching", code, account or "", (q or "").strip().lower(), selection]))
    return await _move_lots(session, company_id, matching, request, code, key, actor_id)


async def _move_lots(session: AsyncSession, company_id, lots, request: str, code: str, key: str | None,
                     actor_id) -> dict:
    """Move the older lots ``lots()`` names onto inventory account ``code``, their value
    with them, in one entry dated today. ``lots`` is read once the company is locked.
    ``key`` identifies the operation, so a request sent twice moves once and returns the
    first result; the key reused for a different ``request`` is refused (409). A lot
    already on ``code`` is skipped. Refused, writing nothing: no key, an account that is
    not inventory or cannot take entries (_check_target), a lot not placed automatically
    or no longer in stock, a lot an unshipped invoice holds, or a locked period. Returns
    {"moved", "value", "code", "move_id"}."""
    from celerp.services.company_lock import lock_chart, locked_company
    from celerp.services.journal_accounts import lock_accounts

    key = (key or "").strip()
    if not key or len(key) > 64:
        raise _refused(422, "older_stock.move.no_key",
                       "This move carries no operation key, so it was not made. Select Try again.")
    code = (code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail=no_account_chosen())
    move_id = _move_id(company_id, key)
    await locked_company(session, company_id)
    if done := await _move_events(session, company_id, move_id):
        if any((e.metadata_ or {}).get("request") != request for e in done):
            raise _refused(409, "older_stock.move.key_reused",
                           "This operation key was already used for a different move, so nothing was moved. "
                           "Select Try again.")
        return {"moved": len(done), "value": _debits(await _je_state(session, company_id, _move_je(move_id))),
                "code": done[0].data[LOT_ACCOUNT_FIELD], "move_id": move_id}
    await lock_chart(session, company_id)
    rows = await _locked_lots(session, company_id, await lots())
    if await lock_accounts(session, company_id, {code}) is None:
        raise HTTPException(status_code=409, detail=needs_accounting())
    await _check_target(session, company_id, code)
    placed = set((await session.execute(select(LedgerEntry.entity_id).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == RECORDED,
        LedgerEntry.entity_id.in_([r.entity_id for r in rows]),
        LedgerEntry.metadata_["placement"].as_string().is_not(None)))).scalars())
    for row in rows:
        if row.entity_id not in placed or not in_stock(row.state):
            raise _refused(422, "older_stock.move.not_placed",
                           f"{_sku(row)} was not placed automatically or is no longer in stock, so it cannot be "
                           "moved here.", sku=_sku(row))
    await _refuse_invoiced(session, company_id, rows)
    day = await _open_day(session, company_id)
    moves = [(r, r.state[LOT_ACCOUNT_FIELD], code) for r in rows if r.state.get(LOT_ACCOUNT_FIELD) != code]
    value = await _move(session, company_id, moves, move_id, actor_id, day, request=request)
    return {"moved": len(moves), "value": float(value), "code": code, "move_id": move_id}


async def undo_lot_move(session: AsyncSession, company_id, move_id: str, actor_id) -> dict:
    """Put the lots of move ``move_id`` back on the accounts they came from, their value
    with them, in one entry dated today (move ``<move_id>:undo``). Once only, and only
    while every lot is still where the move put it, free of unshipped invoices, and the
    period is open; otherwise refused, writing nothing."""
    from celerp.services.company_lock import lock_chart, locked_company

    await locked_company(session, company_id)
    events = await _move_events(session, company_id, move_id)
    if not events:
        raise _refused(404, "older_stock.undo.no_move", "This move was not found.")
    undo_id = f"{move_id}:undo"
    if await _move_events(session, company_id, undo_id):
        raise _refused(409, "older_stock.undo.done", "This move was already undone.")
    await lock_chart(session, company_id)
    rows = {r.entity_id: r for r in await _locked_lots(session, company_id, [e.entity_id for e in events])}
    for e in events:
        row = rows[e.entity_id]
        if (row.state or {}).get(LOT_ACCOUNT_FIELD) != e.data[LOT_ACCOUNT_FIELD] or not in_stock(row.state):
            raise _refused(409, "older_stock.undo.moved_since",
                           f"{_sku(row)} has moved since, so this move can no longer be undone.", sku=_sku(row))
    await _refuse_invoiced(session, company_id, list(rows.values()))
    day = await _open_day(session, company_id)
    moves = [(rows[e.entity_id], e.data[LOT_ACCOUNT_FIELD], e.data["from_account"]) for e in events]
    value = await _move(session, company_id, moves, undo_id, actor_id, day, undo_of=move_id)
    return {"moved": len(moves), "value": float(value), "code": None, "move_id": undo_id}


# How the accountant can correct what a placement did with its difference (_place):
# book it against retained earnings the way opening stock is booked, or take a
# recognized amount back off the books and leave it unresolved.
CORRECTIONS = frozenset({"recognize", "unresolve"})


async def _placement_lots(session: AsyncSession, company_id, pids: list[str]) -> dict[str, list[str]]:
    """The lots each of placements ``pids`` recorded on its account, read in one query."""
    placement = LedgerEntry.metadata_["placement"].as_string()
    lots: dict[str, set[str]] = {pid: set() for pid in pids}
    for item_id, pid in (await session.execute(select(LedgerEntry.entity_id, placement).where(
            LedgerEntry.company_id == company_id, LedgerEntry.event_type == RECORDED, placement.in_(pids)))).all():
        lots[pid].add(item_id)
    return {pid: sorted(ids) for pid, ids in lots.items()}


# What a correction posts and records, by its kind and whether it acts on a surplus.
_CORRECTION_MEMOS = {
    ("recognize", False): "Older stock difference booked against retained earnings",
    ("unresolve", False): "Older stock opening stock taken back off the books, left unresolved",
    ("recognize", True): "Older stock surplus written off against retained earnings as a prior-period correction",
    ("unresolve", True): "Older stock surplus write-off undone, left unresolved",
}
_CORRECTION_KINDS = {("recognize", False): "recognize", ("unresolve", False): "unresolve",
                     ("recognize", True): "write_off", ("unresolve", True): "write_off_undone"}


async def correct_placements(session: AsyncSession, company_id, pids: list[str], kind: str, actor_id) -> int:
    """Correct how placements ``pids`` treated their difference, all of them or none:
    ``recognize`` books what each left unresolved against retained earnings, the way
    opening stock is booked (post_opening_stock_delta); ``unresolve`` takes what each
    recognized back off the books and leaves it unresolved for the accountant. On a
    surplus (more on the books than the stock), which placing always leaves open,
    ``recognize`` is the accountant's explicit write-off, a prior-period correction
    recorded as ``write_off``, and ``unresolve`` undoes it (``write_off_undone``),
    restoring the open difference. Each
    correction is one entry dated today and one record on the placement
    (sys.older_stock.corrected) moving the amount between recognized and unresolved, so
    the account still holds its lots, less unshipped recognized cost, plus its unresolved
    difference; that is checked again once booked. A placement with nothing to move is
    skipped, so a correction sent twice books once. Refused, writing nothing: no
    placements or too many, an unknown kind or placement, one that proved no difference
    while its accounts hold lines that may have been stock (the books check's uncertain
    evidence, stock_off_books), never booked
    and each account named, a lot of a placement sold,
    shipped, used up or moved since it was placed or held by an unshipped invoice, a
    locked today, or an account out of line with its stock afterwards. Returns how many
    placements were corrected."""
    from celerp.events.engine import emit_event
    from celerp.services.auto_je import unshipped_claims
    from celerp.services.company_lock import locked_company
    from celerp.services.journal_accounts import lock_accounts

    pids = list(dict.fromkeys(pid for pid in pids if pid))
    if not pids:
        raise _refused(422, "older_stock.correct.nothing_selected", "Select the placements to correct.")
    if len(pids) > MAX_MOVE:
        raise _refused(422, "older_stock.correct.too_many", f"Correct at most {MAX_MOVE} placements at a time.",
                       limit=MAX_MOVE)
    if kind not in CORRECTIONS:
        raise _refused(422, "older_stock.correct.no_kind",
                       "Choose whether to book the difference against retained earnings or leave it unresolved.")
    company = await locked_company(session, company_id)
    records = {}
    for pid in pids:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": _placement_entity(pid)},
                                populate_existing=True)
        if row is None or (row.state or {}).get("entity_type") != PLACEMENT:
            raise _refused(404, "older_stock.correct.no_placement", "This placement was not found.")
        records[pid] = dict(row.state)
    settings = dict(company.settings or {})
    currency = settings.get("currency", "USD")
    weighed = {c for state in records.values() if not (state.get("unresolved") or state.get("recognized"))
               for c in state.get("rooms") or {}}
    found = [f for f in await stock_off_books(session, company_id)
             if f["kind"] == "uncertain_evidence" and f["account"] in weighed]
    if found:
        accounts = [refusal("older_stock.correct.uncertain_account",
                            f"{f['account']} holds {f['amount']:.2f} on lines that may or may not have been "
                            f"stock ({', '.join(f['entries'])}).",
                            account=f["account"], amount=f["amount"], entries=", ".join(f["entries"]))
                    for f in found]
        raise _refused(409, "older_stock.correct.uncertain",
                       " ".join(a["message"] for a in accounts) + " That is not a difference to book, so nothing "
                       "was posted. Review those entries and post a journal entry if one is needed.",
                       accounts=accounts)
    codes = {state["account"] for state in records.values()}
    retained = role_map(settings).get(AccountRole.RETAINED_EARNINGS.value)
    if await lock_accounts(session, company_id, codes | ({retained} if retained else set())) is None:
        raise HTTPException(status_code=409, detail=needs_accounting())
    lots = await _placement_lots(session, company_id, pids)
    rows = {r.entity_id: r for r in await _locked_lots(session, company_id, [i for ids in lots.values() for i in ids])}
    held = {c.lot_id for c in await unshipped_claims(session, company_id, costed=False, lots=sorted(rows))}
    for pid, ids in lots.items():
        for item_id in ids:
            row = rows[item_id]
            if (item_id in held or not in_stock(row.state)
                    or (row.state or {}).get(LOT_ACCOUNT_FIELD) != records[pid]["account"]):
                raise _refused(409, "older_stock.correct.changed_since",
                               f"{_sku(row)} was sold, shipped or moved since it was placed, so this placement can "
                               "no longer be corrected here. Post a journal entry for the correction instead.",
                               sku=_sku(row))
    day = await _open_day(session, company_id)
    corrected = 0
    async with session.begin_nested():
        before = await account_rooms(session, company_id, codes)
        for pid, state in records.items():
            recognized = Decimal(str(state.get("recognized") or 0))
            unresolved = Decimal(str(state.get("unresolved") or 0))
            # What is booked onto the books (taken off them when negative). Recognized and
            # unresolved both move by it, so what the account holds beyond its stock stays.
            delta = round_money(-(unresolved if kind == "recognize" else recognized), currency)
            if not delta:
                continue
            surplus = (kind == "recognize") == (delta < 0)
            n = len(state.get("corrections") or []) + 1
            je_id = _placement_entry(pid, f"correction:{n}")
            await post_opening_stock_delta(
                session, company_id, {state["account"]: abs(delta)}, onto_books=delta > 0, je_id=je_id,
                idem=f"older-stock-placed:{pid}:correction:{n}",
                memo=_CORRECTION_MEMOS[kind, surplus],
                metadata={"trigger": "older_stock.corrected", "placement": pid, "classification": "opening_stock"},
                actor_id=actor_id, day=day)
            await emit_event(
                session, company_id=company_id, entity_id=_placement_entity(pid), entity_type=PLACEMENT,
                event_type=CORRECTED, actor_id=actor_id, location_id=None, source="system",
                idempotency_key=f"{_placement_entity(pid)}:correction:{n}", metadata_={},
                data={"placement": pid, "kind": _CORRECTION_KINDS[kind, surplus], "day": day, "amount": float(delta),
                      "recognized": float(recognized + delta), "unresolved": float(unresolved + delta),
                      "journal": je_id})
            corrected += 1
        await session.flush()
        if await account_rooms(session, company_id, codes) != before:
            raise _refused(409, "older_stock.correct.unbalanced",
                           "This correction would leave an inventory account out of line with its stock, so "
                           "nothing was posted.")
    return corrected


OLDER_STOCK_PAGES = ("lots", "placements", "moves", "corrections")
WAITING_NOTICES = (UNMAPPED_NOTICE, LOCKED_NOTICE, FAILED_NOTICE)


async def _reconciliation(session: AsyncSession, company_id, off_books: list[dict],
                          shown_on: dict[str, str]) -> dict:
    """Where the older stock stands with the books, one of three states:

    - ``action``: older stock waits for its inventory account (``waiting`` lots), each
      standing notice saying why (WAITING_NOTICES, their i18n records under ``reasons``);
      it cannot be sold or moved until someone fixes that.
    - ``review``: everything is placed and sales go on, but something is still open
      (``open``): per placement, newest first, a difference left unresolved or provisional
      cost an invoice recognized on its lots before placing while that invoice still
      holds them unshipped (_open_records); then lines that may have been stock
      (``uncertain``, each with its account, amount and entries, and the placement it is
      shown on, ``shown_on``), evidence only, which nothing books; then each inventory
      account whose books differ from the stock recorded on it beyond all of that
      (``stock_gap``, with what the books hold and the stock).
    - ``reconciled``: nothing waits and nothing is open.

    The uncertain lines and the gaps are ``off_books``, the books check as it stands
    (stock_off_books), so the two never disagree."""
    waiting = len(await _unplaced(session, company_id))
    reasons = [i18n for base in WAITING_NOTICES if (i18n := await _standing_notice(session, company_id, base))]
    records, held = await _open_records(session, company_id)
    open_ = [{"placement": r["placement"], "account": r["account"], "kind": kind, "amount": float(amount)}
             for r in records
             for kind, amount in (("unresolved", r.get("unresolved") or 0),
                                  ("provisional", (r.get("provisional") or 0) if r["placement"] in held else 0))
             if amount]
    open_ += [{**({"placement": shown_on[f["account"]]} if f["account"] in shown_on else {}), "kind": "uncertain",
               "account": f["account"], "amount": f["amount"], "entries": f["entries"]}
              for f in off_books if f["kind"] == "uncertain_evidence"]
    if not waiting:  # stock waiting for its account is the gap until placed
        open_ += [{"account": f["account"], "kind": "stock_gap", "amount": round(f["books"] - f["stock"], 2),
                   "books": f["books"], "stock": f["stock"]}
                  for f in off_books if f["kind"] == "stock_gap"]
    state = "action" if waiting or reasons else "review" if open_ else "reconciled"
    return {"state": state, "waiting": waiting, "reasons": reasons, "open": open_}


async def _held_placements(session: AsyncSession, company_id, pids: list[str]) -> set[str]:
    """Which of placements ``pids`` an unshipped invoice still holds a lot of
    (auto_je.unshipped_claims on the lot's family), read in the same queries however many."""
    from celerp.services.auto_je import _lot_families, unshipped_claims

    lots = {pid: ids for pid, ids in (await _placement_lots(session, company_id, pids)).items() if ids}
    if not lots:
        return set()
    claimed = {c.lot_id for c in await unshipped_claims(
        session, company_id, costed=False, lots=sorted(set().union(*lots.values())))}
    return {pid for pid, family in (await _lot_families(session, company_id, lots)).items() if family & claimed}


async def _open_records(session: AsyncSession, company_id) -> tuple[list[dict], set[str]]:
    """The placement records holding something open, newest first, and which of them an
    unshipped invoice holds (_held_placements): those leaving a difference unresolved,
    and those keeping provisional cost on lots they recorded while an invoice holds one.
    Read only from those placements, never from every placement."""
    placement = LedgerEntry.metadata_["placement"].as_string()
    provisional = (await session.execute(select(placement).join(Projection, and_(
        Projection.company_id == LedgerEntry.company_id, Projection.entity_type == PLACEMENT,
        Projection.state["placement"].as_string() == placement)).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == RECORDED,
        Projection.state["provisional"].as_float() != 0).distinct())).scalars().all()
    held = await _held_placements(session, company_id, list(provisional))
    records = (await session.execute(_placement_records(company_id).where(or_(
        Projection.state["unresolved"].as_float() != 0,
        Projection.state["placement"].as_string().in_(sorted(held)))))).scalars().all()
    return list(records), held


async def _shown_on(session: AsyncSession, company_id, off_books: list[dict]) -> dict[str, str]:
    """The placement each account's uncertain lines in ``off_books`` are shown on: the
    newest that weighed the account (its ``rooms``). An account no placement weighed (an
    inventory role pointed at it since) is shown on none. One read per account."""
    shown = {}
    for f in off_books:
        if f["kind"] == "uncertain_evidence":
            state = (await session.execute(_placement_records(company_id).where(
                Projection.state["rooms"][f["account"]].as_float().is_not(None)).limit(1))).scalar()
            if state:
                shown[f["account"]] = state["placement"]
    return shown


def _uncertain_on(off_books: list[dict], shown_on: dict[str, str], pid: str) -> list[dict]:
    """The uncertain lines in ``off_books`` shown on placement ``pid`` (_shown_on)."""
    return [{k: f[k] for k in ("account", "amount", "entries")} for f in off_books
            if f["kind"] == "uncertain_evidence" and shown_on.get(f["account"]) == pid]


def _placement_records(company_id):
    """The placement records (PLACEMENT) of ``company_id``, newest placement first."""
    return (select(Projection.state).join(LedgerEntry, and_(
        LedgerEntry.company_id == Projection.company_id, LedgerEntry.entity_id == Projection.entity_id,
        LedgerEntry.event_type == PLACED)).where(Projection.company_id == company_id,
                                                  Projection.entity_type == PLACEMENT)
        .order_by(LedgerEntry.id.desc()))


async def older_stock_lots(session: AsyncSession, company_id, currency: str, account: str | None = None,
                           q: str | None = None, items: list[Projection] | None = None) -> list[dict]:
    """The lots placed automatically still in stock, newest placement first, each with
    its SKU, name, account, booked value, placement and version; filtered (_matching) to
    ``account`` and to ``q`` in their SKU or name. Only the placements of lots in stock
    now are read (_in_stock_row), with the item rows of ``items`` (_held_items) when the
    caller has already read them."""
    placement = LedgerEntry.metadata_["placement"].as_string()
    recorded = (await session.execute(
        select(LedgerEntry.entity_id, placement)
        .join(Projection, and_(Projection.company_id == LedgerEntry.company_id,
                               Projection.entity_id == LedgerEntry.entity_id))
        .where(LedgerEntry.company_id == company_id, LedgerEntry.event_type == RECORDED, placement.is_not(None),
               Projection.entity_type == "item", _in_stock_row())
        .order_by(LedgerEntry.id.desc()))).all()
    rows = {r.entity_id: r for r in (await _held_items(session, company_id) if items is None else items)}
    return _matching([{"item_id": item_id, "sku": rows[item_id].state.get("sku") or "",
                       "name": rows[item_id].state.get("name") or "",
                       "account": rows[item_id].state.get(LOT_ACCOUNT_FIELD),
                       "value": float(booked_value(rows[item_id], currency)), "placement": pid,
                       "version": rows[item_id].version}
                      for item_id, pid in recorded if item_id in rows and in_stock(rows[item_id].state)],
                     account, q)


def _matching(lots: list[dict], account: str | None, q: str | None) -> list[dict]:
    needle = (q or "").strip().lower()
    return [lot for lot in lots if (not account or lot["account"] == account)
            and (not needle or needle in lot["sku"].lower() or needle in lot["name"].lower())]


async def _count(session: AsyncSession, query) -> int:
    return (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()


async def _placements_page(session: AsyncSession, company_id, offset: int, limit: int, off_books: list[dict],
                           shown_on: dict[str, str]) -> tuple[list[dict], int]:
    """A page of the placements, newest first, as their records hold them, each with the
    uncertain lines of the books check (``off_books``) shown on it (_uncertain_on), and
    how many there are."""
    query = _placement_records(company_id)
    return [{"pid": r["placement"], "day": r["day"], "count": r["count"], "account": r["account"],
             "classification": r.get("classification"), "transferred": float(r.get("transferred") or 0),
             "recognized": float(r.get("recognized") or 0), "unresolved": float(r.get("unresolved") or 0),
             "provisional": float(r.get("provisional") or 0), "uncertain": _uncertain_on(off_books, shown_on, r["placement"]),
             "journal": r.get("journal") or [], "corrections": r.get("corrections") or []}
            for r in (await session.execute(query.offset(offset).limit(limit))).scalars()], \
        await _count(session, query)


async def _moves_page(session: AsyncSession, company_id, tz: str | None, offset: int,
                      limit: int) -> tuple[list[dict], int]:
    """A page of the moves, newest first, each with its day, account, how many lots it
    moved, the value its entry moved, whether it was undone and what it undid, and how
    many moves there are."""
    move = LedgerEntry.metadata_["move"].as_string()
    move_id = move.label("move_id")
    query = (select(move_id, func.count().label("count"), func.max(LedgerEntry.id).label("last"))
             .where(LedgerEntry.company_id == company_id, LedgerEntry.event_type == MOVED).group_by(move_id))
    page = (await session.execute(query.order_by(func.max(LedgerEntry.id).desc()).offset(offset).limit(limit))).all()
    last = {e.id: e for e in (await session.execute(select(LedgerEntry).where(
        LedgerEntry.id.in_([row.last for row in page])))).scalars()}
    ids = [row.move_id for row in page]
    journals = {eid: state for eid, state in (await session.execute(select(Projection.entity_id, Projection.state)
                                                                     .where(Projection.company_id == company_id,
                                                                            Projection.entity_id.in_(
                                                                                [_move_je(m) for m in ids])))).all()}
    undone = set((await session.execute(select(move).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == MOVED,
        move.in_([f"{m}:undo" for m in ids])).distinct())).scalars())
    return [{"move_id": row.move_id, "day": business_date_at(last[row.last].ts, tz),
             "code": last[row.last].data[LOT_ACCOUNT_FIELD], "count": row.count,
             "value": _debits(_posted(journals.get(_move_je(row.move_id)))),
             "undone": f"{row.move_id}:undo" in undone, "undo_of": (last[row.last].metadata_ or {}).get("undo_of")}
            for row in page], await _count(session, query)


async def _corrections_page(session: AsyncSession, company_id, offset: int, limit: int) -> tuple[list[dict], int]:
    """A page of the corrections, newest first, each with who made it, and how many there
    are."""
    from celerp.services.ledger_display import display_fields

    query = select(LedgerEntry).where(LedgerEntry.company_id == company_id, LedgerEntry.event_type == CORRECTED)
    page = (await session.execute(query.add_columns(Projection.state["account"].as_string()).outerjoin(
        Projection, and_(Projection.company_id == LedgerEntry.company_id,
                         Projection.entity_id == LedgerEntry.entity_id))
        .order_by(LedgerEntry.id.desc()).offset(offset).limit(limit))).all()
    shown = await display_fields([e for e, _ in page], company_id, session)
    return [{"placement": e.data["placement"], "day": e.data["day"], "kind": e.data["kind"],
             "amount": e.data["amount"], "journal": e.data["journal"], "account": code,
             "actor_name": s["actor_name"]}
            for (e, code), s in zip(page, shown)], await _count(session, query)


async def older_stock_view(session: AsyncSession, company_id, account: str | None = None,
                           q: str | None = None, offsets: dict[str, int] | None = None, limit: int = 50) -> dict:
    """Settings > Accounting > Older stock: the lots placed automatically still in stock,
    newest placement first (filtered to ``account`` and to ``q`` in their SKU or name),
    each placement with its classification, what it transferred, recognized and left
    unresolved and its journal entries, every move and every correction (with who made
    it) newest first, a summary per inventory account, the accounts the lots sit on, the
    accounts they can be moved to, a standing failure to place older stock
    (failed_placement), and where the older stock stands with the books
    (_reconciliation). Each list in OLDER_STOCK_PAGES is a page of ``limit`` rows from
    its own offset, with its total under ``totals``; how many lots match the filters and
    their value, which a move of every matching lot (move_matching_lots) takes, under
    ``matching``. Placements, moves and corrections are paged in their queries; the lots
    in stock are read once (_held_items), for the books check and for the lots, as the
    summary and ``matching`` add up their booked values; lots no longer held are never read."""
    from celerp.services.posting_readiness import older_stock_targets

    settings = await current_settings(session, company_id)
    currency, tz = settings.get("currency", "USD"), settings.get("timezone")
    offsets = offsets or {}
    items = await _held_items(session, company_id)
    held = await older_stock_lots(session, company_id, currency, items=items)
    summary: dict[str, dict] = {}

    def _account(code: str) -> dict:
        return summary.setdefault(code, {"account": code, "lots": 0, "value": 0.0, "transferred": 0.0,
                                         "recognized": 0.0, "unresolved": 0.0})
    for lot in held:
        if lot["account"]:
            _account(lot["account"])["lots"] += 1
            _account(lot["account"])["value"] += lot["value"]
    code = Projection.state["account"].as_string().label("code")
    totals = select(code, *(func.coalesce(func.sum(Projection.state[f].as_float()), 0) for f in _PLACED_AMOUNTS)).where(
        Projection.company_id == company_id, Projection.entity_type == PLACEMENT).group_by(code)
    for code, *amounts in (await session.execute(totals)).all():
        for f, amount in zip(_PLACED_AMOUNTS, amounts):
            _account(code)[f] += float(amount)
    for row in summary.values():
        for f in ("value", *_PLACED_AMOUNTS):
            row[f] = round(row[f], 2)
    lots = _matching(held, account, q)
    off_books = await stock_off_books(session, company_id, items)
    shown_on = await _shown_on(session, company_id, off_books)
    lists = {"lots": (lots[offsets.get("lots", 0):offsets.get("lots", 0) + limit], len(lots)),
             "placements": await _placements_page(session, company_id, offsets.get("placements", 0), limit,
                                                  off_books, shown_on),
             "moves": await _moves_page(session, company_id, tz, offsets.get("moves", 0), limit),
             "corrections": await _corrections_page(session, company_id, offsets.get("corrections", 0), limit)}
    return {**{name: page for name, (page, _) in lists.items()},
            "totals": {name: total for name, (_, total) in lists.items()}, "limit": limit,
            "matching": _matching_summary(lots),
            "summary": sorted(summary.values(), key=lambda r: r["account"]),
            "accounts": sorted({lot["account"] for lot in held if lot["account"]}),
            "failed": await failed_placement(session, company_id),
            "reconciliation": await _reconciliation(session, company_id, off_books, shown_on),
            **await older_stock_targets(session, company_id)}
