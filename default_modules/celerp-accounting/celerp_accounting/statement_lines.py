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
from celerp_accounting.models import BankAccount, BankStatementLine, ReconciliationSession, holding_any

# Request fields that name a record, by the kind ``scoped`` resolves them as.
RECORD_FIELDS = {
    "line_ids": "line", "line_id": "line", "paired_line_id": "line",
    "target_bank_id": "bank", "bank_id": "bank", "bank_account_id": "bank", "from_bank_id": "bank", "to_bank_id": "bank",
    "statement_id": "session", "through_statement_id": "session", "session_id": "session",
    "job_id": "job", "doc_id": "doc", "je_ids": "entry",
    "group_keys": "group", "file_ids": "file",
    "account_code": "account", "target_account_code": "account", "contact": "contact", "contact_id": "contact", "fund_id": "fund", "rule_id": "rule",
}

SETTLED = ("matched", "posted")
_OWN_ENTRY_TYPES = {"recon_create", "recon_split", "recon_transfer", "recon_suspense", "recon_reclass"}
# The entry id a line's own posting takes, by entry type, before ``next_je_id`` numbers retries.
_ENTRY_BASE = {"recon_create": "je:recon:", "recon_split": "je:recon:split:", "recon_suspense": "je:recon:suspense:",
               "recon_transfer": "je:recon:transfer:", "recon_reclass": "je:recon:reclass:"}


# What a reclassification carries over from a Suspense line: its amounts and fund. The
# posting engine stamps roles and gift answers on the new entry itself.
_MOVED_FIELDS = ("account", "debit", "credit", "fx_debit", "fx_credit", "fx_currency", "fx_rate", "fund_id")


# What a stored statement or entry amount can hold (Numeric(20, 4)).
MONEY_LIMIT = Decimal(10) ** 16


def money_value(value, currency: str, *, zero_ok: bool = False) -> Decimal:
    """``value`` as money in ``currency``: refused when it is zero (unless ``zero_ok``),
    larger than the books can hold, or finer than the currency's minor unit, never rounded
    into another figure."""
    from celerp.services.money import round_money, to_decimal

    amount = to_decimal(value)
    if not amount.is_finite() or abs(amount) >= MONEY_LIMIT:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_amount_too_large"))
    if amount == 0 and not zero_ok:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_amount_zero"))
    if round_money(amount, currency) != amount:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_amount_minor_unit", currency=currency))
    return amount


def _not_found(kind: str) -> HTTPException:
    return HTTPException(status_code=404, detail=catalog_refusal(
        "acct.err_statement_line_not_found" if kind == "line"
        else "acct.err_bank_account_not_found" if kind == "bank" else "acct.err_recon_not_found"))


async def scoped(db: AsyncSession, company_id: uuid.UUID, kind: str, ids: list, *, bank: BankAccount | None = None,
                 state: tuple[str, ...] | None = None, state_key: str = "acct.err_recon_line_state") -> list:
    """The records ``ids`` name, each the caller company's (and, for a line or statement, the
    path bank's), lines read under their row lock in id order. A foreign id gets the same 404
    as an unknown one; a line outside ``state`` is refused with its status under ``state_key``
    (the default fits a match-side call, where undoing first is the way out; a caller whose
    only possible mismatch is a status that names the caller's own action, like unmatch on an
    already-unmatched line, passes its own key)."""
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
    else:
        raise ValueError(kind)
    if len(rows) != len(want):
        raise _not_found(kind)
    if state is not None and kind == "line":
        for r in rows:
            if r.status not in state:
                raise HTTPException(status_code=409, detail=catalog_refusal(state_key, status=r.status))
    return rows


def _uuids(ids: set[str]) -> list[uuid.UUID]:
    out = []
    for value in ids:
        try:
            out.append(uuid.UUID(value))
        except ValueError:
            continue
    return out


async def record_names(*, session: AsyncSession, company_id: uuid.UUID, settings: dict, role: str,
                       ids: set[str]) -> dict[str, str]:
    """The names a confirm card shows for the company's bank accounts, statement lines and
    journal entries (date, description or memo, and amount), for a role that may open the bank
    pages: the bank routes need manage_accounting, and so does a record's name on a card."""
    from celerp.services.permissions import role_has_permission

    keys, entries = _uuids(ids), [i for i in ids if i.startswith("je:")]
    if not (keys or entries) or not role_has_permission(settings, role, "manage_accounting"):
        return {}
    names = await _entry_names(session, company_id, settings, entries) if entries else {}
    if not keys:
        return names
    names |= {str(bank_id): f"{bank_name} ({number})" if number else bank_name
              for bank_id, bank_name, number in (await session.execute(
                 select(BankAccount.id, BankAccount.bank_name, BankAccount.account_number).where(
                     BankAccount.company_id == company_id, BankAccount.id.in_(keys)))).all()}
    rows = (await session.execute(
        select(BankStatementLine.id, BankStatementLine.line_date, BankStatementLine.description,
               BankStatementLine.amount).where(
            BankStatementLine.company_id == company_id, BankStatementLine.id.in_(keys)))).all()
    names.update({str(line_id): f"{line_date}, {description}, {Decimal(amount):.2f}"
                  for line_id, line_date, description, amount in rows})
    return names


