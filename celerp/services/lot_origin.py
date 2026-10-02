# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Inventory accounts for stock from before lots recorded their own.

A lot records the inventory account its value is booked into when it first takes on
stock (events.engine._record_lot_account). Stock from an older release recorded none,
and each such lot gets the account its own history proves, nothing else:

- stock a receipt, a bill, a returned sale or a production run brought in sits where
  that document's entry debited inventory;
- stock entered with no document behind it (by hand or by import) sits where the
  opening inventory entry carries pre-system stock, when that account holds all of it;
- a part of a lot sits with the lot it came from, and a merge result sits where all of
  its sources sat.

A lot whose history proves no single account (stock from two kinds of source, a
migration, a restored snapshot) records none and refuses to move its cost
(account_roles.lot_account) until the user picks its account here. A pick is accepted
only when that account holds the lot's value beyond what its other stock accounts for.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import LOT_ACCOUNT_FIELD, AccountRole
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.account_roles import current_settings, line_roles, scope_codes
from celerp.services.money import round_money

RECORDED = "item.inventory_account.recorded"

# Statuses of lots whose stock is not on the books: no longer owned, or (draft) not yet
# committed. Must match get_valuation()'s filter.
NOT_HELD = frozenset({"archived", "deleted", "void", "sold", "fulfilled", "merged", "expired", "draft", "disposed"})

_INVENTORY = (AccountRole.INVENTORY_PURCHASED.value, AccountRole.INVENTORY_OPENING.value)
_OPENING = object()  # stock with no document behind it: carried by the opening inventory entry


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
    """Stock on hand whose inventory account is neither recorded nor proven, oldest first."""
    rows = sorted(unrecorded(await _items(session, company_id)), key=lambda r: (r.created_at is None, r.created_at))
    return [{"item_id": r.entity_id, "sku": r.state.get("sku") or "", "name": r.state.get("name") or "",
             "value": float(held_value(r))} for r in rows]


async def _record(session: AsyncSession, company_id, item_id: str, code: str, why: str, actor_id) -> None:
    from celerp.events.engine import emit_event

    await emit_event(session, company_id=company_id, entity_id=item_id, entity_type="item", event_type=RECORDED,
                     data={LOT_ACCOUNT_FIELD: code}, actor_id=actor_id, location_id=None, source="system",
                     idempotency_key=f"lot-account:{item_id}", metadata_={"proven_by": why})


def _single(codes) -> str | None:
    codes = set(codes)
    return next(iter(codes)) if len(codes) == 1 else None


async def prove_lot_accounts(session: AsyncSession, company_id) -> int:
    """Record, on each lot with no inventory account, the account its history proves
    (module docstring). Lots it proves nothing for are left as they are. Running it
    again changes nothing. Returns how many lots it recorded."""
    from celerp.services.company_lock import locked_company

    items = await _items(session, company_id)
    pending = {r.entity_id: r for r in items if not (r.state or {}).get(LOT_ACCOUNT_FIELD)}
    if not pending or await locked_company(session, company_id) is None:
        return 0
    settings = await current_settings(session, company_id)
    entries = await _posted_entries(session, company_id)

    # Inventory accounts each document's entries debited, keyed by the document id
    # (every prefix of je:auto:<document>:<step>).
    debited: dict[str, set[str]] = {}
    for je_id, entry in entries:
        if not je_id.startswith("je:auto:") or not float(entry.get("debit") or 0):
            continue
        if not set(_INVENTORY).intersection(line_roles(settings, entry)):
            continue
        parts = je_id[len("je:auto:"):].split(":")
        for n in range(1, len(parts)):
            debited.setdefault(":".join(parts[:n]), set()).add(entry["account"])
    opening = _single(entry["account"] for je_id, entry in entries
                      if je_id == f"je:auto:opening-inventory:{company_id}" and float(entry.get("debit") or 0)
                      and AccountRole.INVENTORY_OPENING.value in line_roles(settings, entry))

    events: dict[str, list[LedgerEntry]] = {}
    ids = list(pending)
    for i in range(0, len(ids), 1000):
        for ev in (await session.execute(select(LedgerEntry).where(
                LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(ids[i:i + 1000]))
                .order_by(LedgerEntry.id))).scalars():
            events.setdefault(ev.entity_id, []).append(ev)

    def doc_account(meta: dict) -> tuple[str | None, str | None]:
        doc = meta.get("source_doc") or meta.get("source_return_cn") or meta.get("manufacturing_order_id")
        return (doc, _single(debited.get(doc, ()))) if doc else (None, None)

    recorded = {r.entity_id: (r.state or {}).get(LOT_ACCOUNT_FIELD) for r in items}
    proofs: dict[str, tuple[object, str] | None] = {}

    def prove(item_id: str) -> tuple[object, str] | None:
        """(account or _OPENING, what proves it), or None when nothing proves one account."""
        if item_id not in pending:
            code = recorded.get(item_id)
            return (code, item_id) if code else None
        if item_id in proofs:
            return proofs[item_id]
        proofs[item_id] = None  # a cycle proves nothing
        history = events.get(item_id) or []
        if not history or history[0].event_type != "item.created" or history[0].source == "migration":
            return None
        meta = history[0].metadata_ or {}
        if meta.get("parent_id"):
            found = prove(str(meta["parent_id"]))
        elif meta.get("merged_from"):
            sources = [prove(str(s)) for s in meta["merged_from"]]
            account = None if None in sources else _single(p[0] for p in sources)
            found = (account, "merge") if account is not None else None
        else:
            doc, account = doc_account(meta)
            found = (account, doc) if account else None if doc else (_OPENING, "opening-inventory")
        # Stock a later document added must sit on the same account.
        for ev in history[1:]:
            meta = ev.metadata_ or {}
            if found and (meta.get("source_doc") or meta.get("source_return_cn")):
                later = doc_account({k: meta.get(k) for k in ("source_doc", "source_return_cn")})[1]
                if later is None or later != (opening if found[0] is _OPENING else found[0]):
                    found = None
        proofs[item_id] = found
        return found

    proven = {item_id: prove(item_id) for item_id in pending}
    proven = {item_id: found for item_id, found in proven.items() if found}
    by_document = {i: p for i, p in proven.items() if p[0] is not _OPENING}
    by_opening = {i: p for i, p in proven.items() if p[0] is _OPENING}
    if by_opening:
        # Pre-system stock sits on the opening inventory account only if that account holds all of it.
        currency = settings.get("currency", "USD")
        held = sum((held_value(pending[i]) or Decimal("0") for i in by_opening), Decimal("0"))
        held += sum((held_value(pending[i]) or Decimal("0") for i, p in by_document.items() if p[0] == opening),
                    Decimal("0"))
        if opening and round_money(held, currency) <= round_money(_room(entries, items, opening), currency):
            by_document.update({i: (opening, why) for i, (_, why) in by_opening.items()})
    for item_id, (code, why) in sorted(by_document.items()):
        await _record(session, company_id, item_id, str(code), why, None)
    return len(by_document)


async def choose_lot_account(session: AsyncSession, company_id, item_id: str, code: str, actor_id) -> None:
    """Record the inventory account the user picked for a lot that records none. It must be
    an account that has held purchased or opening inventory, and it must hold the lot's
    value beyond what the other stock recording it accounts for."""
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
            f"recorded on it, it holds {max(room, Decimal('0'))}. Choose the account its cost was booked to."))
    await _record(session, company_id, item_id, code, "chosen", actor_id)
