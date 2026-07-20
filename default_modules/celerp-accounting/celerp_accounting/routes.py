# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import math
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form as FastForm, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.constants import ISO_4217_CURRENCIES
from celerp_accounting.models import Account, BankAccount, BankStatementLine, ReconciliationRule, ReconciliationSession
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user, require_manager, viewer_read_only
from celerp.services.je_keys import je_void_data
from celerp.services.money import round_money, to_decimal, to_stored_float

router = APIRouter(dependencies=[Depends(get_current_user), Depends(viewer_read_only)])

# Default Thai chart of accounts seeded on company creation.
# Follows Thai Accounting Standards (TAS) structure.
THAI_CHART_OF_ACCOUNTS: list[dict] = [
    # --- Assets ---
    {"code": "1000", "name": "Assets", "account_type": "asset", "parent_code": None},
    {"code": "1100", "name": "Current Assets", "account_type": "asset", "parent_code": "1000"},
    {"code": "1110", "name": "Cash and Cash Equivalents", "account_type": "asset", "parent_code": "1100"},
    {"code": "1120", "name": "Accounts Receivable", "account_type": "asset", "parent_code": "1100"},
    {"code": "1130", "name": "Inventory", "account_type": "asset", "parent_code": "1100"},
    {"code": "1130-P", "name": "Inventory - Purchased", "account_type": "asset", "parent_code": "1130"},
    {"code": "1130-OB", "name": "Inventory - Opening Balance", "account_type": "asset", "parent_code": "1130"},
    {"code": "1130-FRT", "name": "Inventory - Freight Clearing", "account_type": "asset", "parent_code": "1130"},
    {"code": "1130-INS", "name": "Inventory - Insurance Clearing", "account_type": "asset", "parent_code": "1130"},
    {"code": "1130-DTY", "name": "Inventory - Import Duty Clearing", "account_type": "asset", "parent_code": "1130"},
    {"code": "1130-IVT", "name": "Inventory - Non-Recoverable Import VAT Clearing", "account_type": "asset", "parent_code": "1130"},
    {"code": "1140", "name": "Prepaid Expenses", "account_type": "asset", "parent_code": "1100"},
    {"code": "1150", "name": "VAT Receivable (Input VAT)", "account_type": "asset", "parent_code": "1100"},
    {"code": "1200", "name": "Non-Current Assets", "account_type": "asset", "parent_code": "1000"},
    {"code": "1210", "name": "Property, Plant and Equipment", "account_type": "asset", "parent_code": "1200"},
    {"code": "1220", "name": "Accumulated Depreciation", "account_type": "asset", "parent_code": "1200"},
    {"code": "1230", "name": "Intangible Assets", "account_type": "asset", "parent_code": "1200"},
    # --- Liabilities ---
    {"code": "2000", "name": "Liabilities", "account_type": "liability", "parent_code": None},
    {"code": "2100", "name": "Current Liabilities", "account_type": "liability", "parent_code": "2000"},
    {"code": "2110", "name": "Accounts Payable", "account_type": "liability", "parent_code": "2100"},
    {"code": "2120", "name": "VAT Payable (Output VAT)", "account_type": "liability", "parent_code": "2100"},
    {"code": "2130", "name": "Withholding Tax Payable", "account_type": "liability", "parent_code": "2100"},
    {"code": "2140", "name": "Accrued Expenses", "account_type": "liability", "parent_code": "2100"},
    {"code": "2150", "name": "Social Security Payable", "account_type": "liability", "parent_code": "2100"},
    {"code": "2200", "name": "Non-Current Liabilities", "account_type": "liability", "parent_code": "2000"},
    {"code": "2210", "name": "Long-term Loans", "account_type": "liability", "parent_code": "2200"},
    # --- Equity ---
    {"code": "3000", "name": "Equity", "account_type": "equity", "parent_code": None},
    {"code": "3100", "name": "Registered Capital", "account_type": "equity", "parent_code": "3000"},
    {"code": "3200", "name": "Retained Earnings", "account_type": "equity", "parent_code": "3000"},
    {"code": "3300", "name": "Current Year Earnings", "account_type": "equity", "parent_code": "3000"},
    # --- Revenue ---
    {"code": "4000", "name": "Revenue", "account_type": "revenue", "parent_code": None},
    {"code": "4100", "name": "Sales Revenue", "account_type": "revenue", "parent_code": "4000"},
    {"code": "4200", "name": "Service Revenue", "account_type": "revenue", "parent_code": "4000"},
    {"code": "4300", "name": "Other Income", "account_type": "revenue", "parent_code": "4000"},
    {"code": "4400", "name": "Interest Income", "account_type": "revenue", "parent_code": "4000"},
    # --- COGS ---
    {"code": "5000", "name": "Cost of Goods Sold", "account_type": "cogs", "parent_code": None},
    {"code": "5100", "name": "Cost of Goods Sold", "account_type": "cogs", "parent_code": "5000"},
    {"code": "5200", "name": "Direct Labor", "account_type": "cogs", "parent_code": "5000"},
    {"code": "5300", "name": "Manufacturing Overhead", "account_type": "cogs", "parent_code": "5000"},
    # --- Expenses ---
    {"code": "6000", "name": "Operating Expenses", "account_type": "expense", "parent_code": None},
    {"code": "6100", "name": "Salaries and Wages", "account_type": "expense", "parent_code": "6000"},
    {"code": "6200", "name": "Rent", "account_type": "expense", "parent_code": "6000"},
    {"code": "6300", "name": "Utilities", "account_type": "expense", "parent_code": "6000"},
    {"code": "6400", "name": "Depreciation", "account_type": "expense", "parent_code": "6000"},
    {"code": "6500", "name": "Professional Fees", "account_type": "expense", "parent_code": "6000"},
    {"code": "6600", "name": "Marketing and Advertising", "account_type": "expense", "parent_code": "6000"},
    {"code": "6700", "name": "Insurance", "account_type": "expense", "parent_code": "6000"},
    {"code": "6800", "name": "Office Supplies", "account_type": "expense", "parent_code": "6000"},
    {"code": "6900", "name": "Travel and Transportation", "account_type": "expense", "parent_code": "6000"},
    {"code": "6950", "name": "Miscellaneous Expenses", "account_type": "expense", "parent_code": "6000"},
]


class AccountCreate(BaseModel):
    code: str
    name: str
    account_type: str  # asset|liability|equity|revenue|expense|cogs
    parent_code: str | None = None


class AccountPatch(BaseModel):
    name: str | None = None
    account_type: str | None = None
    parent_code: str | None = None
    is_active: bool | None = None


class AccImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


class AccBatchImportRequest(BaseModel):
    records: list[AccImportRecord]


class BatchImportResult(BaseModel):
    created: int
    skipped: int
    updated: int = 0
    errors: list[str]


async def seed_chart_of_accounts(session: AsyncSession, company_id: uuid.UUID) -> None:
    """Seed Thai default chart of accounts for a new company. Idempotent."""
    from sqlalchemy import select as _select

    existing_codes = set(
        (await session.execute(
            _select(Account.code).where(Account.company_id == company_id)
        )).scalars().all()
    )
    for entry in THAI_CHART_OF_ACCOUNTS:
        if entry["code"] in existing_codes:
            continue
        acc = Account(
            id=uuid.uuid4(),
            company_id=company_id,
            code=entry["code"],
            name=entry["name"],
            account_type=entry["account_type"],
            parent_code=entry["parent_code"],
        )
        session.add(acc)


async def _seed_default_bank_account(session: AsyncSession, company_id: uuid.UUID) -> None:
    """Create a default bank account so reconciliation is never empty. Idempotent."""
    from celerp.models.company import Company
    from sqlalchemy import select as _select

    company = await session.get(Company, company_id)
    currency = (company.settings or {}).get("currency", "THB") if company else "THB"

    code = "1111"
    existing = (await session.execute(
        _select(Account.id).where(Account.company_id == company_id, Account.code == code)
    )).scalar_one_or_none()
    if existing:
        return

    acc = Account(
        id=uuid.uuid4(),
        company_id=company_id,
        code=code,
        name="Default Bank Account (Checking)",
        account_type="asset",
        parent_code="1110",
    )
    session.add(acc)

    bank = BankAccount(
        id=uuid.uuid4(),
        company_id=company_id,
        chart_account_code=code,
        bank_name="Default Bank Account",
        account_number="",
        bank_type="checking",
        currency=currency,
        opening_balance=0.0,
    )
    session.add(bank)


async def seed_chart_of_accounts_hook(*, session: AsyncSession, company_id: uuid.UUID) -> None:
    """Lifecycle hook called via on_company_created slot."""
    await seed_chart_of_accounts(session, company_id)
    await _seed_default_bank_account(session, company_id)


async def backfill_chart_of_accounts_hook(*, session: AsyncSession) -> None:
    """Lifecycle hook called via on_modules_ready slot.

    Seeds the chart of accounts for any existing company that has none yet.
    This handles the case where accounting is enabled after the company was
    already created (e.g. first-run with no modules, then preset applied).
    """
    from celerp.models.company import Company
    from sqlalchemy import select as _select

    companies = (await session.execute(_select(Company))).scalars().all()
    for company in companies:
        has_accounts = (await session.execute(
            _select(Account.id).where(Account.company_id == company.id).limit(1)
        )).scalar_one_or_none()
        if has_accounts:
            continue
        await seed_chart_of_accounts(session, company.id)
        await _seed_default_bank_account(session, company.id)


def _account_to_dict(acc: Account) -> dict:
    return {
        "id": str(acc.id),
        "code": acc.code,
        "name": acc.name,
        "account_type": acc.account_type,
        "parent_code": acc.parent_code,
        "is_active": acc.is_active,
    }