async def _entry_names(session: AsyncSession, company_id: uuid.UUID, settings: dict,
                       entries: list[str]) -> dict[str, str]:
    """Each journal entry by its date, memo and the amount its bank line moves the bank, in the
    bank's currency and signed as the statement shows it, so it reads like the line it is set
    against. An entry with no bank line, or one in a currency the bank cannot read, shows no amount."""
    from celerp.services.money import books_currency
    from celerp_accounting.routes import _bank_line_amounts

    base = books_currency(settings)
    banks = {b.chart_account_code: b for b in (await session.execute(
        select(BankAccount).where(BankAccount.company_id == company_id))).scalars()}
    out = {}
    for je_id, state in (await session.execute(select(Projection.entity_id, Projection.state).where(
            Projection.company_id == company_id, Projection.entity_type == "journal_entry",
            Projection.entity_id.in_(entries)))).all():
        state = state or {}
        moved = next((amounts for line in state.get("entries") or [] if (bank := banks.get(line.get("account")))
                      and (amounts := _bank_line_amounts(bank, base, je_id, line)) is not None), None)
        parts = [str(state.get("ts") or "")[:10], str(state.get("memo") or "").strip(),
                 f"{moved[0] - moved[1]:.2f}" if moved else ""]
        out[je_id] = ", ".join(p for p in parts if p)
    return out


async def chart_account(db: AsyncSession, company_id: uuid.UUID, code: str) -> None:
    """Refuse ``code`` unless it is an active account in the company's chart."""
    from celerp_accounting.models import Account

    active = (await db.execute(select(Account.is_active).where(
        Account.company_id == company_id, Account.code == code))).scalar_one_or_none()
    if active is None:
        raise HTTPException(status_code=404, detail=catalog_refusal("acct.err_account_unknown", code=code))
    if not active:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_account_archived", code=code))


def suggestion_value(suggestion: dict) -> dict:
    """``suggestion`` as stored: a contact stays only when its evidence names the record that
    names that contact (an open document, a person's or an answer's rule, a read receipt,
    history on a named counterparty, or a client answer); a posting with no contact says why,
    once however often the stored value is written again."""
    from ui.i18n import t
    s = {k: v for k, v in suggestion.items() if v is not None or k == "kind"}
    if s.get("contact_id") and not (s.get("evidence") or {}).get("names_contact"):
        s.pop("contact_id")
    why = t("acct.recon_no_contact_named")
    if s.get("kind") == "post" and not s.get("contact_id") and why not in (s.get("reason") or ""):
        s["reason"] = f"{s.get('reason') or ''} {why}".strip()
    return s


def write_suggestion(line: BankStatementLine, suggestion: dict | None) -> None:
    """The one writer of a line's suggestion."""
    line.suggestion = None if suggestion is None else suggestion_value(suggestion)


async def _session(db: AsyncSession, line: BankStatementLine) -> ReconciliationSession:
    """The line's statement under its lock, refused unless it can still change."""
    from celerp_accounting.routes import _lock_recon
    recon, _bank = await _lock_recon(db, line.reconciliation_id, line.company_id, editable=True)
    return recon


def completed(recon: ReconciliationSession) -> bool:
    return recon.status == "completed"


def complete(recon: ReconciliationSession) -> None:
    from datetime import datetime, timezone

    recon.status = "completed"
    recon.completed_at = datetime.now(timezone.utc)


def carry_in(recon: ReconciliationSession, je_ids: list[str]) -> None:
    """The bank's first statement's list with its carry-in as the books stand (bank_statements._first_counted)."""
    recon.reconciled_je_ids = list(je_ids)


def hold(recon: ReconciliationSession, je_ids: list[str]) -> None:
    held = list(recon.reconciled_je_ids or [])
    recon.reconciled_je_ids = held + [j for j in je_ids if j not in held]


