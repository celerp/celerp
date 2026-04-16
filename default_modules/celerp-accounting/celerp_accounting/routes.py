# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form as FastForm, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp_accounting.models import Account, BankAccount, BankStatementLine, ReconciliationRule, ReconciliationSession
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user, require_manager

router = APIRouter(dependencies=[Depends(get_current_user)])

# Default Thai chart of accounts seeded on company creation.
# Follows Thai Accounting Standards (TAS) structure.
THAI_CHART_OF_ACCOUNTS: list[dict] = [
    # --- Assets ---
    {"code": "1000", "name": "Assets", "account_type": "asset", "parent_code": None},
    {"code": "1100", "name": "Current Assets", "account_type": "asset", "parent_code": "1000"},
    {"code": "1110", "name": "Cash and Cash Equivalents", "account_type": "asset", "parent_code": "1100"},
    {"code": "1120", "name": "Accounts Receivable", "account_type": "asset", "parent_code": "1100"},
    {"code": "1130", "name": "Inventory", "account_type": "asset", "parent_code": "1100"},
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
    for entry in THAI_CHART_OF_ACCOUNTS:
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
    """Create a default bank account so reconciliation is never empty."""
    from celerp.models.company import Company

    company = await session.get(Company, company_id)
    currency = (company.settings or {}).get("currency", "THB") if company else "THB"

    code = "1111"
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
    existing_codes = {
        row.code for row in (
            await session.execute(
                select(Account.code).where(Account.company_id == company_id)
            )
        ).scalars().all()
    }
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

    await session.flush()
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
        _select(LedgerEntry.idempotency_key).where(LedgerEntry.idempotency_key.in_(keys))
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

def _build_balances(rows: list, date_from: str | None, date_to: str | None) -> dict[str, Decimal]:
    """Aggregate net balance per account_code from posted journal entry projections.

    Returns {account_code: net_balance} where net_balance = total_debit - total_credit.
    Asset/Expense accounts are debit-normal (positive = debit balance).
    Liability/Equity/Revenue accounts are credit-normal (positive = credit balance, stored as positive here).
    We store the raw difference and let the report layer interpret sign conventions.
    """
    balances: dict[str, Decimal] = {}
    for row in rows:
        state = row.state
        if state.get("status") != "posted":
            continue
        ts = state.get("ts") or state.get("created_at") or ""
        if date_from and ts < date_from:
            continue
        if date_to and ts > date_to:
            continue
        for entry in state.get("entries", []):
            code = entry.get("account")
            if not code:
                continue
            debit = Decimal(str(entry.get("debit") or 0))
            credit = Decimal(str(entry.get("credit") or 0))
            balances[code] = balances.get(code, Decimal(0)) + debit - credit
    return balances


@router.get("/trial-balance")
async def trial_balance(
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Trial balance: one row per account with debit/credit totals.

    Reads posted journal_entry projections. Each journal entry stores
    entries: [{account, debit?, credit?}] in its state.
    """
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()
    accounts = (
        await session.execute(
            select(Account).where(Account.company_id == company_id).order_by(Account.code)
        )
    ).scalars().all()
    account_map = {a.code: a for a in accounts}

    # Accumulate raw debit/credit per account code
    raw: dict[str, tuple[Decimal, Decimal]] = {}  # code -> (total_debit, total_credit)
    for row in rows:
        state = row.state
        if state.get("status") != "posted":
            continue
        ts = state.get("ts") or state.get("created_at") or ""
        if date_from and ts < date_from:
            continue
        if date_to and ts > date_to:
            continue
        for entry in state.get("entries", []):
            code = entry.get("account")
            if not code:
                continue
            d, c = raw.get(code, (Decimal(0), Decimal(0)))
            raw[code] = (d + Decimal(str(entry.get("debit") or 0)), c + Decimal(str(entry.get("credit") or 0)))

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
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()
    accounts = (
        await session.execute(
            select(Account).where(Account.company_id == company_id).order_by(Account.code)
        )
    ).scalars().all()
    account_map = {a.code: a for a in accounts}

    balances = _build_balances(rows, date_from, date_to)

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
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Balance sheet as of a given date (default: all posted entries to date)."""
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()
    accounts = (
        await session.execute(
            select(Account).where(Account.company_id == company_id).order_by(Account.code)
        )
    ).scalars().all()
    account_map = {a.code: a for a in accounts}

    # Balance sheet uses all entries up to as_of
    balances = _build_balances(rows, date_from=None, date_to=as_of)

    def _section(types: list[str], credit_normal: bool) -> tuple[list[dict], float]:
        lines = []
        for code in sorted(balances):
            acc = account_map.get(code)
            if not acc or acc.account_type not in types:
                continue
            net = balances[code]
            amount = float(-net) if credit_normal else float(net)
            lines.append({"code": code, "name": acc.name, "account_type": acc.account_type, "amount": amount})
        total = sum(l["amount"] for l in lines)
        return lines, total

    asset_lines, total_assets = _section(["asset"], credit_normal=False)
    liability_lines, total_liabilities = _section(["liability"], credit_normal=True)
    equity_lines, total_equity = _section(["equity"], credit_normal=True)

    # Derive retained earnings from P&L (Assets - Liabilities - explicit Equity)
    retained_earnings = total_assets - total_liabilities - total_equity
    if abs(retained_earnings) >= 0.01:
        equity_lines.append({"code": "—", "name": "Retained Earnings (derived)", "account_type": "equity", "amount": retained_earnings})
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
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()
    net = Decimal(str(opening_balance))
    for row in rows:
        state = row.state
        if state.get("status") != "posted":
            continue
        for entry in state.get("entries", []):
            if entry.get("account") == chart_account_code:
                net += Decimal(str(entry.get("debit") or 0)) - Decimal(str(entry.get("credit") or 0))
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
        currency=payload.currency,
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
            data={},
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
        b.currency = payload.currency
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
        data={},
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


def _je_entries_for_account(rows: list, account_code: str) -> list[dict]:
    """Return list of {je_id, ts, memo, amount, debit, credit} for a given account code."""
    result = []
    for row in rows:
        state = row.state
        if state.get("status") != "posted":
            continue
        for entry in state.get("entries", []):
            if entry.get("account") == account_code:
                result.append({
                    "je_id": row.entity_id,
                    "ts": state.get("ts") or state.get("created_at") or "",
                    "memo": state.get("memo", ""),
                    "debit": float(entry.get("debit") or 0),
                    "credit": float(entry.get("credit") or 0),
                    "amount": float(entry.get("debit") or 0) - float(entry.get("credit") or 0),
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

    je_rows = (
        await db.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()

    all_entries = _je_entries_for_account(je_rows, bank.chart_account_code)
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

    je_rows = (
        await db.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()
    all_entries = _je_entries_for_account(je_rows, bank.chart_account_code)
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

    je_rows = (await db.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "journal_entry",
        )
    )).scalars().all()
    book_entries = _je_entries_for_account(je_rows, bank.chart_account_code)
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
        data={}, actor_id=user.id, location_id=None, source="reconciliation",
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
        data={}, actor_id=user.id, location_id=None, source="reconciliation",
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

    je_rows = (await db.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "journal_entry",
        )
    )).scalars().all()
    all_entries = _je_entries_for_account(je_rows, bank.chart_account_code)
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
        data={}, actor_id=user.id, location_id=None, source="reconciliation",
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
    from decimal import Decimal
    from celerp.models.company import Company

    year_end = payload.fiscal_year_end
    # Build account balances through the year-end date
    je_rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()
    balances = _build_balances(je_rows, None, year_end)

    # Get chart of accounts to determine account types
    acct_rows = (
        await session.execute(
            select(Account).where(Account.company_id == company_id)
        )
    ).scalars().all()
    acct_map = {a.code: a for a in acct_rows}

    # Collect revenue (4xxx) and expense (5xxx, 6xxx) balances
    closing_entries: list[dict] = []
    net_income = Decimal("0")

    for code, balance in balances.items():
        acct = acct_map.get(code)
        if not acct:
            continue
        if acct.type in ("revenue", "income"):
            # Revenue accounts have credit-normal balances (negative in our debit-credit system)
            # Close by debiting the revenue account
            if balance != 0:
                closing_entries.append({"account": code, "debit": float(max(balance, 0)), "credit": float(abs(min(balance, 0)))})
                net_income -= balance  # Revenue reduces net income calc (credit normal -> subtract)
        elif acct.type in ("expense", "cost_of_goods"):
            # Expense accounts have debit-normal balances (positive)
            # Close by crediting the expense account
            if balance != 0:
                closing_entries.append({"account": code, "debit": float(max(-balance, 0)), "credit": float(max(balance, 0))})
                net_income += balance  # Expenses reduce net income

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
        data={},
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