@router.get("/chart")
async def get_chart(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return all accounts sorted by code."""
    rows = (
        await session.execute(
            select(Account).where(Account.company_id == company_id).order_by(Account.code)
        )
    ).scalars().all()
    items = [_account_to_dict(a) for a in rows]
    return {"items": items, "total": len(items)}


@router.post("/chart/seed")
async def seed_chart_endpoint(
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Seed the default chart of accounts for this company. Only adds missing accounts."""
    existing_codes = set(
        (await session.execute(
            select(Account.code).where(Account.company_id == company_id)
        )).scalars().all()
    )
    added = 0
    for entry in THAI_CHART_OF_ACCOUNTS:
        if entry["code"] not in existing_codes:
            session.add(Account(
                id=uuid.uuid4(),
                company_id=company_id,
                code=entry["code"],
                name=entry["name"],
                account_type=entry["account_type"],
                parent_code=entry["parent_code"],
            ))
            added += 1
    # Ensure at least one bank account exists (backfill for existing companies)
    existing_bank = (
        await session.execute(
            select(BankAccount.id).where(BankAccount.company_id == company_id).limit(1)
        )
    ).scalar_one_or_none()
    if not existing_bank:
        await _seed_default_bank_account(session, company_id)

    await session.commit()
    return {"added": added, "already_existed": len(existing_codes)}


@router.post("/accounts")
async def create_account(
    payload: AccountCreate,
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    existing = (
        await session.execute(
            select(Account).where(Account.company_id == company_id, Account.code == payload.code)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Account code {payload.code} already exists")

    acc = Account(
        id=uuid.uuid4(),
        company_id=company_id,
        code=payload.code,
        name=payload.name,
        account_type=payload.account_type,
        parent_code=payload.parent_code,
    )
    session.add(acc)
    await session.commit()
    return _account_to_dict(acc)


@router.patch("/accounts/{code}")
async def patch_account(
    code: str,
    payload: AccountPatch,
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    acc = (
        await session.execute(
            select(Account).where(Account.company_id == company_id, Account.code == code)
        )
    ).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    if payload.name is not None:
        acc.name = payload.name
    if payload.account_type is not None:
        acc.account_type = payload.account_type
    if payload.parent_code is not None:
        acc.parent_code = payload.parent_code
    if payload.is_active is not None:
        acc.is_active = payload.is_active

    await session.commit()
    return _account_to_dict(acc)


@router.get("/import/template", response_class=PlainTextResponse, include_in_schema=False)
async def import_accounting_template():
    return PlainTextResponse(
        "entity_id,event_type,idempotency_key,code,name,account_type,parent_code,is_active\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounting.csv"},
    )


@router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_accounting(
    body: AccBatchImportRequest,
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = Depends(require_manager),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    from sqlalchemy import select as _select
    from celerp.models.ledger import LedgerEntry

    keys = [r.idempotency_key for r in body.records]
    existing_keys = set((await session.execute(
        _select(LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.idempotency_key.in_(keys),
        )
    )).scalars().all())

    create_entity_ids = [r.entity_id for r in body.records if r.event_type == "acc.journal_entry.created"]
    existing_entities: set[str] = set()
    if create_entity_ids:
        existing_entities = set((await session.execute(
            _select(Projection.entity_id).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(create_entity_ids),
            )
        )).scalars().all())

    created = skipped = 0
    errors: list[str] = []
    for rec in body.records:
        if rec.idempotency_key in existing_keys:
            skipped += 1
            continue
        if rec.event_type == "acc.journal_entry.created" and rec.entity_id in existing_entities:
            skipped += 1
            continue
        try:
            await emit_event(
                session,
                company_id=company_id,
                entity_id=rec.entity_id,
                entity_type="journal_entry",
                event_type=rec.event_type,
                data=rec.data,
                actor_id=user.id,
                location_id=None,
                source=rec.source,
                idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing_keys.add(rec.idempotency_key)
            if rec.event_type == "acc.journal_entry.created":
                existing_entities.add(rec.entity_id)
            created += 1
        except Exception as exc:
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: {exc}")

    await session.commit()
    return BatchImportResult(created=created, skipped=skipped, errors=errors)


# ---------------------------------------------------------------------------
# Accounting reports - derived from journal_entry projections
# ---------------------------------------------------------------------------

async def _je_rows(
    session: AsyncSession, company_id: uuid.UUID, *, include_void: bool = False
) -> list[tuple[str, dict, str]]:
    """All journal entry projections as (entity_id, state, date) tuples.

    date is the entry's effective ISO date (YYYY-MM-DD, "" when the entry has none).
    Posted entries only by default; include_void=True adds voided entries for views
    that must show the full record (the journal). Deliberately not date-filtered:
    the general ledger buckets one scan into opening (before date_from) and period
    ranges, so callers apply their own date filtering.
    """
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()
    out: list[tuple[str, dict, str]] = []
    for row in rows:
        state = row.state
        status = state.get("status")
        if status != "posted" and not (include_void and status == "void"):
            continue
        ts_raw = state.get("ts") or state.get("created_at") or ""
        out.append((row.entity_id, state, str(ts_raw)[:10] if ts_raw else ""))
    return out


async def _base_currency(session: AsyncSession, company_id: uuid.UUID) -> str:
    """Company base currency for report amounts."""
    from celerp.models.company import Company

    company = await session.get(Company, company_id)
    return (company.settings or {}).get("currency", "USD") if company else "USD"


def _id_chunks(ids: list[str], size: int = 10_000):
    """Slices for IN() clauses: asyncpg caps a statement at 32767 bind
    arguments, and a mature company's id lists can exceed that."""
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


async def _je_doc_refs(session: AsyncSession, company_id: uuid.UUID, je_ids: list[str]) -> dict[str, dict]:
    """Source-doc display info per journal entry: {je_id: {"doc_id", "doc_ref", "fx"}}.

    Only JEs whose creation event carries a doc_id in its ledger metadata appear;
    doc_ref falls back to the raw doc_id when the doc projection is gone.
    fx is {"currency", "rate"} when the linked doc was recorded in a foreign
    currency with a stored conversion rate, else None - a missing rate leaves fx
    empty rather than guessing. Payment JEs (metadata carries payment_index)
    resolve currency/rate from that specific payment, since a payment may
    legitimately use a different rate than its invoice.
    """
    from celerp.models.ledger import LedgerEntry

    if not je_ids:
        return {}
    ledger_events = []
    for chunk in _id_chunks(je_ids):
        ledger_events.extend((
            await session.execute(
                select(LedgerEntry).where(
                    LedgerEntry.company_id == company_id,
                    LedgerEntry.event_type == "acc.journal_entry.created",
                    LedgerEntry.entity_id.in_(chunk),
                )
            )
        ).scalars().all())
    je_meta: dict[str, dict] = {}
    je_ts: dict[str, str] = {}
    for ev in ledger_events:
        meta = ev.metadata_ or {}
        if meta.get("doc_id"):
            je_meta[ev.entity_id] = meta
            je_ts[ev.entity_id] = str((ev.data or {}).get("ts") or "")[:10]

    doc_ids = sorted({m["doc_id"] for m in je_meta.values()})
    doc_states: dict[str, dict] = {}
    for chunk in _id_chunks(doc_ids):
        doc_rows = (
            await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_id.in_(chunk),
                )
            )
        ).scalars().all()
        doc_states.update({dr.entity_id: dr.state for dr in doc_rows})

    base = await _base_currency(session, company_id)
    refs: dict[str, dict] = {}
    for je_id, meta in je_meta.items():
        doc_id = meta["doc_id"]
        state = doc_states.get(doc_id, {})
        currency = state.get("currency")
        rate = state.get("conversion_rate")
        payment_index = meta.get("payment_index")
        if isinstance(payment_index, int) and meta.get("trigger") in ("doc.payment.received", "doc.payment.voided"):
            payments = state.get("payments", [])
            # Payments are identified by their index FIELD (stable since
            # deletions tombstone in place). Projections compacted before that
            # change may hold rewritten fields pointing at a different payment,
            # so the JE's date (the payment date it was posted for) is checked;
            # on mismatch fall back to the doc-level rate rather than show
            # another payment's.
            payment = next((p for p in payments if p.get("index") == payment_index), None)
            if payment is not None:
                ev_ts = je_ts.get(je_id, "")
                p_date = str(payment.get("payment_date") or "")[:10]
                if not ev_ts or not p_date or ev_ts == p_date:
                    currency = payment.get("currency") or currency
                    rate = payment.get("conversion_rate") or rate
        fx = None
        if currency and currency != base and rate:
            fx = {"currency": currency, "rate": float(rate)}
        refs[je_id] = {
            "doc_id": doc_id,
            "doc_ref": state.get("ref_id") or state.get("doc_number") or doc_id,
            "fx": fx,
        }
    return refs


def _line_amounts(entry: dict) -> tuple[Decimal, Decimal] | None:
    """(debit, credit) of a JE line as Decimals, or None for a malformed line.

    One rule for every report: a line whose amounts cannot be parsed is skipped
    the same way everywhere, so the trial balance, journal, ledger, and general
    ledger always agree on which lines count.
    """
    try:
        return (Decimal(str(entry.get("debit") or 0)), Decimal(str(entry.get("credit") or 0)))
    except Exception:
        return None


def _build_balances(posted: list[tuple[str, dict, str]], date_from: str | None, date_to: str | None) -> dict[str, Decimal]:
    """Aggregate net balance per account_code from posted journal entries (_je_rows output).

    Returns {account_code: net_balance} where net_balance = total_debit - total_credit.
    Asset/Expense accounts are debit-normal (positive = debit balance).
    Liability/Equity/Revenue accounts are credit-normal (positive = credit balance, stored as positive here).
    We store the raw difference and let the report layer interpret sign conventions.
    """
    balances: dict[str, Decimal] = {}
    for _, state, ts in posted:
        if date_from and ts < date_from:
            continue
        if date_to and ts > date_to:
            continue
        for entry in state.get("entries", []):
            code = entry.get("account")
            amounts = _line_amounts(entry)
            if not code or amounts is None:
                continue
            balances[code] = balances.get(code, Decimal(0)) + amounts[0] - amounts[1]
    return balances


@router.get("/journal")
async def journal(
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Journal: every entry with its lines, source-doc link, and FX info.

    Voided entries stay visible flagged status="void" - a journal is a record, and
    hiding voids would misstate it - but they are excluded from the period totals.
    """
    rows = await _je_rows(session, company_id, include_void=True)
    # Dateless JEs are always included, matching the per-account ledger.
    rows = [
        r for r in rows
        if not (r[2] and date_from and r[2] < date_from)
        and not (r[2] and date_to and r[2] > date_to)
    ]
    # Sort by date with entity_id as the deterministic, replay-stable tiebreak.
    rows.sort(key=lambda r: (r[2], r[0]))

    accounts = (
        await session.execute(
            select(Account).where(Account.company_id == company_id)
        )
    ).scalars().all()
    account_names = {a.code: a.name for a in accounts}
    refs = await _je_doc_refs(session, company_id, [je_id for je_id, _, _ in rows])
    base = await _base_currency(session, company_id)

    total_debit = Decimal(0)
    total_credit = Decimal(0)
    entries_out = []
    for je_id, state, ts in rows:
        posted = state.get("status") == "posted"
        lines = []
        for entry in state.get("entries", []):
            code = entry.get("account")
            amounts = _line_amounts(entry)
            if amounts is None:
                continue
            if posted:
                total_debit += amounts[0]
                total_credit += amounts[1]
            lines.append({
                "account": code,
                "name": account_names.get(code, code),
                "debit": float(amounts[0]),
                "credit": float(amounts[1]),
            })
        ref = refs.get(je_id)
        entries_out.append({
            "je_id": je_id,
            "ts": ts,
            "memo": state.get("memo", ""),
            "status": state.get("status"),
            "je_type": state.get("je_type"),
            "void_reason": state.get("void_reason"),
            "source_doc": {"doc_id": ref["doc_id"], "doc_ref": ref["doc_ref"]} if ref else None,
            "lines": lines,
            "fx": ref["fx"] if ref else None,
        })

    return {
        "date_from": date_from,
        "date_to": date_to,
        "entries": entries_out,
        "total_debit": to_stored_float(round_money(total_debit, base)),
        "total_credit": to_stored_float(round_money(total_credit, base)),
    }


class ManualJELine(BaseModel):
    account: str
    debit: float = 0
    credit: float = 0


class ManualJECreate(BaseModel):
    ts: str  # ISO date "YYYY-MM-DD"
    memo: str = ""
    entries: list[ManualJELine]
    idempotency_token: str


class ManualJEVoidPayload(BaseModel):
    reason: str | None = None


@router.post("/journal-entries")
async def create_manual_journal_entry(
    payload: ManualJECreate,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Post a manual journal entry. Validates accounts and balance; the period
    lock is enforced by the event engine on the entry's date."""
    # Strict YYYY-MM-DD: reports filter and sort by lexical string compare, so
    # variants fromisoformat tolerates (basic 20260105, week dates) would sort
    # outside their real period and vanish from date-bounded reports.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload.ts or ""):
        raise HTTPException(status_code=422, detail="Invalid date format. Use YYYY-MM-DD.")
    try:
        date.fromisoformat(payload.ts)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format. Use YYYY-MM-DD.")
    if len(payload.entries) < 2:
        raise HTTPException(status_code=422, detail="A journal entry needs at least 2 lines.")
    if not payload.idempotency_token:
        raise HTTPException(status_code=422, detail="idempotency_token is required.")

    accounts = (
        await session.execute(
            select(Account).where(Account.company_id == company_id)
        )
    ).scalars().all()
    account_map = {a.code: a for a in accounts}
    children_of: dict[str, list[str]] = {}
    for a in accounts:
        if a.parent_code:
            children_of.setdefault(a.parent_code, []).append(a.code)

    base = await _base_currency(session, company_id)
    total_debit = Decimal(0)
    total_credit = Decimal(0)
    entries: list[dict] = []
    for line in payload.entries:
        acc = account_map.get(line.account)
        if not acc:
            raise HTTPException(status_code=422, detail=f"Unknown account {line.account}.")
        if not acc.is_active:
            raise HTTPException(status_code=422, detail=f"Account {line.account} is inactive.")
        children = children_of.get(line.account)
        if children:
            # Parent accounts are grouping rollups (the balance sheet sums their
            # children); postings belong on leaf accounts.
            raise HTTPException(
                status_code=422,
                detail=f"Account {line.account} is a parent account. Post to one of its sub-accounts: {', '.join(sorted(children))}.",
            )
        if not (math.isfinite(line.debit) and math.isfinite(line.credit)):
            raise HTTPException(status_code=422, detail="Debit and credit amounts must be finite numbers.")
        if line.debit < 0 or line.credit < 0:
            raise HTTPException(status_code=422, detail="Debit and credit amounts cannot be negative.")
        d = round_money(line.debit, base)
        c = round_money(line.credit, base)
        if d > 0 and c > 0:
            raise HTTPException(status_code=422, detail="Each line must have an amount on only one side, debit or credit.")
        if d == 0 and c == 0:
            raise HTTPException(status_code=422, detail="Each line needs a debit or credit amount.")
        total_debit += d
        total_credit += c
        entries.append({"account": line.account, "debit": to_stored_float(d), "credit": to_stored_float(c)})

    if total_debit != total_credit:
        raise HTTPException(
            status_code=422,
            detail=f"Entry is out of balance: debits {total_debit} do not equal credits {total_credit}.",
        )
    if total_debit == 0:
        raise HTTPException(status_code=422, detail="Entry total must be greater than zero.")

    je_id = f"je:manual:{uuid.uuid4()}"
    created = await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={"memo": payload.memo, "ts": payload.ts, "entries": entries, "je_type": "manual", "status": "posted"},
        actor_id=user.id,
        location_id=None,
        source="manual",
        idempotency_key=f"je:manual:{payload.idempotency_token}:c",
        metadata_={},
    )
    if getattr(created, "was_deduped", False):
        # The token was already used: emit_event returned the original event
        # instead of inserting. A byte-identical retry (double click, network
        # replay) answers with the original entry; a DIFFERENT payload on the
        # same token means the user edited and resubmitted a stale form, and a
        # success response would describe amounts that were never posted.
        orig = created.data or {}

        def _norm(lines: list) -> list:
            # Compare money as rounded Decimals so a byte-identical retry never
            # trips on float representation differences across storage layers.
            out = []
            for l in lines or []:
                amounts = _line_amounts(l) or (Decimal(0), Decimal(0))
                out.append((l.get("account"), round_money(amounts[0], base), round_money(amounts[1], base)))
            return out

        if (orig.get("ts") != payload.ts
                or (orig.get("memo") or "") != (payload.memo or "")
                or _norm(orig.get("entries")) != _norm(entries)):
            raise HTTPException(
                status_code=409,
                detail="This entry was already posted with different amounts. Review the journal, then post again if a new entry is intended.",
            )
        je_id = created.entity_id
    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.posted",
        data={"ts": payload.ts},
        actor_id=user.id,
        location_id=None,
        source="manual",
        idempotency_key=f"je:manual:{payload.idempotency_token}:p",
        metadata_={},
    )
    await session.commit()

    return {
        "je_id": je_id,
        "ts": payload.ts,
        "memo": payload.memo,
        "entries": entries,
        "je_type": "manual",
        "status": "posted",
    }


@router.post("/journal-entries/{entity_id}/void")
async def void_manual_journal_entry(
    entity_id: str,
    payload: ManualJEVoidPayload | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Void a manual journal entry. The voided entry stays on the record - accounting
    requires the full trail, so entries are never deleted."""
    row = await session.get(Projection, (company_id, entity_id))
    if not row or row.entity_type != "journal_entry":
        raise HTTPException(status_code=404, detail="Journal entry not found")
    state = row.state
    if state.get("je_type") != "manual":
        # Auto-posted entries mirror their source document; voiding one here would
        # desync the books from the document. Undoing the document reverses its JEs.
        raise HTTPException(
            status_code=422,
            detail="Only manual journal entries can be voided here. Undo the source document instead.",
        )
    if state.get("status") == "void":
        return {"je_id": entity_id, "status": "void", "void_reason": state.get("void_reason")}

    reason = payload.reason if payload else None
    data = je_void_data(reason, state)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.voided",
        data=data,
        actor_id=user.id,
        location_id=None,
        source="manual",
        idempotency_key=f"{entity_id}:void",
        metadata_={},
    )
    await session.commit()
    return {"je_id": entity_id, "status": "void", "void_reason": reason}


@router.get("/ledger/{account_code}")
async def account_ledger(
    account_code: str,
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Account ledger: all posted JE lines for a single account, with running balance and source doc links."""
    # Fetch account metadata for name + type (sign convention)
    account = (
        await session.execute(
            select(Account).where(Account.company_id == company_id, Account.code == account_code)
        )
    ).scalar_one_or_none()

    posted = await _je_rows(session, company_id)
    refs = await _je_doc_refs(session, company_id, [je_id for je_id, _, _ in posted])

    # Lines are matched by the literal account code, exactly like the trial
    # balance, journal, and general ledger bucket them, so the drilldown always
    # reconciles with the report row it was opened from. Legacy entries posted
    # directly to a parent code appear on the parent's own ledger.
    match_codes = {account_code}

    # Filter to lines that touch this account, apply date filter
    lines = []
    for je_id, state, ts in posted:
        # Dateless JEs (ts="") count as pre-period, exactly as the trial
        # balance, journal, and general ledger treat them: excluded once a
        # start date is set, included otherwise.
        if date_from and (not ts or ts < date_from):
            continue
        if ts and date_to and ts > date_to:
            continue
        for entry in state.get("entries", []):
            if entry.get("account") not in match_codes:
                continue
            amounts = _line_amounts(entry)
            if amounts is None:
                continue
            ref = refs.get(je_id) or {}
            lines.append({
                "date": ts,
                "je_id": je_id,
                "memo": state.get("memo", ""),
                "doc_id": ref.get("doc_id"),
                "doc_ref": ref.get("doc_ref"),
                "debit": float(amounts[0]),
                "credit": float(amounts[1]),
            })

    # Sort chronologically for running balance
    lines.sort(key=lambda x: (x["date"], x["je_id"]))

    # Compute running balance (debit-normal for asset/expense, credit-normal for others)
    account_type = account.account_type if account else "asset"
    debit_normal = account_type in ("asset", "expense", "cogs")
    running = Decimal(0)
    for line in lines:
        d, c = Decimal(str(line["debit"])), Decimal(str(line["credit"]))
        running += (d - c) if debit_normal else (c - d)
        line["balance"] = float(running)

    return {
        "account_code": account_code,
        "account_name": account.name if account else account_code,
        "account_type": account_type,
        "date_from": date_from,
        "date_to": date_to,
        "lines": lines,
    }


@router.get("/trial-balance")
async def trial_balance(
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Trial balance: one row per account with debit/credit totals.

    Reads posted journal_entry projections. Each journal entry stores
    entries: [{account, debit?, credit?}] in its state.
    """
    posted = await _je_rows(session, company_id)
    accounts = (
        await session.execute(
            select(Account).where(Account.company_id == company_id).order_by(Account.code)
        )
    ).scalars().all()
    account_map = {a.code: a for a in accounts}

    # Accumulate raw debit/credit per account code
    raw: dict[str, tuple[Decimal, Decimal]] = {}  # code -> (total_debit, total_credit)
    for _, state, ts in posted:
        if date_from and ts < date_from:
            continue
        if date_to and ts > date_to:
            continue
        for entry in state.get("entries", []):
            code = entry.get("account")
            amounts = _line_amounts(entry)
            if not code or amounts is None:
                continue
            d, c = raw.get(code, (Decimal(0), Decimal(0)))
            raw[code] = (d + amounts[0], c + amounts[1])

    lines = []
    total_debit = Decimal(0)
    total_credit = Decimal(0)
    for code in sorted(raw):
        acc = account_map.get(code)
        d, c = raw[code]
        total_debit += d
        total_credit += c
        lines.append({
            "code": code,
            "name": acc.name if acc else code,
            "account_type": acc.account_type if acc else "unknown",
            "total_debit": float(d),
            "total_credit": float(c),
            "net": float(d - c),
        })

    return {
        "date_from": date_from,
        "date_to": date_to,
        "lines": lines,
        "total_debit": float(total_debit),
        "total_credit": float(total_credit),
        "balanced": abs(total_debit - total_credit) < Decimal("0.01"),
    }


@router.get("/general-ledger")
async def general_ledger(
    date_from: str | None = None,
    date_to: str | None = None,
    include_lines: bool = False,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """General ledger summary: opening balance, period debits/credits, and closing
    balance per account. Balances are signed by the account's normal side
    (debit-normal for asset/expense/cogs, credit-normal otherwise), matching the
    per-account ledger's running balance. Detail rows live at /ledger/{code}.
    """
    posted = await _je_rows(session, company_id)
    accounts = (
        await session.execute(
            select(Account).where(Account.company_id == company_id)
        )
    ).scalars().all()
    account_map = {a.code: a for a in accounts}

    opening_raw: dict[str, Decimal] = {}
    period_debit: dict[str, Decimal] = {}
    period_credit: dict[str, Decimal] = {}
    detail: dict[str, list] = {}
    detail_jes: set[str] = set()
    for je_id, state, ts in posted:
        for entry in state.get("entries", []):
            code = entry.get("account")
            amounts = _line_amounts(entry)
            if not code or amounts is None:
                continue
            d, c = amounts
            if date_from and ts < date_from:
                opening_raw[code] = opening_raw.get(code, Decimal(0)) + d - c
                continue
            if date_to and ts > date_to:
                continue
            period_debit[code] = period_debit.get(code, Decimal(0)) + d
            period_credit[code] = period_credit.get(code, Decimal(0)) + c
            if include_lines:
                detail.setdefault(code, []).append({
                    "date": ts, "je_id": je_id, "memo": state.get("memo", ""),
                    "debit": float(d), "credit": float(c),
                })
                detail_jes.add(je_id)

    detail_refs: dict[str, dict] = {}
    if include_lines and detail_jes:
        detail_refs = await _je_doc_refs(session, company_id, sorted(detail_jes))
        for rows_for_code in detail.values():
            rows_for_code.sort(key=lambda l: (l["date"], l["je_id"]))
            for line in rows_for_code:
                ref = detail_refs.get(line["je_id"]) or {}
                line["source_ref"] = ref.get("doc_ref")

    base = await _base_currency(session, company_id)
    rows_out = []
    tot_opening = tot_debit = tot_credit = tot_closing = Decimal(0)
    raw_closing_total = Decimal(0)
    # Sorted by account code: stable across replays, unlike dict encounter order.
    for code in sorted(set(opening_raw) | set(period_debit) | set(period_credit)):
        opening = opening_raw.get(code, Decimal(0))
        d = period_debit.get(code, Decimal(0))
        c = period_credit.get(code, Decimal(0))
        if opening == 0 and d == 0 and c == 0:
            continue
        acc = account_map.get(code)
        account_type = acc.account_type if acc else "unknown"
        debit_normal = account_type not in ("liability", "equity", "revenue")
        raw_closing = opening + d - c
        raw_closing_total += raw_closing
        signed_opening = opening if debit_normal else -opening
        signed_closing = raw_closing if debit_normal else -raw_closing
        tot_opening += signed_opening
        tot_debit += d
        tot_credit += c
        tot_closing += signed_closing
        row_out = {
            "code": code,
            "name": acc.name if acc else code,
            "account_type": account_type,
            # The sign convention is decided here, once; consumers (UI, CSV)
            # must use this flag rather than re-deriving it from account_type.
            "debit_normal": debit_normal,
            "opening": to_stored_float(round_money(signed_opening, base)),
            "debit": to_stored_float(round_money(d, base)),
            "credit": to_stored_float(round_money(c, base)),
            "closing": to_stored_float(round_money(signed_closing, base)),
        }
        if include_lines:
            row_out["lines"] = detail.get(code, [])
        rows_out.append(row_out)

    return {
        "date_from": date_from,
        "date_to": date_to,
        "rows": rows_out,
        "totals": {
            "opening": to_stored_float(round_money(tot_opening, base)),
            "debit": to_stored_float(round_money(tot_debit, base)),
            "credit": to_stored_float(round_money(tot_credit, base)),
            "closing": to_stored_float(round_money(tot_closing, base)),
        },
        "balanced": abs(raw_closing_total) < Decimal("0.01"),
    }


@router.get("/pnl")
async def profit_and_loss(
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Profit and Loss statement for the period.

    Revenue accounts (4xxx) = credit-normal -> positive net credit = revenue.
    COGS accounts (5xxx) = debit-normal -> positive net debit = cost.
    Expense accounts (6xxx) = debit-normal -> positive net debit = expense.
    """
    posted = await _je_rows(session, company_id)
    accounts = (
        await session.execute(
            select(Account).where(Account.company_id == company_id).order_by(Account.code)
        )
    ).scalars().all()
    account_map = {a.code: a for a in accounts}

    balances = _build_balances(posted, date_from, date_to)

    def _section(types: list[str]) -> list[dict]:
        lines = []
        for code in sorted(balances):
            acc = account_map.get(code)
            if not acc or acc.account_type not in types:
                continue
            net = balances[code]
            # Revenue is credit-normal: net = debit - credit, so revenue = -net
            amount = float(-net) if acc.account_type == "revenue" else float(net)
            lines.append({"code": code, "name": acc.name, "account_type": acc.account_type, "amount": amount})
        return lines

    revenue_lines = _section(["revenue"])
    cogs_lines = _section(["cogs"])
    expense_lines = _section(["expense"])

    total_revenue = sum(l["amount"] for l in revenue_lines)
    total_cogs = sum(l["amount"] for l in cogs_lines)
    total_expenses = sum(l["amount"] for l in expense_lines)
    gross_profit = total_revenue - total_cogs
    net_profit = gross_profit - total_expenses

    return {
        "date_from": date_from,
        "date_to": date_to,
        "revenue": {"lines": revenue_lines, "total": total_revenue},
        "cogs": {"lines": cogs_lines, "total": total_cogs},
        "gross_profit": gross_profit,
        "expenses": {"lines": expense_lines, "total": total_expenses},
        "net_profit": net_profit,
    }


@router.get("/balance-sheet")
async def balance_sheet(
    as_of: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Balance sheet as of a given date (default: all posted entries to date)."""
    from celerp.services.auto_je import upsert_opening_inventory_je
    await upsert_opening_inventory_je(session, company_id=company_id, user_id=user.id)
    await session.commit()

    posted = await _je_rows(session, company_id)
    accounts = (
        await session.execute(
            select(Account).where(Account.company_id == company_id).order_by(Account.code)
        )
    ).scalars().all()
    account_map = {a.code: a for a in accounts}

    # Balance sheet uses all entries up to as_of
    balances = _build_balances(posted, date_from=None, date_to=as_of)

    def _section(types: list[str], credit_normal: bool) -> tuple[list[dict], float]:
        lines = []
        # Collect all leaf balances first
        leaf_lines: list[dict] = []
        seen_codes: set[str] = set()
        for code in sorted(balances):
            acc = account_map.get(code)
            if not acc or acc.account_type not in types:
                continue
            net = balances[code]
            amount = float(-net) if credit_normal else float(net)
            leaf_lines.append({"code": code, "name": acc.name, "account_type": acc.account_type, "amount": amount, "parent_code": acc.parent_code})
            seen_codes.add(code)

        # Also include child accounts that exist in account_map but have zero balance,
        # so parent accounts with sub-accounts always expand correctly.
        parent_codes_in_balance = {l["code"] for l in leaf_lines}
        for acc in sorted(accounts, key=lambda a: a.code):
            if acc.code in seen_codes:
                continue
            if acc.account_type not in types:
                continue
            if acc.parent_code in parent_codes_in_balance:
                leaf_lines.append({"code": acc.code, "name": acc.name, "account_type": acc.account_type, "amount": 0.0, "parent_code": acc.parent_code})
                seen_codes.add(acc.code)
        leaf_lines.sort(key=lambda l: l["code"])

        # For accounts that have children in the result set, replace with parent + indented children.
        child_codes = {l["code"] for l in leaf_lines if l.get("parent_code") and any(l2["code"] == l["parent_code"] for l2 in leaf_lines)}
        for leaf in leaf_lines:
            code = leaf["code"]
            children = [l for l in leaf_lines if l.get("parent_code") == code]
            if children:
                # Parent total = its own directly-posted balance (legacy
                # pre-sub-account entries) plus its children. The parent's own
                # amount stays attributed to the parent, matching how the
                # trial balance, general ledger, and ledger drilldown bucket
                # by literal account code.
                parent_total = leaf["amount"] + sum(c["amount"] for c in children)
                lines.append({"code": code, "name": leaf["name"], "account_type": leaf["account_type"], "amount": parent_total, "is_parent": True})
                for child in children:
                    lines.append({**child, "is_child": True})
            elif code not in child_codes:
                lines.append(leaf)

        total = sum(l["amount"] for l in lines if not l.get("is_child"))
        return lines, total

    asset_lines, total_assets = _section(["asset"], credit_normal=False)
    liability_lines, total_liabilities = _section(["liability"], credit_normal=True)
    equity_lines, total_equity = _section(["equity"], credit_normal=True)

    # Retained earnings = net income (all revenue - COGS - expenses) accumulated to date.
    # This equals Assets - Liabilities - explicit Equity by the accounting equation.
    retained_earnings = total_assets - total_liabilities - total_equity
    if abs(retained_earnings) >= 0.01:
        equity_lines.append({
            "code": "RE",
            "name": "Retained Earnings",
            "account_type": "equity",
            "amount": retained_earnings,
            "synthetic": True,
            "href_pnl": True,
        })
        total_equity += retained_earnings

    total_l_e = total_liabilities + total_equity

    return {
        "as_of": as_of,
        "assets": {"lines": asset_lines, "total": total_assets},
        "liabilities": {"lines": liability_lines, "total": total_liabilities},
        "equity": {"lines": equity_lines, "total": total_equity},
        "total_liabilities_equity": total_l_e,
        "balanced": abs(total_assets - total_l_e) < 0.01,
    }


# Doc types that appear on a statement of account (the same financial docs the
# AR/AP aging counts, plus credit notes which reduce what the contact owes).
_SOA_DOC_TYPES = frozenset({"invoice", "credit_note", "purchase_order", "bill"})
# Legacy doc_type spellings normalised to their canonical names. The AR/AP aging
# accepts these same spellings (and the "type" state fallback), and a statement
# must count exactly the doc set aging counts, so the tolerance matches.
_SOA_TYPE_ALIASES = {"Invoice": "invoice", "PO": "purchase_order"}


@router.get("/soa/{contact_id}")
async def statement_of_account(
    contact_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Statement of account for one contact: opening balance, dated doc/payment
    rows with a running balance, closing balance.

    Amounts are in base currency so mixed-currency contacts read on one statement
    (doc totals convert via their stored conversion_rate, payments via their own
    per-payment rate). Finalized-and-later docs count, drafts and voids do not;
    only active payments count, on their payment date. Credit notes reduce the
    balance and their payments (applications, refunds) add back, so an applied
    credit note is never double-counted against the invoice it settles.
    """
    contact_row = await session.get(Projection, (company_id, contact_id))
    if not contact_row or contact_row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Contact not found")
    # Merge tombstones carry both deleted and merged_into, so the merge check
    # must come first or merged contacts would 404 instead of redirecting.
    if contact_row.state.get("merged_into"):
        # Follow the merge chain so bookmarked statements land on the surviving contact.
        seen = {contact_id}
        winner = contact_row.state["merged_into"]
        while winner not in seen:
            seen.add(winner)
            row = await session.get(Projection, (company_id, winner))
            nxt = row.state.get("merged_into") if row else None
            if not nxt:
                break
            winner = nxt
        return {"merged_into": winner}
    if contact_row.state.get("deleted"):
        raise HTTPException(status_code=404, detail="Contact not found")

    doc_rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
            )
        )
    ).scalars().all()

    base = await _base_currency(session, company_id)
    # (date, doc_id, seq, doc_ref, kind, debit, credit); seq keeps a doc's own row
    # ahead of its same-day payments and makes the ordering replay-stable.
    events: list[tuple[str, str, int, str, str, Decimal, Decimal]] = []
    for dr in doc_rows:
        state = dr.state
        linked = state.get("contact_id") or state.get("customer_id") or state.get("supplier_id")
        if linked != contact_id:
            continue
        doc_type = state.get("doc_type", state.get("type", ""))
        doc_type = _SOA_TYPE_ALIASES.get(doc_type, doc_type)
        if doc_type not in _SOA_DOC_TYPES:
            continue
        if state.get("status") in ("draft", "void"):
            continue
        doc_ref = state.get("ref_id") or state.get("doc_number") or dr.entity_id
        doc_rate = to_decimal(state.get("conversion_rate") or 1)
        doc_date = str(state.get("issue_date") or state.get("date") or "")[:10]
        total = round_money(to_decimal(state.get("total") or 0) * doc_rate, base)
        # Statement sign convention: positive balance = the contact owes us.
        # Receivable-side docs (invoices) charge as debits; payable-side docs
        # (bills, purchase orders) and credit notes reduce the net as credits,
        # so a dual-role contact's receivables and payables net correctly
        # instead of stacking in one direction. Payments mirror their doc side.
        reduces_balance = doc_type in ("credit_note", "purchase_order", "bill")
        if reduces_balance:
            events.append((doc_date, dr.entity_id, 0, doc_ref, doc_type, Decimal(0), total))
        else:
            events.append((doc_date, dr.entity_id, 0, doc_ref, doc_type, total, Decimal(0)))
        for i, p in enumerate(state.get("payments", [])):
            if p.get("status") != "active":
                continue
            p_rate = to_decimal(p.get("conversion_rate") or state.get("conversion_rate") or 1)
            amount = round_money(to_decimal(p.get("amount") or 0) * p_rate, base)
            p_date = str(p.get("payment_date") or doc_date or "")[:10]
            if reduces_balance:
                events.append((p_date, dr.entity_id, i + 1, doc_ref, "payment", amount, Decimal(0)))
            else:
                events.append((p_date, dr.entity_id, i + 1, doc_ref, "payment", Decimal(0), amount))

    # Ascending: a statement's running balance reads down the page.
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    opening = Decimal(0)
    in_range = []
    for e in events:
        if date_from and e[0] < date_from:
            opening += e[5] - e[6]
            continue
        if date_to and e[0] > date_to:
            continue
        in_range.append(e)

    running = opening
    rows_out = []
    for d, doc_id, _seq, doc_ref, kind, debit, credit in in_range:
        running += debit - credit
        rows_out.append({
            "date": d,
            "doc_id": doc_id,
            "doc_ref": doc_ref,
            "kind": kind,
            "debit": to_stored_float(debit),
            "credit": to_stored_float(credit),
            "balance": to_stored_float(round_money(running, base)),
        })

    cstate = contact_row.state
    return {
        "contact": {
            "id": contact_id,
            "name": cstate.get("name") or contact_id,
            "type": cstate.get("contact_type") or "customer",
        },
        "date_from": date_from,
        "date_to": date_to,
        "opening_balance": to_stored_float(round_money(opening, base)),
        "rows": rows_out,
        "closing_balance": to_stored_float(round_money(running, base)),
    }


# ---------------------------------------------------------------------------
# Bank Accounts CRUD
# ---------------------------------------------------------------------------

_BANK_TYPES = frozenset({"checking", "savings", "credit_card"})


class BankAccountCreate(BaseModel):
    bank_name: str
    account_number: str
    bank_type: str  # checking|savings|credit_card
    currency: str
    opening_balance: float = 0.0
    account_code: str | None = None  # optional override; auto-assigned if None


class BankAccountPatch(BaseModel):
    bank_name: str | None = None
    account_number: str | None = None
    bank_type: str | None = None
    currency: str | None = None
    is_active: bool | None = None


def _bank_to_dict(b: BankAccount) -> dict:
    return {
        "id": str(b.id),
        "chart_account_code": b.chart_account_code,
        "bank_name": b.bank_name,
        "account_number": b.account_number,
        "bank_type": b.bank_type,
        "currency": b.currency,
        "opening_balance": float(b.opening_balance),
        "is_active": b.is_active,
        "created_at": b.created_at.isoformat(),
    }


async def _compute_bank_balance(
    session: AsyncSession,
    company_id: uuid.UUID,
    chart_account_code: str,
    opening_balance: float,
) -> float:
    """Compute bank balance: opening + JE debits - JE credits for this account code."""
    net = Decimal(str(opening_balance))
    for _, state, _ in await _je_rows(session, company_id):
        for entry in state.get("entries", []):
            if entry.get("account") == chart_account_code:
                amounts = _line_amounts(entry)
                if amounts is None:
                    continue
                net += amounts[0] - amounts[1]
    return float(net)


async def _next_bank_account_code(session: AsyncSession, company_id: uuid.UUID) -> str:
    """Find next available account code under 1110 (1111, 1112, …)."""
    rows = (
        await session.execute(
            select(Account.code).where(
                Account.company_id == company_id,
                Account.code.like("111%"),
            )
        )
    ).scalars().all()
    used = set(rows)
    for i in range(1, 100):
        code = f"111{i}"
        if code not in used:
            return code
    raise HTTPException(status_code=400, detail="No available account codes under 1110")


@router.get("/bank-accounts")
async def list_bank_accounts(
    include_inactive: bool = False,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    q = select(BankAccount).where(BankAccount.company_id == company_id)
    if not include_inactive:
        q = q.where(BankAccount.is_active.is_(True))
    q = q.order_by(BankAccount.created_at)
    rows = (await session.execute(q)).scalars().all()
    items = []
    for b in rows:
        d = _bank_to_dict(b)
        d["balance"] = await _compute_bank_balance(session, company_id, b.chart_account_code, float(b.opening_balance))
        items.append(d)
    return {"items": items, "total": len(items)}


@router.get("/bank-accounts/{bank_id}")
async def get_bank_account(
    bank_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    b = (
        await session.execute(
            select(BankAccount).where(BankAccount.id == bank_id, BankAccount.company_id == company_id)
        )
    ).scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Bank account not found")
    d = _bank_to_dict(b)
    d["balance"] = await _compute_bank_balance(session, company_id, b.chart_account_code, float(b.opening_balance))
    return d


@router.post("/bank-accounts")
async def create_bank_account(
    payload: BankAccountCreate,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if payload.bank_type not in _BANK_TYPES:
        raise HTTPException(status_code=422, detail=f"bank_type must be one of {sorted(_BANK_TYPES)}")
    currency = payload.currency.upper()
    if currency not in ISO_4217_CURRENCIES:
        raise HTTPException(status_code=422, detail=f"Invalid currency '{payload.currency}'. Must be a valid ISO 4217 code.")

    # Resolve or auto-assign chart account code
    code = payload.account_code or await _next_bank_account_code(session, company_id)

    # Ensure account code doesn't already exist
    existing_acc = (
        await session.execute(
            select(Account).where(Account.company_id == company_id, Account.code == code)
        )
    ).scalar_one_or_none()
    if not existing_acc:
        # Auto-create a chart-of-accounts sub-entry under 1110
        acc = Account(
            id=uuid.uuid4(),
            company_id=company_id,
            code=code,
            name=f"{payload.bank_name} ({payload.bank_type.replace('_', ' ').title()})",
            account_type="asset",
            parent_code="1110",
        )
        session.add(acc)

    bank = BankAccount(
        id=uuid.uuid4(),
        company_id=company_id,
        chart_account_code=code,
        bank_name=payload.bank_name,
        account_number=payload.account_number,
        bank_type=payload.bank_type,
        currency=currency,
        opening_balance=payload.opening_balance,
    )
    session.add(bank)

    # Create opening balance JE if opening_balance != 0
    if payload.opening_balance and payload.opening_balance != 0.0:
        je_id = f"je:opening:{bank.id}"
        idem_c = f"opening:{bank.id}:c"
        idem_p = f"opening:{bank.id}:p"
        from celerp.services.je_keys import je_idempotency_key as _je_key  # noqa
        today = datetime.now(timezone.utc).date().isoformat()
        ob = float(payload.opening_balance)
        # Debit the bank account, credit equity (retained earnings 3200)
        entries = [
            {"account": code, "debit": ob, "credit": 0.0},
            {"account": "3200", "debit": 0.0, "credit": ob},
        ] if ob > 0 else [
            {"account": "3200", "debit": abs(ob), "credit": 0.0},
            {"account": code, "debit": 0.0, "credit": abs(ob)},
        ]
        await emit_event(
            session,
            company_id=company_id,
            entity_id=je_id,
            entity_type="journal_entry",
            event_type="acc.journal_entry.created",
            data={"memo": f"Opening balance for {payload.bank_name}", "ts": today, "entries": entries},
            actor_id=user.id,
            location_id=None,
            source="bank_account_opening",
            idempotency_key=idem_c,
            metadata_={"bank_account_id": str(bank.id)},
        )
        await emit_event(
            session,
            company_id=company_id,
            entity_id=je_id,
            entity_type="journal_entry",
            event_type="acc.journal_entry.posted",
            data={"ts": today},
            actor_id=user.id,
            location_id=None,
            source="bank_account_opening",
            idempotency_key=idem_p,
            metadata_={},
        )

    await session.commit()
    d = _bank_to_dict(bank)
    d["balance"] = await _compute_bank_balance(session, company_id, code, float(payload.opening_balance))
    return d


@router.patch("/bank-accounts/{bank_id}")
async def patch_bank_account(
    bank_id: uuid.UUID,
    payload: BankAccountPatch,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    b = (
        await session.execute(
            select(BankAccount).where(BankAccount.id == bank_id, BankAccount.company_id == company_id)
        )
    ).scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Bank account not found")
    if payload.bank_type is not None and payload.bank_type not in _BANK_TYPES:
        raise HTTPException(status_code=422, detail=f"bank_type must be one of {sorted(_BANK_TYPES)}")

    if payload.bank_name is not None:
        b.bank_name = payload.bank_name
    if payload.account_number is not None:
        b.account_number = payload.account_number
    if payload.bank_type is not None:
        b.bank_type = payload.bank_type
    if payload.currency is not None:
        normed = payload.currency.upper()
        if normed not in ISO_4217_CURRENCIES:
            raise HTTPException(status_code=422, detail=f"Invalid currency '{payload.currency}'. Must be a valid ISO 4217 code.")
        b.currency = normed
    if payload.is_active is not None:
        b.is_active = payload.is_active

    await session.commit()
    d = _bank_to_dict(b)
    d["balance"] = await _compute_bank_balance(session, company_id, b.chart_account_code, float(b.opening_balance))
    return d


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

class TransferCreate(BaseModel):
    from_bank_id: str
    to_bank_id: str
    amount: float
    date: str  # ISO date "YYYY-MM-DD"
    description: str = ""
    reference: str = ""


@router.post("/transfers")
async def create_transfer(
    payload: TransferCreate,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if payload.amount <= 0:
        raise HTTPException(status_code=422, detail="Transfer amount must be positive")

    from_bank = (
        await session.execute(
            select(BankAccount).where(
                BankAccount.id == uuid.UUID(payload.from_bank_id),
                BankAccount.company_id == company_id,
                BankAccount.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not from_bank:
        raise HTTPException(status_code=404, detail="Source bank account not found")

    to_bank = (
        await session.execute(
            select(BankAccount).where(
                BankAccount.id == uuid.UUID(payload.to_bank_id),
                BankAccount.company_id == company_id,
                BankAccount.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not to_bank:
        raise HTTPException(status_code=404, detail="Destination bank account not found")

    je_id = f"je:transfer:{uuid.uuid4()}"
    idem_c = f"transfer:{je_id}:c"
    idem_p = f"transfer:{je_id}:p"
    memo = payload.description or f"Transfer {payload.from_bank_id[:8]} → {payload.to_bank_id[:8]}"
    entries = [
        {"account": to_bank.chart_account_code, "debit": payload.amount, "credit": 0.0},
        {"account": from_bank.chart_account_code, "debit": 0.0, "credit": payload.amount},
    ]

    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={
            "memo": memo,
            "ts": payload.date,
            "entries": entries,
            "je_type": "transfer",
            "reference": payload.reference,
            "from_bank_account_id": payload.from_bank_id,
            "to_bank_account_id": payload.to_bank_id,
        },
        actor_id=user.id,
        location_id=None,
        source="transfer",
        idempotency_key=idem_c,
        metadata_={},
    )
    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.posted",
        data={"ts": payload.date},
        actor_id=user.id,
        location_id=None,
        source="transfer",
        idempotency_key=idem_p,
        metadata_={},
    )
    await session.commit()

    return {
        "je_id": je_id,
        "from_bank_id": payload.from_bank_id,
        "to_bank_id": payload.to_bank_id,
        "amount": payload.amount,
        "date": payload.date,
        "memo": memo,
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class ReconciliationStart(BaseModel):
    bank_account_id: str
    statement_date: str  # "YYYY-MM-DD"
    statement_balance: float


class ReconciliationMatch(BaseModel):
    je_ids: list[str]


def _recon_to_dict(r: ReconciliationSession) -> dict:
    return {
        "id": str(r.id),
        "bank_account_id": str(r.bank_account_id),
        "statement_date": r.statement_date,
        "statement_balance": float(r.statement_balance),
        "status": r.status,
        "reconciled_je_ids": list(r.reconciled_je_ids or []),
        "csv_filename": r.csv_filename,
        "csv_row_count": r.csv_row_count or 0,
        "auto_matched_count": r.auto_matched_count or 0,
        "manual_matched_count": r.manual_matched_count or 0,
        "created_count": r.created_count or 0,
        "tolerance": float(r.tolerance) if r.tolerance is not None else 1.0,
        "imported_at": r.imported_at.isoformat() if r.imported_at else None,
        "created_at": r.created_at.isoformat(),
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }


def _stmt_line_to_dict(l: BankStatementLine) -> dict:
    return {
        "id": str(l.id),
        "reconciliation_id": str(l.reconciliation_id),
        "company_id": str(l.company_id),
        "line_date": l.line_date,
        "description": l.description,
        "amount": float(l.amount),
        "raw_balance": float(l.raw_balance) if l.raw_balance is not None else None,
        "reference": l.reference,
        "status": l.status,
        "matched_je_id": l.matched_je_id,
        "attachment_ids": list(l.attachment_ids or []),
        "raw_csv_row": dict(l.raw_csv_row or {}),
        "created_at": l.created_at.isoformat(),
    }


def _rule_to_dict(r: ReconciliationRule) -> dict:
    return {
        "id": str(r.id),
        "company_id": str(r.company_id),
        "bank_account_id": str(r.bank_account_id),
        "match_field": r.match_field,
        "match_pattern": r.match_pattern,
        "match_type": r.match_type,
        "target_account_code": r.target_account_code,
        "default_memo": r.default_memo,
        "default_tax": r.default_tax,
        "is_active": r.is_active,
        "times_applied": r.times_applied or 0,
        "created_at": r.created_at.isoformat(),
    }


async def _je_entries_for_account(
    session: AsyncSession, company_id: uuid.UUID, account_code: str
) -> list[dict]:
    """Posted JE lines for one account as {je_id, ts, memo, amount, debit, credit}."""
    result = []
    for je_id, state, _ in await _je_rows(session, company_id):
        for entry in state.get("entries", []):
            if entry.get("account") == account_code:
                amounts = _line_amounts(entry)
                if amounts is None:
                    continue
                result.append({
                    "je_id": je_id,
                    "ts": state.get("ts") or state.get("created_at") or "",
                    "memo": state.get("memo", ""),
                    "debit": float(amounts[0]),
                    "credit": float(amounts[1]),
                    "amount": float(amounts[0] - amounts[1]),
                })
    result.sort(key=lambda x: x["ts"])
    return result


@router.post("/reconciliation/start")
async def start_reconciliation(
    payload: ReconciliationStart,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    bank = (
        await session.execute(
            select(BankAccount).where(
                BankAccount.id == uuid.UUID(payload.bank_account_id),
                BankAccount.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if not bank:
        raise HTTPException(status_code=404, detail="Bank account not found")

    recon = ReconciliationSession(
        id=uuid.uuid4(),
        company_id=company_id,
        bank_account_id=bank.id,
        statement_date=payload.statement_date,
        statement_balance=payload.statement_balance,
        status="open",
        reconciled_je_ids=[],
    )
    session.add(recon)
    await session.commit()
    return _recon_to_dict(recon)


@router.get("/reconciliation/{session_id}")
async def get_reconciliation(
    session_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_session),
) -> dict:
    recon = (
        await db.execute(
            select(ReconciliationSession).where(
                ReconciliationSession.id == session_id,
                ReconciliationSession.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail="Reconciliation session not found")

    bank = (await db.execute(select(BankAccount).where(BankAccount.id == recon.bank_account_id))).scalar_one_or_none()
    if not bank:
        raise HTTPException(status_code=404, detail="Bank account not found")

    all_entries = await _je_entries_for_account(db, company_id, bank.chart_account_code)
    reconciled_ids = set(recon.reconciled_je_ids or [])
    unreconciled = [e for e in all_entries if e["je_id"] not in reconciled_ids]
    reconciled = [e for e in all_entries if e["je_id"] in reconciled_ids]

    book_balance = float(bank.opening_balance) + sum(e["amount"] for e in all_entries)
    matched_sum = sum(e["amount"] for e in reconciled)
    difference = float(recon.statement_balance) - (float(bank.opening_balance) + matched_sum)

    d = _recon_to_dict(recon)
    d.update({
        "bank_account": _bank_to_dict(bank),
        "all_entries": all_entries,
        "unreconciled_entries": unreconciled,
        "reconciled_entries": reconciled,
        "book_balance": book_balance,
        "matched_balance": float(bank.opening_balance) + matched_sum,
        "difference": difference,
    })
    return d


@router.post("/reconciliation/{session_id}/match")
async def match_reconciliation(
    session_id: uuid.UUID,
    payload: ReconciliationMatch,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    recon = (
        await db.execute(
            select(ReconciliationSession).where(
                ReconciliationSession.id == session_id,
                ReconciliationSession.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail="Reconciliation session not found")
    if recon.status == "completed":
        raise HTTPException(status_code=409, detail="Session already completed")

    existing = set(recon.reconciled_je_ids or [])
    existing.update(payload.je_ids)
    recon.reconciled_je_ids = list(existing)
    await db.commit()
    return _recon_to_dict(recon)


@router.post("/reconciliation/{session_id}/complete")
async def complete_reconciliation(
    session_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    recon = (
        await db.execute(
            select(ReconciliationSession).where(
                ReconciliationSession.id == session_id,
                ReconciliationSession.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail="Reconciliation session not found")
    if recon.status == "completed":
        raise HTTPException(status_code=409, detail="Session already completed")

    bank = (await db.execute(select(BankAccount).where(BankAccount.id == recon.bank_account_id))).scalar_one_or_none()
    if not bank:
        raise HTTPException(status_code=404, detail="Bank account not found")

    all_entries = await _je_entries_for_account(db, company_id, bank.chart_account_code)
    reconciled_ids = set(recon.reconciled_je_ids or [])
    reconciled = [e for e in all_entries if e["je_id"] in reconciled_ids]
    matched_sum = sum(e["amount"] for e in reconciled)
    difference = float(recon.statement_balance) - (float(bank.opening_balance) + matched_sum)

    if abs(difference) >= 0.01:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot complete: difference of {difference:.2f} remains. Mark all matching transactions first.",
        )

    recon.status = "completed"
    recon.completed_at = datetime.now(timezone.utc)
    await db.commit()
    return _recon_to_dict(recon)


# ── Reconciliation V2 — CSV import, statement lines, auto-match, rules ────────

class StmtLineMatchPayload(BaseModel):
    je_id: str
    confidence: str = "manual"


class StmtLineCreatePayload(BaseModel):
    account_code: str
    memo: str = ""
    amount: float | None = None  # defaults to line amount
    date: str | None = None      # defaults to line_date


class StmtLineSplitPayload(BaseModel):
    splits: list[dict]  # [{account_code, amount, memo}]


class StmtLinePatch(BaseModel):
    status: str | None = None


class BulkConfirmPayload(BaseModel):
    confidence: str | None = None  # if set, only confirm at this confidence level or above


class WriteOffPayload(BaseModel):
    account_code: str = "6950"  # default to misc expenses
    memo: str = "Bank reconciliation adjustment"


class ReconRuleCreate(BaseModel):
    bank_account_id: str
    match_field: str = "description"
    match_pattern: str
    match_type: str = "contains"
    target_account_code: str
    default_memo: str | None = None
    default_tax: str | None = None


class ReconRulePatch(BaseModel):
    match_field: str | None = None
    match_pattern: str | None = None
    match_type: str | None = None
    target_account_code: str | None = None
    default_memo: str | None = None
    default_tax: str | None = None
    is_active: bool | None = None


@router.post("/reconciliation/{session_id}/import-csv")
async def import_recon_csv(
    session_id: uuid.UUID,
    file: UploadFile = File(...),
    column_map: str | None = FastForm(None),  # JSON-encoded dict
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Upload and parse a bank statement CSV, store lines."""
    import json as _json
    from celerp_accounting.csv_parser import parse_bank_csv

    recon = (await db.execute(
        select(ReconciliationSession).where(
            ReconciliationSession.id == session_id,
            ReconciliationSession.company_id == company_id,
        )
    )).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail="Reconciliation session not found")
    if recon.status == "completed":
        raise HTTPException(status_code=409, detail="Session already completed")

    content = await file.read()
    col_map = _json.loads(column_map) if column_map else None
    parsed = parse_bank_csv(content, col_map)

    if parsed["needs_mapping"]:
        return {
            "needs_mapping": True,
            "headers": parsed["headers"],
            "preview": parsed["preview"],
            "session_id": str(session_id),
        }

    # Delete existing lines for this session (re-import)
    existing = (await db.execute(
        select(BankStatementLine).where(BankStatementLine.reconciliation_id == session_id)
    )).scalars().all()
    for line in existing:
        await db.delete(line)

    new_lines = []
    for raw_line in parsed["lines"]:
        sl = BankStatementLine(
            id=uuid.uuid4(),
            company_id=company_id,
            reconciliation_id=session_id,
            line_date=raw_line.get("line_date", ""),
            description=raw_line.get("description", ""),
            amount=raw_line.get("amount", "0"),
            raw_balance=raw_line.get("raw_balance"),
            reference=raw_line.get("reference"),
            status="unmatched",
            attachment_ids=[],
            raw_csv_row=raw_line.get("raw_csv_row", {}),
        )
        db.add(sl)
        new_lines.append(sl)

    recon.csv_filename = file.filename
    recon.csv_row_count = len(new_lines)
    recon.imported_at = datetime.now(timezone.utc)
    recon.auto_matched_count = 0
    recon.manual_matched_count = 0
    recon.created_count = 0

    await db.commit()
    return {
        "needs_mapping": False,
        "session_id": str(session_id),
        "rows_imported": len(new_lines),
        "csv_filename": file.filename,
    }


@router.get("/reconciliation/{session_id}/statement-lines")
async def get_statement_lines(
    session_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_session),
) -> dict:
    recon = (await db.execute(
        select(ReconciliationSession).where(
            ReconciliationSession.id == session_id,
            ReconciliationSession.company_id == company_id,
        )
    )).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail="Reconciliation session not found")

    lines = (await db.execute(
        select(BankStatementLine).where(
            BankStatementLine.reconciliation_id == session_id
        ).order_by(BankStatementLine.line_date, BankStatementLine.created_at)
    )).scalars().all()

    return {"items": [_stmt_line_to_dict(l) for l in lines], "total": len(lines)}


@router.post("/reconciliation/{session_id}/auto-match")
async def auto_match_recon(
    session_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Run the auto-matching algorithm against all unmatched statement lines."""
    from celerp_accounting.matcher import auto_match

    recon = (await db.execute(
        select(ReconciliationSession).where(
            ReconciliationSession.id == session_id,
            ReconciliationSession.company_id == company_id,
        )
    )).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail="Reconciliation session not found")
    if recon.status == "completed":
        raise HTTPException(status_code=409, detail="Session already completed")

    bank = (await db.execute(select(BankAccount).where(BankAccount.id == recon.bank_account_id))).scalar_one_or_none()
    if not bank:
        raise HTTPException(status_code=404, detail="Bank account not found")

    stmt_lines = (await db.execute(
        select(BankStatementLine).where(
            BankStatementLine.reconciliation_id == session_id,
            BankStatementLine.status == "unmatched",
        )
    )).scalars().all()

    book_entries = await _je_entries_for_account(db, company_id, bank.chart_account_code)
    already_matched = set(recon.reconciled_je_ids or [])
    unmatched_entries = [e for e in book_entries if e["je_id"] not in already_matched]

    stmt_dicts = [_stmt_line_to_dict(l) for l in stmt_lines]
    matches = auto_match(stmt_dicts, unmatched_entries)

    high_conf = 0
    med_conf = 0
    line_map = {str(l.id): l for l in stmt_lines}

    for line_id, je_id, confidence in matches:
        sl = line_map.get(line_id)
        if not sl:
            continue
        sl.status = "matched" if confidence == "high" else "suggested"
        sl.matched_je_id = je_id
        if confidence == "high":
            high_conf += 1
            existing = list(recon.reconciled_je_ids or [])
            if je_id not in existing:
                existing.append(je_id)
                recon.reconciled_je_ids = existing
        else:
            med_conf += 1

    recon.auto_matched_count = high_conf
    await db.commit()

    return {
        "matched": high_conf,
        "suggested": med_conf,
        "total_processed": len(stmt_lines),
    }


@router.post("/reconciliation/{session_id}/lines/{line_id}/match")
async def match_stmt_line(
    session_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: StmtLineMatchPayload,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    recon, sl = await _get_recon_and_line(db, session_id, line_id, company_id)
    sl.status = "matched"
    sl.matched_je_id = payload.je_id
    existing = list(recon.reconciled_je_ids or [])
    if payload.je_id not in existing:
        existing.append(payload.je_id)
        recon.reconciled_je_ids = existing
    recon.manual_matched_count = (recon.manual_matched_count or 0) + 1
    await db.commit()
    return _stmt_line_to_dict(sl)


@router.post("/reconciliation/{session_id}/lines/{line_id}/unmatch")
async def unmatch_stmt_line(
    session_id: uuid.UUID,
    line_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    recon, sl = await _get_recon_and_line(db, session_id, line_id, company_id)
    old_je_id = sl.matched_je_id
    sl.status = "unmatched"
    sl.matched_je_id = None
    if old_je_id:
        existing = [j for j in (recon.reconciled_je_ids or []) if j != old_je_id]
        recon.reconciled_je_ids = existing
    await db.commit()
    return _stmt_line_to_dict(sl)


@router.post("/reconciliation/{session_id}/lines/{line_id}/create")
async def create_je_from_line(
    session_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: StmtLineCreatePayload,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _m: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Create a journal entry from a bank statement line and auto-match it."""
    from celerp.services.je_keys import je_idempotency_key

    recon, sl = await _get_recon_and_line(db, session_id, line_id, company_id)
    bank = (await db.execute(select(BankAccount).where(BankAccount.id == recon.bank_account_id))).scalar_one_or_none()
    if not bank:
        raise HTTPException(status_code=404, detail="Bank account not found")

    amount = payload.amount if payload.amount is not None else abs(float(sl.amount))
    entry_date = payload.date or sl.line_date
    memo = payload.memo or sl.description
    je_id = f"je:recon:{sl.id}"

    # Determine debit/credit based on amount sign
    bank_debit = max(float(sl.amount), 0)
    bank_credit = max(-float(sl.amount), 0)
    other_debit = bank_credit  # offset entry
    other_credit = bank_debit

    idem_c = je_idempotency_key(entry_date, f"recon_create_{sl.id}", "c")
    idem_p = je_idempotency_key(entry_date, f"recon_create_{sl.id}", "p")

    entries = [
        {"account": bank.chart_account_code, "debit": bank_debit, "credit": bank_credit},
        {"account": payload.account_code, "debit": other_debit, "credit": other_credit},
    ]

    await emit_event(
        db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={"memo": memo, "ts": entry_date, "entries": entries, "je_type": "recon_create"},
        actor_id=user.id, location_id=None, source="reconciliation",
        idempotency_key=idem_c, metadata_={"recon_session_id": str(session_id)},
    )
    await emit_event(
        db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
        event_type="acc.journal_entry.posted",
        data={"ts": entry_date}, actor_id=user.id, location_id=None, source="reconciliation",
        idempotency_key=idem_p, metadata_={},
    )

    sl.status = "created"
    sl.matched_je_id = je_id
    existing = list(recon.reconciled_je_ids or [])
    if je_id not in existing:
        existing.append(je_id)
        recon.reconciled_je_ids = existing
    recon.created_count = (recon.created_count or 0) + 1
    await db.commit()
    return _stmt_line_to_dict(sl)


@router.post("/reconciliation/{session_id}/lines/{line_id}/split")
async def split_stmt_line(
    session_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: StmtLineSplitPayload,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _m: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Split a bank line into multiple JE lines across different accounts."""
    from celerp.services.je_keys import je_idempotency_key

    recon, sl = await _get_recon_and_line(db, session_id, line_id, company_id)
    bank = (await db.execute(select(BankAccount).where(BankAccount.id == recon.bank_account_id))).scalar_one_or_none()
    if not bank:
        raise HTTPException(status_code=404, detail="Bank account not found")

    if not payload.splits:
        raise HTTPException(status_code=422, detail="At least one split entry required")

    je_id = f"je:recon:split:{sl.id}"
    idem_c = je_idempotency_key(sl.line_date, f"recon_split_{sl.id}", "c")
    idem_p = je_idempotency_key(sl.line_date, f"recon_split_{sl.id}", "p")

    bank_debit = max(float(sl.amount), 0)
    bank_credit = max(-float(sl.amount), 0)
    entries = [{"account": bank.chart_account_code, "debit": bank_debit, "credit": bank_credit}]
    for s in payload.splits:
        amt = float(s.get("amount", 0))
        entries.append({
            "account": s["account_code"],
            "debit": amt if sl.amount < 0 else 0.0,
            "credit": amt if sl.amount >= 0 else 0.0,
        })

    await emit_event(
        db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={"memo": sl.description, "ts": sl.line_date, "entries": entries, "je_type": "recon_split"},
        actor_id=user.id, location_id=None, source="reconciliation",
        idempotency_key=idem_c, metadata_={"recon_session_id": str(session_id)},
    )
    await emit_event(
        db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
        event_type="acc.journal_entry.posted",
        data={"ts": sl.line_date}, actor_id=user.id, location_id=None, source="reconciliation",
        idempotency_key=idem_p, metadata_={},
    )

    sl.status = "created"
    sl.matched_je_id = je_id
    existing = list(recon.reconciled_je_ids or [])
    if je_id not in existing:
        existing.append(je_id)
        recon.reconciled_je_ids = existing
    recon.created_count = (recon.created_count or 0) + 1
    await db.commit()
    return _stmt_line_to_dict(sl)


@router.patch("/reconciliation/{session_id}/lines/{line_id}")
async def patch_stmt_line(
    session_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: StmtLinePatch,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    _, sl = await _get_recon_and_line(db, session_id, line_id, company_id)
    if payload.status is not None:
        if payload.status not in ("unmatched", "matched", "created", "skipped"):
            raise HTTPException(status_code=422, detail="Invalid status")
        sl.status = payload.status
    await db.commit()
    return _stmt_line_to_dict(sl)


@router.post("/reconciliation/{session_id}/lines/{line_id}/attach")
async def attach_to_line(
    session_id: uuid.UUID,
    line_id: uuid.UUID,
    file: UploadFile = File(...),
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Attach a document to a statement line (stores file, returns attachment id)."""
    import hashlib, os
    from pathlib import Path

    _, sl = await _get_recon_and_line(db, session_id, line_id, company_id)
    data = await file.read()
    att_id = hashlib.sha256(data).hexdigest()[:16]
    # Store in static attachments dir (mirrors inventory attachment pattern)
    att_dir = Path("static/attachments")
    att_dir.mkdir(parents=True, exist_ok=True)
    att_path = att_dir / att_id
    att_path.write_bytes(data)

    ids = list(sl.attachment_ids or [])
    if att_id not in ids:
        ids.append(att_id)
        sl.attachment_ids = ids
    await db.commit()
    return {"attachment_id": att_id, "filename": file.filename}


@router.delete("/reconciliation/{session_id}/lines/{line_id}/attach/{att_id}")
async def remove_line_attachment(
    session_id: uuid.UUID,
    line_id: uuid.UUID,
    att_id: str,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    _, sl = await _get_recon_and_line(db, session_id, line_id, company_id)
    ids = [i for i in (sl.attachment_ids or []) if i != att_id]
    sl.attachment_ids = ids
    await db.commit()
    return {"removed": att_id}


@router.post("/reconciliation/{session_id}/bulk-confirm")
async def bulk_confirm_recon(
    session_id: uuid.UUID,
    payload: BulkConfirmPayload | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Confirm all 'suggested' matches (make them fully matched)."""
    recon = (await db.execute(
        select(ReconciliationSession).where(
            ReconciliationSession.id == session_id,
            ReconciliationSession.company_id == company_id,
        )
    )).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail="Reconciliation session not found")
    if recon.status == "completed":
        raise HTTPException(status_code=409, detail="Session already completed")

    lines = (await db.execute(
        select(BankStatementLine).where(
            BankStatementLine.reconciliation_id == session_id,
            BankStatementLine.status == "suggested",
        )
    )).scalars().all()

    confirmed = 0
    existing = list(recon.reconciled_je_ids or [])
    for sl in lines:
        sl.status = "matched"
        if sl.matched_je_id and sl.matched_je_id not in existing:
            existing.append(sl.matched_je_id)
        confirmed += 1

    recon.reconciled_je_ids = existing
    recon.manual_matched_count = (recon.manual_matched_count or 0) + confirmed
    await db.commit()
    return {"confirmed": confirmed}


@router.post("/reconciliation/{session_id}/write-off")
async def write_off_difference(
    session_id: uuid.UUID,
    payload: WriteOffPayload,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _m: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Create a small adjustment JE to zero out the remaining difference."""
    from celerp.services.je_keys import je_idempotency_key

    recon = (await db.execute(
        select(ReconciliationSession).where(
            ReconciliationSession.id == session_id,
            ReconciliationSession.company_id == company_id,
        )
    )).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail="Reconciliation session not found")
    if recon.status == "completed":
        raise HTTPException(status_code=409, detail="Session already completed")

    bank = (await db.execute(select(BankAccount).where(BankAccount.id == recon.bank_account_id))).scalar_one_or_none()
    if not bank:
        raise HTTPException(status_code=404, detail="Bank account not found")

    all_entries = await _je_entries_for_account(db, company_id, bank.chart_account_code)
    reconciled_ids = set(recon.reconciled_je_ids or [])
    reconciled = [e for e in all_entries if e["je_id"] in reconciled_ids]
    matched_sum = sum(e["amount"] for e in reconciled)
    difference = float(recon.statement_balance) - (float(bank.opening_balance) + matched_sum)

    tol = float(recon.tolerance) if recon.tolerance is not None else 1.0
    if abs(difference) > tol:
        raise HTTPException(
            status_code=422,
            detail=f"Difference {difference:.2f} exceeds tolerance {tol:.2f}. Cannot write off.",
        )
    if abs(difference) < 0.005:
        raise HTTPException(status_code=422, detail="No difference to write off.")

    je_id = f"je:recon:writeoff:{session_id}"
    idem_c = je_idempotency_key(recon.statement_date, f"recon_wo_{session_id}", "c")
    idem_p = je_idempotency_key(recon.statement_date, f"recon_wo_{session_id}", "p")

    bank_debit = max(difference, 0)
    bank_credit = max(-difference, 0)
    entries = [
        {"account": bank.chart_account_code, "debit": bank_debit, "credit": bank_credit},
        {"account": payload.account_code, "debit": bank_credit, "credit": bank_debit},
    ]

    await emit_event(
        db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={"memo": payload.memo, "ts": recon.statement_date, "entries": entries, "je_type": "recon_writeoff"},
        actor_id=user.id, location_id=None, source="reconciliation",
        idempotency_key=idem_c, metadata_={"recon_session_id": str(session_id)},
    )
    await emit_event(
        db, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
        event_type="acc.journal_entry.posted",
        data={"ts": recon.statement_date}, actor_id=user.id, location_id=None, source="reconciliation",
        idempotency_key=idem_p, metadata_={},
    )

    existing = list(recon.reconciled_je_ids or [])
    if je_id not in existing:
        existing.append(je_id)
        recon.reconciled_je_ids = existing
    await db.commit()
    return {"je_id": je_id, "amount": difference}


# ── Reconciliation Rules ───────────────────────────────────────────────────────

@router.get("/rules")
async def get_recon_rules(
    bank_account_id: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_session),
) -> dict:
    q = select(ReconciliationRule).where(ReconciliationRule.company_id == company_id)
    if bank_account_id:
        q = q.where(ReconciliationRule.bank_account_id == uuid.UUID(bank_account_id))
    rows = (await db.execute(q.order_by(ReconciliationRule.created_at))).scalars().all()
    return {"items": [_rule_to_dict(r) for r in rows], "total": len(rows)}


@router.post("/rules")
async def create_recon_rule(
    payload: ReconRuleCreate,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    rule = ReconciliationRule(
        id=uuid.uuid4(),
        company_id=company_id,
        bank_account_id=uuid.UUID(payload.bank_account_id),
        match_field=payload.match_field,
        match_pattern=payload.match_pattern,
        match_type=payload.match_type,
        target_account_code=payload.target_account_code,
        default_memo=payload.default_memo,
        default_tax=payload.default_tax,
        is_active=True,
        times_applied=0,
    )
    db.add(rule)
    await db.commit()
    return _rule_to_dict(rule)


@router.patch("/rules/{rule_id}")
async def patch_recon_rule(
    rule_id: uuid.UUID,
    payload: ReconRulePatch,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    rule = (await db.execute(
        select(ReconciliationRule).where(
            ReconciliationRule.id == rule_id,
            ReconciliationRule.company_id == company_id,
        )
    )).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    for field in ("match_field", "match_pattern", "match_type", "target_account_code",
                  "default_memo", "default_tax", "is_active"):
        val = getattr(payload, field)
        if val is not None:
            setattr(rule, field, val)
    await db.commit()
    return _rule_to_dict(rule)


@router.delete("/rules/{rule_id}")
async def delete_recon_rule(
    rule_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict:
    rule = (await db.execute(
        select(ReconciliationRule).where(
            ReconciliationRule.id == rule_id,
            ReconciliationRule.company_id == company_id,
        )
    )).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.delete(rule)
    await db.commit()
    return {"deleted": str(rule_id)}


# ── Helper ────────────────────────────────────────────────────────────────────

async def _get_recon_and_line(
    db: AsyncSession,
    session_id: uuid.UUID,
    line_id: uuid.UUID,
    company_id: uuid.UUID,
) -> tuple[ReconciliationSession, BankStatementLine]:
    recon = (await db.execute(
        select(ReconciliationSession).where(
            ReconciliationSession.id == session_id,
            ReconciliationSession.company_id == company_id,
        )
    )).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail="Reconciliation session not found")
    if recon.status == "completed":
        raise HTTPException(status_code=409, detail="Session already completed")
    sl = (await db.execute(
        select(BankStatementLine).where(
            BankStatementLine.id == line_id,
            BankStatementLine.reconciliation_id == session_id,
        )
    )).scalar_one_or_none()
    if not sl:
        raise HTTPException(status_code=404, detail="Statement line not found")
    return recon, sl


# ── Period Lock + Fiscal Year Close ──────────────────────────────────────────


class PeriodLockPayload(BaseModel):
    lock_date: str | None  # ISO date or None to unlock


class CloseYearPayload(BaseModel):
    fiscal_year_end: str  # ISO date, e.g. "2025-12-31"


@router.get("/period-lock")
async def get_period_lock(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    settings = company.settings or {}
    return {
        "lock_date": settings.get("lock_date"),
        "lock_date_set_by": settings.get("lock_date_set_by"),
        "lock_date_set_at": settings.get("lock_date_set_at"),
    }


@router.post("/period-lock")
async def set_period_lock(
    payload: PeriodLockPayload,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user: object = Depends(get_current_user),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    settings = dict(company.settings or {})
    if payload.lock_date:
        # Validate date format
        from datetime import date as date_type
        try:
            date_type.fromisoformat(payload.lock_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format. Use YYYY-MM-DD.")
        settings["lock_date"] = payload.lock_date
        settings["lock_date_set_by"] = str(user.id)
        settings["lock_date_set_at"] = datetime.now(timezone.utc).isoformat()
    else:
        settings.pop("lock_date", None)
        settings.pop("lock_date_set_by", None)
        settings.pop("lock_date_set_at", None)
    company.settings = settings
    await session.commit()
    return {
        "lock_date": settings.get("lock_date"),
        "lock_date_set_by": settings.get("lock_date_set_by"),
        "lock_date_set_at": settings.get("lock_date_set_at"),
    }


@router.post("/close-year")
async def close_fiscal_year(
    payload: CloseYearPayload,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user: object = Depends(get_current_user),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Close a fiscal year: zero revenue + expense accounts, transfer net income to Retained Earnings."""
    from celerp.models.company import Company

    year_end = payload.fiscal_year_end
    # Build account balances through the year-end date
    posted = await _je_rows(session, company_id)
    balances = _build_balances(posted, None, year_end)

    # Get chart of accounts to determine account types
    acct_rows = (
        await session.execute(
            select(Account).where(Account.company_id == company_id)
        )
    ).scalars().all()
    acct_map = {a.code: a for a in acct_rows}

    # Collect revenue (4xxx) and expense/COGS (5xxx, 6xxx) balances
    closing_entries: list[dict] = []
    net_income = Decimal("0")

    for code, balance in balances.items():
        acct = acct_map.get(code)
        if not acct or acct.account_type not in ("revenue", "expense", "cogs"):
            continue
        if balance == 0:
            continue
        # Reverse whatever balance the account carries so it starts the new year at
        # zero: debit a credit surplus (revenue), credit a debit surplus (costs).
        closing_entries.append({"account": code, "debit": float(max(-balance, 0)), "credit": float(max(balance, 0))})
        # balance = debit - credit, so credit-normal revenue arrives negative and
        # debit-normal costs positive; negating both nets income minus costs.
        net_income -= balance

    if not closing_entries:
        raise HTTPException(status_code=422, detail="No revenue or expense balances to close.")

    # Net income goes to Retained Earnings (3200)
    # If net income is positive (profit): credit 3200
    # If net income is negative (loss): debit 3200
    net_float = float(net_income)
    closing_entries.append({
        "account": "3200",
        "debit": abs(net_float) if net_float < 0 else 0.0,
        "credit": net_float if net_float >= 0 else 0.0,
    })

    # Emit the closing JE
    je_id = f"je:close:{year_end}"
    from celerp.events.engine import emit_event
    from celerp.services.je_keys import je_idempotency_key

    idem_create = je_idempotency_key(year_end, "fiscal.close", "c")
    idem_posted = je_idempotency_key(year_end, "fiscal.close", "p")

    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={"memo": f"Fiscal year close {year_end}", "entries": closing_entries, "ts": year_end},
        actor_id=user.id,
        location_id=None,
        source="fiscal_close",
        idempotency_key=idem_create,
        metadata_={"trigger": "fiscal.close", "year_end": year_end},
    )
    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.posted",
        data={"ts": year_end},
        actor_id=user.id,
        location_id=None,
        source="fiscal_close",
        idempotency_key=idem_posted,
        metadata_={"trigger": "fiscal.close", "year_end": year_end},
    )

    # Set period lock to the year-end date
    company = await session.get(Company, company_id)
    settings = dict(company.settings or {})
    settings["lock_date"] = year_end
    settings["lock_date_set_by"] = str(user.id)
    settings["lock_date_set_at"] = datetime.now(timezone.utc).isoformat()
    company.settings = settings

    await session.commit()

    return {
        "je_id": je_id,
        "year_end": year_end,
        "net_income": net_float,
        "entries_count": len(closing_entries),
        "lock_date": year_end,
    }