async def _release(db: AsyncSession, recon: ReconciliationSession, line: BankStatementLine) -> None:
    """``line`` lets go of its entries: the statement keeps any another of its lines still
    holds (several lines matched to one entry), so the two never disagree."""
    others = {je for ids in (await db.execute(select(BankStatementLine.matched_je_ids).where(
        BankStatementLine.reconciliation_id == recon.id, BankStatementLine.id != line.id))).scalars()
        for je in ids or []}
    drop = set(line.matched_je_ids or []) - others
    recon.reconciled_je_ids = [j for j in (recon.reconciled_je_ids or []) if j not in drop]


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
    """Create and post one entry for ``line``; the event keys derive from the request key.
    A key already used for another entry (one an undo voided) is refused, so a line is
    never settled to an entry that was not written."""
    meta = {"recon_session_id": str(line.reconciliation_id), "statement_line_id": str(line.id)}
    for event_type, data, suffix, metadata in (
            ("acc.journal_entry.created", {"memo": memo, "ts": entry_date, "entries": entries, "je_type": je_type},
             "c", meta),
            ("acc.journal_entry.posted", {"ts": entry_date}, "p", {})):
        event = await emit_event(db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
                                 event_type=event_type, data=data, actor_id=actor_id, location_id=None,
                                 source="reconciliation", idempotency_key=f"{key}:{line.id}:{suffix}",
                                 metadata_=metadata)
        if getattr(event, "was_deduped", False) and (event.entity_id, event.event_type) != (je_id, event_type):
            raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_entry_exists"))


async def post_line(db: AsyncSession, *, company_id: uuid.UUID, actor_id, bank: BankAccount, line: BankStatementLine,
                    offsets: list[tuple[dict, Decimal]], rate: float | None, memo: str | None, entry_date: str | None,
                    key: str, je_type: str | None = None) -> list[str]:
    """Post ``line`` against ``offsets`` (one, or several for a split) and hold the entry.
    The offsets must total the line; each carries its account and optional contact and fund."""
    from celerp_accounting import import_service
    from celerp_accounting.routes import (_bank_entries, _bank_rate, _base_currency, _money_text,
                                          _pooled_offsets, require_destinations)

    recon = await _session(db, line)
    amount = abs(Decimal(str(line.amount)))
    total = sum((a for _, a in offsets), Decimal(0))
    if total != amount:
        raise HTTPException(status_code=422, detail=catalog_refusal(
            "acct.err_split_total", total=_money_text(total, bank.currency), amount=_money_text(amount, bank.currency)))
    await require_destinations(db, company_id, {o["account"] for o, _ in offsets})
    je_type = je_type or ("recon_split" if len(offsets) > 1 else "recon_create")
    je_id = await next_je_id(db, company_id, f"{_ENTRY_BASE[je_type]}{line.id}")
    when = entry_date or line.line_date
    base = await _base_currency(db, company_id)
    fx = _bank_rate(bank, base, rate)
    pooled = await _pooled_offsets(db, company_id, bank, base, fx, offsets, inflow=line.amount > 0, as_of=when)
    entries = _bank_entries(bank, base, fx, pooled, inflow=line.amount > 0)
    await import_service.check_line_contacts(db, company_id, entries)
    await post_entry(db, company_id=company_id, actor_id=actor_id, line=line, je_id=je_id, je_type=je_type,
                     entries=entries, memo=memo or line.description, entry_date=when, key=key)
    _settle(recon, line, "posted", [je_id])
    return [je_id]


def _settle(recon: ReconciliationSession, line: BankStatementLine, status: str, je_ids: list[str]) -> None:
    line.status = status
    line.matched_je_ids = list(je_ids)
    hold(recon, je_ids)
    if status == "posted":
        recon.created_count = (recon.created_count or 0) + 1
    else:
        recon.manual_matched_count = (recon.manual_matched_count or 0) + 1


async def post_transfer(db: AsyncSession, *, company_id: uuid.UUID, actor_id, bank: BankAccount, line: BankStatementLine,
                        target: BankAccount, paired: BankStatementLine | None, rate: float | None,
                        to_amount: float | None, key: str) -> list[str]:
    """Post ``line`` as a transfer between its bank and ``target`` (one ``recon_transfer``
    entry) and match ``paired``, the target bank's line for the same money, to that entry.
    ``to_amount`` is what the target bank moved in its own currency when the two differ."""
    from celerp_accounting.routes import transfer_entries

    recon = await _session(db, line)
    amount = abs(Decimal(str(line.amount)))
    other = None if to_amount is None else Decimal(str(to_amount))
    when = line.line_date
    if line.amount < 0:
        entries = await transfer_entries(db, company_id, bank, target, amount, when, rate=rate,
                                         to_amount=None if other is None else float(other))
    else:
        if target.currency != bank.currency and other is None:
            raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_transfer_to_amount",
                                                                        currency=target.currency))
        entries = await transfer_entries(db, company_id, target, bank, other if other is not None else amount, when,
                                         rate=rate, to_amount=float(amount))
    je_id = await next_je_id(db, company_id, f"{_ENTRY_BASE['recon_transfer']}{line.id}")
    await post_entry(db, company_id=company_id, actor_id=actor_id, line=line, je_id=je_id, je_type="recon_transfer",
                     entries=entries, memo=line.description, entry_date=when, key=key)
    _settle(recon, line, "posted", [je_id])
    if paired is not None:
        _settle(await _session(db, paired), paired, "matched", [je_id])
    return [je_id]


