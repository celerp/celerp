# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The bank reconciliations, judged at the one boundary every journal change passes.

emit_event calls ``guard_journal_change`` (registered on core's ``ChartAccess``) for
every journal entry event, whoever writes it. The change is compared as the books read
it, before and after: the entry's effective rows (``ledger_rows.entry_rows``) and their
lines on each bank account, each line read in the account's own currency the way its
reconciliation reads it (``_bank_amounts``), so a change of the foreign amount that
leaves the books-currency figure as it was is a change too. Two rules refuse it:

- Held: a row a reconciliation counts (matched on it, or the bank's opening entry) may
  not disappear or change, and a counted entry may not be voided or reversed.
- Period: a bank row may not appear, disappear or change on a day inside the period of
  a completed reconciliation. That period runs from the day after the latest earlier
  statement of the same bank that existed when it was completed, up to its own
  statement date. A stored statement date that is not an ISO date bounds nothing and
  has no period, since any day read into it would be a guess.

Everything else passes: journals without a bank line, postings after every completed
period, other bank accounts, and a repeat of a change already applied.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine
from celerp.services.company_lock import lock_company
from celerp.services.journal_accounts import ReconciledHistoryChanged
from celerp.services.ledger_rows import entry_rows
from celerp.services.permissions import _authority_for, read_authority, role_has_permission
from celerp_accounting.models import BankAccount, ReconciliationSession
from celerp_accounting.routes import _bank_amounts, _base_currency, _line_amounts, _opening_je_id
from ui.i18n import list_text, t

# {(row id, bank chart code): (day, the row's lines on it)}, each line its (debit, credit)
# in the currency of every bank account kept on that code (None where it has no amount in it).
LineAmounts = tuple[tuple[Decimal, Decimal] | None, ...]
BankRows = dict[tuple[str, str], tuple[str, tuple[LineAmounts, ...]]]


def iso_day(value: str) -> str | None:
    """``value`` when it is a real ISO date, else None."""
    try:
        return date.fromisoformat(value).isoformat() if len(value or "") == 10 else None
    except ValueError:
        return None


def _bank_rows(entity_id: str, state: dict, currencies: dict[str, list[str]], base: str) -> BankRows:
    """The entry's effective rows on bank accounts, keyed by row and bank code: the day
    and every line on the code, each line's amounts in the currencies of the bank
    accounts kept on it (*currencies*, by code), in a stable order."""
    lines: dict[tuple[str, str], tuple[str, list[LineAmounts]]] = {}
    for row_id, row_state, day in entry_rows(entity_id, state):
        for line in row_state.get("entries") or []:
            code = line.get("account")
            if code not in currencies or _line_amounts(line) is None:
                continue
            lines.setdefault((row_id, code), (day, []))[1].append(
                tuple(_bank_amounts(line, currency, base) for currency in currencies[code]))
    return {key: (day, tuple(sorted(amounts, key=repr))) for key, (day, amounts) in lines.items()}


async def _state(session: AsyncSession, company_id, entity_id: str, *, fresh: bool) -> dict:
    row = await session.get(Projection, (company_id, entity_id), populate_existing=fresh)
    return dict(row.state or {}) if row is not None else {}


def _reversed_id(event_type: str, data: dict) -> str | None:
    return data.get("reverses") if event_type == "acc.journal_entry.created" else None


def _period_start(recon: ReconciliationSession, sessions: list[ReconciliationSession]) -> str | None:
    """The latest statement date before ``recon``'s among the bank's sessions that existed
    when it was completed; the period starts the day after. None: from the beginning."""
    own = iso_day(recon.statement_date)
    cutoff = recon.completed_at or recon.created_at
    earlier = [
        d for s in sessions
        if s.id != recon.id and s.created_at < cutoff
        and (d := iso_day(s.statement_date)) is not None and d < own
    ]
    return max(earlier, default=None)


def _in_period(day: str, recon: ReconciliationSession, sessions: list[ReconciliationSession]) -> bool:
    own = iso_day(recon.statement_date)
    if recon.status != "completed" or own is None:
        return False
    start = _period_start(recon, sessions)
    return day <= own and (start is None or day > start)


def _holds(recon: ReconciliationSession, bank: BankAccount, row_id: str) -> bool:
    return row_id in (recon.reconciled_je_ids or []) or row_id == _opening_je_id(bank.id)


async def guard_journal_change(
    session: AsyncSession, company_id, entity_id: str, event_type: str, data: dict
) -> None:
    """Refuse with ``ReconciledHistoryChanged`` a journal event that would change what a
    bank reconciliation counts; return for every other event."""
    company_id = uuid.UUID(str(company_id))
    # Several bank accounts may share one ledger account: each is judged on its own.
    codes: dict[str, list[uuid.UUID]] = {}
    currencies: dict[str, list[str]] = {}
    for bank_id, code, currency in (await session.execute(
        select(BankAccount.id, BankAccount.chart_account_code, BankAccount.currency)
        .where(BankAccount.company_id == company_id).order_by(BankAccount.currency)
    )).all():
        codes.setdefault(code, []).append(bank_id)
        if currency not in currencies.setdefault(code, []):
            currencies[code].append(currency)
    if not codes:
        return
    base = await _base_currency(session, company_id)
    reversed_id = _reversed_id(event_type, data)

    async def touched(fresh: bool) -> tuple[BankRows, BankRows, set[str]]:
        before = await _state(session, company_id, entity_id, fresh=fresh)
        after = ProjectionEngine._apply(before, event_type, data)
        rows_before = _bank_rows(entity_id, before, currencies, base)
        rows_after = _bank_rows(entity_id, after, currencies, base)
        banks = {code for _, code in rows_before} | {code for _, code in rows_after}
        if reversed_id:
            reversed_state = await _state(session, company_id, reversed_id, fresh=fresh)
            banks |= {code for _, code in _bank_rows(reversed_id, reversed_state, currencies, base)}
        return rows_before, rows_after, banks

    _, _, banks = await touched(False)
    if not banks:
        return

    # Lock order: the company, then the touched bank accounts in id order. A completion
    # holds its bank FOR UPDATE, so a change waits for it (or it for the change) and
    # each judges what the other committed.
    await lock_company(session, company_id)
    bank_ids = sorted(bank_id for code in banks for bank_id in codes[code])
    accounts = (await session.execute(
        select(BankAccount).where(BankAccount.id.in_(bank_ids)).order_by(BankAccount.id)
        .with_for_update(read=True).execution_options(populate_existing=True)
    )).scalars().all()
    rows_before, rows_after, banks = await touched(True)
    judged = [b for b in accounts if b.chart_account_code in banks]
    sessions: dict[uuid.UUID, list[ReconciliationSession]] = {b.id: [] for b in judged}
    for recon in (await session.execute(
        select(ReconciliationSession).where(
            ReconciliationSession.company_id == company_id,
            ReconciliationSession.bank_account_id.in_(list(sessions)),
        ).execution_options(populate_existing=True)
    )).scalars().all():
        sessions[recon.bank_account_id].append(recon)

    changed = {key for key in rows_before.keys() | rows_after.keys() if rows_before.get(key) != rows_after.get(key)}
    cancelled = event_type == "acc.journal_entry.voided" and bool(changed)
    held_ids = {row_id for row_id, _ in changed}
    if cancelled:
        held_ids |= {row_id for row_id, _ in rows_before}
    if reversed_id:
        held_ids |= {reversed_id, f"{reversed_id}:reversal"}

    held: list[tuple[BankAccount, ReconciliationSession]] = []
    period: list[tuple[BankAccount, ReconciliationSession]] = []
    for bank in judged:
        code = bank.chart_account_code
        for recon in sessions[bank.id]:
            if any(_holds(recon, bank, row_id) for row_id in held_ids):
                held.append((bank, recon))
                continue
            days = {
                rows[key][0] for key in changed if key[1] == code
                for rows in (rows_before, rows_after) if key in rows
            }
            if any(_in_period(day, recon, sessions[bank.id]) for day in days):
                period.append((bank, recon))
    if held or period:
        raise ReconciledHistoryChanged(await _refusal(session, company_id, held or period, held=bool(held)))


async def _refusal(session: AsyncSession, company_id: uuid.UUID,
                   hits: list[tuple[BankAccount, ReconciliationSession]], *, held: bool) -> str:
    """The message naming every reconciliation hit, with the remedy the caller can act
    on: someone who manages accounting is told to act, anyone else whom to ask."""
    accounts = list(dict.fromkeys(bank.bank_name for bank, _ in sorted(hits, key=lambda h: h[0].bank_name)))
    dates = sorted({recon.statement_date for _, recon in hits},
                   key=lambda d: (iso_day(d) is None, iso_day(d) or d))
    authority = _authority_for(session, company_id)
    manages = False
    if authority is not None:
        role, settings = await read_authority(session, company_id, authority.user_id)
        manages = role_has_permission(settings, role, "manage_accounting")
    suffix = "" if manages else "_ask"
    params = {"account": list_text(accounts), "dates": list_text(dates)}
    if held:
        return t(f"error.recon_entry_held{suffix}", **params)
    return t(f"error.recon_period_changed{suffix}", last_date=max(d for d in dates if iso_day(d)), **params)
