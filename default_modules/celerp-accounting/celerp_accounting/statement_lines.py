# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Bank statement lines: the one place a line is found, settled or let go.

Every bank route resolves each record id it is given through ``scoped``; ``RECORD_FIELDS``
names the request fields that carry one. A line settles by posting an entry (``post_line``,
one offset or several), by matching book entries already on the bank (``match_lines``), or by
needing none (``skip_line``); ``unmatch_line`` lets it go and voids an entry it posted.
``write_suggestion`` is the only writer of a line's suggestion.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import catalog_refusal
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp_accounting.models import BankAccount, BankStatementLine, ReconciliationSession

# Request fields that name a record, by the kind ``scoped`` resolves them as.
RECORD_FIELDS = {
    "line_ids": "line", "line_id": "line", "paired_line_id": "line",
    "target_bank_id": "bank", "bank_id": "bank",
    "statement_id": "session", "through_statement_id": "session", "session_id": "session",
    "job_id": "job", "doc_id": "doc", "je_ids": "entry",
    "account_code": "account", "contact": "contact", "contact_id": "contact", "fund_id": "fund", "rule_id": "rule",
}

SETTLED = ("matched", "posted")
_OWN_ENTRY_TYPES = {"recon_create", "recon_split", "recon_transfer", "recon_suspense"}


def _not_found(kind: str) -> HTTPException:
    key = {"line": "acct.err_statement_line_not_found", "bank": "acct.err_bank_account_not_found",
           "session": "acct.err_recon_not_found"}[kind]
    return HTTPException(status_code=404, detail=catalog_refusal(key))


async def scoped(db: AsyncSession, company_id: uuid.UUID, kind: str, ids: list, *, bank: BankAccount | None = None,
                 state: tuple[str, ...] | None = None) -> list:
    """The records ``ids`` name, each the caller company's (and, for a line or statement, the
    path bank's), lines read under their row lock in id order. A foreign id gets the same 404
    as an unknown one; a line outside ``state`` is refused with its status."""
    want = sorted({uuid.UUID(str(i)) for i in ids}, key=str)
    if kind == "line":
        q = select(BankStatementLine).where(BankStatementLine.id.in_(want), BankStatementLine.company_id == company_id)
        if bank is not None:
            q = q.where(BankStatementLine.bank_account_id == bank.id)
        rows = list((await db.execute(q.order_by(BankStatementLine.id).with_for_update())).scalars())
    elif kind == "session":
        q = select(ReconciliationSession).where(ReconciliationSession.id.in_(want),
                                                ReconciliationSession.company_id == company_id)
        if bank is not None:
            q = q.where(ReconciliationSession.bank_account_id == bank.id)
        rows = list((await db.execute(q)).scalars())
    elif kind == "bank":
        q = select(BankAccount).where(BankAccount.id.in_(want), BankAccount.company_id == company_id,
                                      BankAccount.is_active.is_(True))
        rows = list((await db.execute(q)).scalars())
        if bank is not None and any(r.id == bank.id for r in rows):
            raise _not_found("bank")
    else:
        raise ValueError(kind)
    if len(rows) != len(want):
        raise _not_found(kind)
    if state is not None and kind == "line":
        for r in rows:
            if r.status not in state:
                raise HTTPException(status_code=409, detail=catalog_refusal(
                    "acct.err_recon_line_state", status=r.status))
    return rows


def suggestion_value(suggestion: dict) -> dict:
    """``suggestion`` as stored: a contact stays only when its evidence names the record that
    names that contact (an open document, a person's or an answer's rule, a read receipt,
    history on a named counterparty, or a client answer); a posting with no contact says why."""
    from ui.i18n import t
    s = {k: v for k, v in suggestion.items() if v is not None or k == "kind"}
    if s.get("contact_id") and not (s.get("evidence") or {}).get("names_contact"):
        s.pop("contact_id")
    if s.get("kind") == "post" and not s.get("contact_id"):
        s["reason"] = f"{s.get('reason') or ''} {t('acct.recon_no_contact_named')}".strip()
    return s


def write_suggestion(line: BankStatementLine, suggestion: dict | None) -> None:
    """The one writer of a line's suggestion."""
    line.suggestion = None if suggestion is None else suggestion_value(suggestion)


async def _session(db: AsyncSession, line: BankStatementLine) -> ReconciliationSession:
    """The line's statement under its lock, refused unless it can still change."""
    from celerp_accounting.routes import _lock_recon
    recon, _bank = await _lock_recon(db, line.reconciliation_id, line.company_id, editable=True)
    return recon


def _hold(recon: ReconciliationSession, je_ids: list[str]) -> None:
    held = list(recon.reconciled_je_ids or [])
    recon.reconciled_je_ids = held + [j for j in je_ids if j not in held]


def _release(recon: ReconciliationSession, je_ids: list[str]) -> None:
    recon.reconciled_je_ids = [j for j in (recon.reconciled_je_ids or []) if j not in je_ids]


async def next_je_id(db: AsyncSession, company_id: uuid.UUID, base_id: str) -> str:
    """The next usable entry id after any voided earlier attempts; the line's row lock
    serialises the choice. After an unmatch voided an attempt, the next posting gets a new
    entry instead of resurrecting the voided one."""
    attempt = 1
    while True:
        entity_id = base_id if attempt == 1 else f"{base_id}:{attempt}"
        row = await db.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if row is None:
            return entity_id
        if row.entity_type != "journal_entry":
            raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_entry_exists"))
        if (row.state or {}).get("status") != "void":
            raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_line_has_entry"))
        attempt += 1