async def post_parts(db: AsyncSession, *, company_id: uuid.UUID, actor_id, bank: BankAccount, line: BankStatementLine,
                     parts: list[tuple[dict, Decimal]], rate: float | None, key: str) -> str:
    """One entry for the parts of a document payment that are not the documents (a bank
    fee taken out, an overpayment kept): a part of the line's sign moves money the line's
    way, an opposite one the other way."""
    from celerp_accounting.routes import _bank_entries, _bank_rate, _base_currency, _pooled_offsets, require_destinations

    await require_destinations(db, company_id, {f["account"] for f, _ in parts})
    base = await _base_currency(db, company_id)
    fx = _bank_rate(bank, base, rate)
    entries: list[dict] = []
    for fields, amount in parts:
        inflow = (line.amount > 0) == (amount > 0)
        pooled = await _pooled_offsets(db, company_id, bank, base, fx, [(fields, abs(amount))], inflow=inflow,
                                       as_of=line.line_date)
        entries += _bank_entries(bank, base, fx, pooled, inflow=inflow)
    je_id = await next_je_id(db, company_id, f"{_ENTRY_BASE['recon_create']}{line.id}")
    await post_entry(db, company_id=company_id, actor_id=actor_id, line=line, je_id=je_id, je_type="recon_create",
                     entries=entries, memo=line.description, entry_date=line.line_date, key=key)
    return je_id


async def reclassify(db: AsyncSession, *, company_id: uuid.UUID, actor_id, line: BankStatementLine, account: str,
                     contact: str | None, suspense: str, key: str) -> str:
    """Move a line's Suspense posting to ``account``: each Suspense line of its entry is
    reversed and posted again on ``account`` with the same amounts and fund."""
    from celerp_accounting import import_service
    from celerp_accounting.routes import require_destinations

    await require_destinations(db, company_id, {account})
    row = await db.get(Projection, {"company_id": company_id, "entity_id": line.matched_je_ids[0]})
    entries: list[dict] = []
    for e in (row.state or {}).get("entries", []) if row is not None else []:
        if e.get("account") != suspense:
            continue
        money = {k: e[k] for k in _MOVED_FIELDS if k in e}
        back = {**money, "debit": e.get("credit"), "credit": e.get("debit")}
        if "fx_debit" in e:
            back.update({"fx_debit": e.get("fx_credit"), "fx_credit": e.get("fx_debit")})
        entries += [back, {**money, "account": account, **({"contact": contact} if contact else {})}]
    if not entries:
        raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_undo_entry_gone"))
    await import_service.check_line_contacts(db, company_id, entries)
    je_id = await next_je_id(db, company_id, f"{_ENTRY_BASE['recon_reclass']}{line.id}:1")
    await post_entry(db, company_id=company_id, actor_id=actor_id, line=line, je_id=je_id, je_type="recon_reclass",
                     entries=entries, memo=line.description, entry_date=line.line_date, key=key)
    line.matched_je_ids = list(line.matched_je_ids) + [je_id]
    return je_id


def settle_payment(recon: ReconciliationSession, line: BankStatementLine, je_ids: list[str]) -> None:
    """Hold the entries a document payment posted for ``line``."""
    _settle(recon, line, "posted", je_ids)


async def lines_holding(db: AsyncSession, company_id: uuid.UUID, bank_id, je_ids, *,
                        except_lines: list | None = None) -> set[str]:
    """Those of ``je_ids`` a statement line of the bank (other than ``except_lines``) is matched
    to: the one reading of an entry a line already counts, for a match and for the candidates a
    line is offered (bank_statements._held). Another bank's line leaves the entry free here."""
    if not je_ids:
        return set()
    q = select(BankStatementLine.matched_je_ids).where(
        BankStatementLine.company_id == company_id, BankStatementLine.bank_account_id == bank_id,
        holding_any(BankStatementLine.matched_je_ids, je_ids))
    if except_lines:
        q = q.where(BankStatementLine.id.notin_(except_lines))
    held: set[str] = set()
    for row in (await db.execute(q)).scalars():
        held.update(row or [])
    return held & set(je_ids)


async def match_lines(db: AsyncSession, *, company_id: uuid.UUID, bank: BankAccount, lines: list[BankStatementLine],
                      je_ids: list[str]) -> None:
    """Match ``lines`` (one or several) to book entries on the bank (one or several) whose
    bank-side total equals theirs. An entry another line already holds is refused."""
    from celerp_accounting.routes import _eligible_entries, _je_entries_for_account, _money_text, _refuse_ineligible

    if not je_ids:
        raise HTTPException(status_code=422, detail=catalog_refusal("acct.err_recon_match_needs_entry"))
    booked = sum((Decimal(str(e["amount"])) for e in await _je_entries_for_account(db, company_id, bank, entry_ids=je_ids)
                  if e["amount"] is not None), Decimal(0))
    stated = sum((Decimal(str(l.amount)) for l in lines), Decimal(0))
    if booked != stated:
        raise HTTPException(status_code=422, detail=catalog_refusal(
            "acct.err_recon_match_total", total=_money_text(booked, bank.currency),
            amount=_money_text(stated, bank.currency)))
    if await lines_holding(db, company_id, bank.id, je_ids, except_lines=[l.id for l in lines]):
        raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_book_entry_matched"))
    by_session: dict = {}
    for line in lines:
        by_session.setdefault(line.reconciliation_id, []).append(line)
    for group in by_session.values():
        recon = await _session(db, group[0])
        _refuse_ineligible(await _eligible_entries(db, company_id, bank, recon, je_ids,
                                                   line_ids=[l.id for l in group]))
    for line in lines:
        _settle(await _session(db, line), line, "matched", je_ids)


async def skip_line(db: AsyncSession, line: BankStatementLine) -> None:
    """Mark a line that needs no entry (a bank reversal pair, a line already in the books)."""
    await _session(db, line)
    line.status = "skipped"
    line.matched_je_ids = []


def payment_entry_id(doc_id: str, index: int) -> str:
    """The id of the entry a document's payment ``index`` posts."""
    return f"je:auto:{doc_id}:pay:{index}"


async def _doc_payment(db: AsyncSession, company_id: uuid.UUID, je_id: str, state: dict) -> tuple[str, int] | None:
    """The document and payment index whose payment posted the entry ``je_id``, else None:
    the entry names its document, and the payment is the one whose entry it is."""
    doc_id = state.get("record_id")
    doc = await db.get(Projection, {"company_id": company_id, "entity_id": doc_id}) if doc_id else None
    if doc is None or doc.entity_type != "doc":
        return None
    return next(((doc_id, p["index"]) for p in (doc.state or {}).get("payments") or []
                 if p.get("index") is not None and payment_entry_id(doc_id, p["index"]) == je_id), None)


async def unmatch_line(db: AsyncSession, *, company_id: uuid.UUID, actor_id, role: str,
                       line: BankStatementLine) -> None:
    """Let ``line`` go: its statement releases the entries first, so the completed-history
    check sees none held, then an entry the line posted itself is voided and a document
    payment it recorded is deleted the way any payment recorded by mistake is, all in the
    caller's transaction."""
    from celerp.services.je_keys import je_void_data

    recon = await _session(db, line)
    je_ids = list(line.matched_je_ids or [])
    await _release(db, recon, line)
    await db.flush()
    if line.status == "posted":
        for je_id in je_ids:
            row = await db.get(Projection, {"company_id": company_id, "entity_id": je_id})
            if row is None or row.entity_type != "journal_entry":
                raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_undo_entry_gone"))
            state = row.state or {}
            if state.get("status") == "void":
                continue
            if (paid := await _doc_payment(db, company_id, je_id, state)) is not None:
                await _delete_payment(db, company_id, actor_id, role, line, *paid)
                continue
            if state.get("je_type") not in _OWN_ENTRY_TYPES:
                raise HTTPException(status_code=409, detail=catalog_refusal("acct.err_recon_undo_foreign"))
            if state.get("je_type") == "recon_transfer":
                await _release_pairs(db, company_id, line, je_id)
            await emit_event(db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
                             event_type="acc.journal_entry.voided",
                             data=je_void_data("Reconciliation line unmatched", state),
                             actor_id=actor_id, location_id=None, source="reconciliation",
                             idempotency_key=f"{je_id}:reconciliation-unmatch:void", metadata_={})
    line.status = "unmatched"
    line.matched_je_ids = []