async def post_entry(db: AsyncSession, *, company_id: uuid.UUID, actor_id, line: BankStatementLine, je_id: str,
                     je_type: str, entries: list[dict], memo: str, entry_date: str, key: str) -> None:
    """Create and post one entry for ``line``; the event keys derive from the request key,
    so a replay answers the first result even after an undo voided it."""
    meta = {"recon_session_id": str(line.reconciliation_id), "statement_line_id": str(line.id)}
    await emit_event(db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
                     event_type="acc.journal_entry.created",
                     data={"memo": memo, "ts": entry_date, "entries": entries, "je_type": je_type},
                     actor_id=actor_id, location_id=None, source="reconciliation",
                     idempotency_key=f"{key}:{line.id}:c", metadata_=meta)
    await emit_event(db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
                     event_type="acc.journal_entry.posted", data={"ts": entry_date}, actor_id=actor_id,
                     location_id=None, source="reconciliation", idempotency_key=f"{key}:{line.id}:p", metadata_={})


async def post_line(db: AsyncSession, *, company_id: uuid.UUID, actor_id, bank: BankAccount, line: BankStatementLine,
                    offsets: list[tuple[dict, Decimal]], rate: float | None, memo: str | None, entry_date: str | None,
                    key: str, je_type: str | None = None) -> list[str]:
    """Post ``line`` against ``offsets`` (one, or several for a split) and hold the entry.
    The offsets must total the line; each carries its account and optional contact and fund."""
    from celerp_accounting import import_service
    from celerp_accounting.routes import (_bank_entries, _bank_rate, _base_currency, _money_text,
                                          _pooled_offsets, require_destinations)
    from celerp.i18n import t

    recon = await _session(db, line)
    amount = abs(Decimal(str(line.amount)))
    total = sum((a for _, a in offsets), Decimal(0))
    if total != amount:
        raise HTTPException(status_code=422, detail=t("acct.err_split_total", total=_money_text(total, bank.currency),
                                                      amount=_money_text(amount, bank.currency)))
    await require_destinations(db, company_id, {o["account"] for o, _ in offsets})
    split = len(offsets) > 1
    je_type = je_type or ("recon_split" if split else "recon_create")
    je_id = await next_je_id(db, company_id, f"je:recon:split:{line.id}" if split else f"je:recon:{line.id}")
    when = entry_date or line.line_date
    base = await _base_currency(db, company_id)
    fx = _bank_rate(bank, base, rate)
    pooled = await _pooled_offsets(db, company_id, bank, base, fx, offsets, inflow=line.amount > 0, as_of=when)
    entries = _bank_entries(bank, base, fx, pooled, inflow=line.amount > 0)
    await import_service.check_line_contacts(db, company_id, entries)
    await post_entry(db, company_id=company_id, actor_id=actor_id, line=line, je_id=je_id, je_type=je_type,
                     entries=entries, memo=memo or line.description, entry_date=when, key=key)
    line.status = "posted"
    line.matched_je_ids = [je_id]
    _hold(recon, [je_id])
    recon.created_count = (recon.created_count or 0) + 1
    return [je_id]


async def match_lines(db: AsyncSession, *, company_id: uuid.UUID, bank: BankAccount, lines: list[BankStatementLine],
                      je_ids: list[str]) -> None:
    """Match ``lines`` (one or several) to book entries on the bank (one or several) whose
    bank-side total equals theirs. An entry another line already holds is refused."""
    from celerp_accounting.routes import _eligible_entries, _refuse_ineligible

    if not je_ids:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_recon_match_needs_entry"))
    held: set[str] = set()
    for row in (await db.execute(select(BankStatementLine.matched_je_ids).where(
            BankStatementLine.company_id == company_id, BankStatementLine.bank_account_id == bank.id,
            BankStatementLine.id.notin_([l.id for l in lines])))).scalars():
        held.update(row or [])
    if held & set(je_ids):
        raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_book_entry_matched"))
    by_session: dict = {}
    for line in lines:
        by_session.setdefault(line.reconciliation_id, []).append(line)
    for group in by_session.values():
        recon = await _session(db, group[0])
        _refuse_ineligible(await _eligible_entries(db, company_id, bank, recon, je_ids,
                                                   line_ids=[l.id for l in group]))
    for line in lines:
        recon = await _session(db, line)
        line.status = "matched"
        line.matched_je_ids = list(je_ids)
        _hold(recon, je_ids)
        recon.manual_matched_count = (recon.manual_matched_count or 0) + 1


async def skip_line(db: AsyncSession, line: BankStatementLine) -> None:
    """Mark a line that needs no entry (a bank reversal pair, a line already in the books)."""
    await _session(db, line)
    line.status = "skipped"
    line.matched_je_ids = []


async def unmatch_line(db: AsyncSession, *, company_id: uuid.UUID, actor_id, line: BankStatementLine) -> None:
    """Let ``line`` go: its statement releases the entries first, so the completed-history
    check sees none held, then an entry the line posted itself is voided."""
    from celerp.services.je_keys import je_void_data

    recon = await _session(db, line)
    je_ids = list(line.matched_je_ids or [])
    _release(recon, je_ids)
    await db.flush()
    if line.status == "posted":
        for je_id in je_ids:
            row = await db.get(Projection, {"company_id": company_id, "entity_id": je_id})
            if row is None or row.entity_type != "journal_entry":
                raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_undo_entry_gone"))
            state = row.state or {}
            if state.get("status") == "void":
                continue
            if state.get("je_type") not in _OWN_ENTRY_TYPES:
                raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_undo_foreign"))
            await emit_event(db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
                             event_type="acc.journal_entry.voided",
                             data=je_void_data("Reconciliation line unmatched", state),
                             actor_id=actor_id, location_id=None, source="reconciliation",
                             idempotency_key=f"{je_id}:reconciliation-unmatch:void", metadata_={})
    line.status = "unmatched"
    line.matched_je_ids = []