async def _delete_payment(db: AsyncSession, company_id: uuid.UUID, actor_id, role: str, line: BankStatementLine,
                          doc_id: str, index: int) -> None:
    """Delete the payment ``line`` recorded on ``doc_id``, under the same permission that recorded it.
    The caller (unmatch_lines) locked the company's recorded payments before the lines."""
    from celerp.services.permissions import admit_permission
    from celerp_docs.routes import delete_doc_payment

    await admit_permission(db, company_id, role, "record_payments")
    key = f"{payment_entry_id(doc_id, index)}:reconciliation-unmatch"
    await delete_doc_payment(db, str(company_id), doc_id, index, actor_id=actor_id,
                             reason="Reconciliation line unmatched", key=key, digest=f"reconciliation-unmatch:{line.id}",
                             source="reconciliation", recorded_locked=True)


async def paired_lines(db: AsyncSession, company_id: uuid.UUID, bank_id, je_id: str) -> list[uuid.UUID]:
    """The other banks' lines matched to the transfer entry ``je_id`` a line of ``bank_id`` posted."""
    rows = (await db.execute(select(BankStatementLine.id, BankStatementLine.matched_je_ids).where(
        BankStatementLine.company_id == company_id, BankStatementLine.status == "matched",
        BankStatementLine.bank_account_id != bank_id))).all()
    return [i for i, held in rows if je_id in (held or [])]


async def _release_pairs(db: AsyncSession, company_id: uuid.UUID, line: BankStatementLine, je_id: str) -> None:
    """Unmatch the other bank's line matched to the transfer ``line`` posted, before that
    entry is voided."""
    ids = await paired_lines(db, company_id, line.bank_account_id, je_id)
    for paired in (await scoped(db, company_id, "line", ids, state=("matched",)) if ids else []):
        await _release(db, await _session(db, paired), paired)
        paired.status = "unmatched"
        paired.matched_je_ids = []


# ── Questions for the client ────────────────────────────────────────────────

# The calls the bank code makes outside the database, each with the test of its failure path.
BANK_CALLS = {
    "send_email": "test_ask_client_email_failure_keeps_question",
    "notify_once": "test_ask_notice_rolls_back_with_question",
    "store_file": "test_answer_receipt_store_failure_keeps_form",
    "model_rung": "test_suggest_without_model_states_why",
    "apply_doc_payment": "test_pay_doc_refuses_over_open_balance",
}

QUESTIONS_URL = "/accounting/questions"
_TITLE_KEY = "acct.recon_questions_title"
# Worst first: the state an Ask reports for its emails is the worst one.
_EMAIL_STATES = ("failed", "unconfirmed", "sent")


async def client_users(db: AsyncSession, company_id: uuid.UUID) -> list:
    """The active members who answer questions and do not keep the books: their role holds
    answer_bank_questions and not manage_accounting."""
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User
    from celerp.services.permissions import resolved_grant_roles

    company = await db.get(Company, company_id)
    settings = company.settings if company else {}
    roles = resolved_grant_roles(settings, "answer_bank_questions") - resolved_grant_roles(settings,
                                                                                            "manage_accounting")
    if not roles:
        return []
    return list((await db.execute(select(User).join(UserCompany, UserCompany.user_id == User.id).where(
        UserCompany.company_id == company_id, UserCompany.is_active.is_(True), User.is_active.is_(True),
        UserCompany.role.in_(sorted(roles))).order_by(User.name, User.id))).scalars())


def asked(line: BankStatementLine) -> bool:
    """A question the client can see: asked and not withdrawn."""
    q = line.client_query or {}
    return bool(q.get("asked")) and not q.get("withdrawn")


def _open(line: BankStatementLine) -> bool:
    """A question the client can see and has not answered."""
    return asked(line) and not (line.client_query or {}).get("answered")


async def _open_questions(db: AsyncSession, company_id: uuid.UUID) -> int:
    rows = (await db.execute(select(BankStatementLine).where(
        BankStatementLine.company_id == company_id, BankStatementLine.client_query.is_not(None)))).scalars()
    return sum(1 for l in rows if _open(l))


def _no_client_user() -> HTTPException:
    return HTTPException(status_code=409, detail=catalog_refusal(
        "reconciliation.no_client_user", permission=catalog_refusal("perm.name.answer_bank_questions"),
        books=catalog_refusal("perm.name.manage_accounting")))


async def ask_plan(db: AsyncSession, *, company_id: uuid.UUID, lines: list[BankStatementLine], note: str) -> dict:
    """What an Ask about ``lines`` does, read before anything is written: the client members it
    tells and emails, and the notice it sends (a reminder when a line was already asked). The
    handler claims its key with this plan before the questions are written, so every run of the
    key emails what the first one planned."""
    members = await client_users(db, company_id)
    if not members:
        raise _no_client_user()
    reminder = max((l.client_query or {}).get("asks", 0) if asked(l) else 0 for l in lines)
    count = await _open_questions(db, company_id) + sum(1 for l in lines if not _open(l))
    return {"asked": [str(l.id) for l in lines], "note": note, "members": [str(m.id) for m in members],
            "body_key": "acct.recon_questions_reminder" if reminder else "acct.recon_questions_body",
            "params": {"count": count, "n": reminder}}


async def write_ask(db: AsyncSession, *, company_id: uuid.UUID, lines: list[BankStatementLine], plan: dict) -> None:
    """Write each line's question and tell the client members in Celerp, as ``plan`` says; the
    caller commits."""
    from datetime import datetime, timezone

    from celerp.notifications.service import notify_once
    from ui.i18n import t

    now = datetime.now(timezone.utc).isoformat()
    for line in lines:
        q = dict(line.client_query or {})
        line.client_query = {"asked": now, "note": plan["note"] or q.get("note") or "", "asks": q.get("asks", 0) + 1,
                             "answer": None, "contact_text": None, "answered": None, "withdrawn": None}
    title_key, body_key, params = _TITLE_KEY, plan["body_key"], plan["params"]
    title, body = str(t(title_key, "en")), str(t(body_key, "en", **params))
    for member in plan["members"]:
        await notify_once(db, company_id, "bank_questions", title, body, user_id=uuid.UUID(member),
                          action_url=QUESTIONS_URL, i18n={"title": title_key, "body": body_key, "params": params})


async def email_ask(db: AsyncSession, *, company_id: uuid.UUID, key: str) -> dict:
    """Email the members the Ask claimed under ``key`` names, through the one durable send
    (email.send_durably): each recipient's attempt is kept on the Ask's own record, so no
    request key names it, and committed before it goes out, with no lock held while it does.
    A member is emailed only while still answering bank questions here (client_users, read
    again at each send); the outcome names those left out. A run of the key answers each
    recipient as stored: an attempt cut off after it may have gone out is not sent again
    unless the relay that took it deduplicates its operation id (email.next_step). Refused,
    with nothing sent, when none of the members still answers."""
    from celerp.services import email as email_service

    sending: list[tuple[str, email_service.Attempt]] = []

    async def begin(session) -> list:
        claim = await _ask_claim(session, company_id, key)
        plan, done = claim.metadata_["result"], dict(claim.metadata_.get("email") or {})
        allowed = {str(u.id): u for u in await client_users(session, company_id)}
        if not done:
            if not any(m in allowed for m in plan["members"]):
                raise _no_client_user()
            done = {"message": await _ask_message(session, company_id, plan), "recipients": {}, "left_out": []}
        recipients, left_out = dict(done["recipients"]), list(done["left_out"])
        for member in plan["members"]:
            record = recipients.get(member)
            step = "new" if record is None else email_service.next_step(record)
            if step not in ("new", "replay") or member in left_out:
                continue
            if member not in allowed:
                left_out.append(member)
                continue
            to = allowed[member].email  # read now: a replay to an address the member left fails its digest
            attempt = email_service.Attempt(
                record["operation_id"] if step == "replay" else
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"celerp-bank-question:{company_id}:{key}:{member}")),
                {**done["message"], "to": to})
            if step == "replay" and email_service.payload_digest(attempt.message) != record["digest"]:
                continue
            recipients[member] = {**email_service.attempt_record(attempt, record), "to": to}
            sending.append((member, attempt))
        claim.metadata_ = {**claim.metadata_, "email": {**done, "recipients": recipients, "left_out": left_out}}
        return [attempt for _m, attempt in sending]

    async def settle(session, results: list) -> dict:
        claim = await _ask_claim(session, company_id, key)
        done = claim.metadata_["email"]
        recipients = dict(done["recipients"])
        for (member, attempt), result in zip(sending, results):
            record = recipients.get(member) or {}
            if record.get("operation_id") == attempt.operation_id and record.get("state") == "sending":
                recipients[member] = email_service.settled_record(record, result)
        claim.metadata_ = {**claim.metadata_, "email": {**done, "recipients": recipients}}
        return await _ask_outcome(session, claim.metadata_["result"], claim.metadata_["email"])

    return await email_service.send_durably(db, begin, settle)


async def _ask_claim(db: AsyncSession, company_id: uuid.UUID, key: str):
    """The Ask's claim under ``key``, held in the order every writer takes: the company, then its rows."""
    from celerp.models.ledger import LedgerEntry
    from celerp.services.company_lock import lock_company

    await lock_company(db, company_id)
    return (await db.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.idempotency_key == key).with_for_update())).scalar_one()


async def _ask_message(db: AsyncSession, company_id: uuid.UUID, plan: dict) -> dict:
    """The one email every recipient of the Ask gets, fixed before the first goes out."""
    from celerp.config import settings
    from celerp.models.company import Company
    from ui.i18n import t

    company = await db.get(Company, company_id)
    base = (settings.celerp_public_url or "").rstrip("/")
    link = f"{base}{QUESTIONS_URL}" if base else None
    text = "\n\n".join(filter(None, [str(t(plan["body_key"], **plan["params"])), plan["note"],
                                     link or str(t("acct.recon_questions_where"))]))
    return {"subject": str(t(_TITLE_KEY)), "body_html": "".join(f"<p>{_html(p)}</p>" for p in text.split("\n\n")),
            "body_text": text, "from_name": company.name if company else ""}


async def _ask_outcome(db: AsyncSession, plan: dict, done: dict) -> dict:
    """The Ask's answer, read from each recipient's stored attempt: one still sending reads as
    unconfirmed, since it may have gone out."""
    from datetime import datetime, timezone

    from celerp.models.company import User
    from celerp.services import email as email_service
    from ui.i18n import as_sentence, t

    ids = [uuid.UUID(m) for m in plan["members"]]
    names = {str(u.id): u.name for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars()}
    now = datetime.now(timezone.utc)
    rows = []
    for member in sorted(done["recipients"], key=lambda m: (names.get(m, ""), m)):
        record = done["recipients"][member]
        state = email_service.attempt_state(record, now)
        rows.append({"name": names.get(member, ""), "state": "unconfirmed" if state == "sending" else state,
                     "reason": record.get("reason"), "detail": record.get("detail")})
    worst = min((r["state"] for r in rows), key=_EMAIL_STATES.index, default="sent")
    failed = [r for r in rows if r["state"] != "sent"]
    count, one = len(plan["asked"]), len(plan["asked"]) == 1
    if not failed:
        outcome = t("acct.recon_asked_one" if one else "acct.recon_asked", count=count,
                    names=", ".join(r["name"] for r in rows))
    else:
        first = failed[0]
        reason = as_sentence(str(t(first["reason"])) if first["reason"] else first["detail"] or "")
        outcome = t(_ASKED_NOT_SENT[first["state"]][one], count=count, name=", ".join(r["name"] for r in failed),
                    reason=reason)
    if left_out := sorted(names.get(m, "") for m in done["left_out"]):
        outcome = f"{outcome} {t('acct.recon_asked_left_out', names=', '.join(left_out))}"
    return {"asked": plan["asked"], "outcome": str(outcome),
            "email": {"state": worst, "recipients": [{"name": r["name"], "state": r["state"]} for r in rows]}}


# The outcome of asking when an email did not go out, by send state, then by whether one line was asked.
_ASKED_NOT_SENT = {
    "failed": {False: "acct.recon_asked_email_failed", True: "acct.recon_asked_email_failed_one"},
    "unconfirmed": {False: "acct.recon_asked_email_unconfirmed", True: "acct.recon_asked_email_unconfirmed_one"},
}


def _html(text: str) -> str:
    from html import escape
    return escape(text).replace("\n", "<br>")


def withdraw_lines(lines: list[BankStatementLine]) -> None:
    """Take back the questions on ``lines``; each must be asked and not withdrawn."""
    from datetime import datetime, timezone

    for line in lines:
        if not asked(line):
            raise HTTPException(status_code=409, detail=catalog_refusal("reconciliation.line_not_asked"))
    now = datetime.now(timezone.utc).isoformat()
    for line in lines:
        line.client_query = {**line.client_query, "withdrawn": now}


async def attach_file(store, company_id: uuid.UUID, line: BankStatementLine, content: bytes, filename: str | None,
                      mime: str) -> dict:
    """Store a file on ``line`` under an id of the line and the content, so the same file
    attached again is the attachment the line already holds. Refused with 413 when the store
    does not take the file."""
    import hashlib

    from celerp.services.attachments import company_file_name, stored_file_name

    att_id = hashlib.sha256(str(line.id).encode() + content).hexdigest()[:32]
    name = stored_file_name(att_id, mime)
    held = next((u for u in line.attachment_ids or [] if name and company_file_name(company_id, u) == name), None)
    if held is not None:
        return {"id": name, "url": held, "filename": filename}
    try:
        meta = await store.file(content, filename, mime, att_id=att_id)
    except ValueError:
        raise HTTPException(status_code=413, detail=catalog_refusal("accounting.statement_attachment_refused"))
    line.attachment_ids = [*(line.attachment_ids or []), meta["url"]]
    return meta
