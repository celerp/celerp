# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
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
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.je_keys import je_void_data
from celerp.services.line_measures import line_label
from celerp.services.money import (
    checked_exchange_rate, currency_dp, round_money, to_base, to_decimal, to_stored_float,
)
from celerp.services.permissions import require_permission

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
    {"code": "6960", "name": "Foreign Exchange Difference", "account_type": "expense", "parent_code": "6000"},
]


# The sections of the cash flow statement, and the values an account may be
# classified as. This endpoint module is the authoritative side; the chart of
# accounts screen holds the same list and a test keeps the two in lockstep.
CASH_FLOW_CATEGORIES = ("operating", "investing", "financing")

# The account types the reports know how to sign and classify. Same arrangement as
# the list above: this module is authoritative, the chart of accounts screen holds a
# copy for its dropdown, and a test keeps the two in lockstep.
ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "cogs", "expense", "other")


class AccountCreate(BaseModel):
    code: str
    name: str
    account_type: str  # asset|liability|equity|revenue|expense|cogs
    parent_code: str | None = None
    cash_flow_category: str | None = None  # unset means "derive it from type and code"


class AccountPatch(BaseModel):
    name: str | None = None
    account_type: str | None = None
    parent_code: str | None = None
    is_active: bool | None = None
    # None leaves the override alone; "" clears it back to the derived default.
    cash_flow_category: str | None = None


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
        "cash_flow_category": acc.cash_flow_category,
    }


def _checked_account_type(value: str) -> str:
    """The account's type, which decides its sign on every report. A type outside
    the known set has no honest sign convention, so it is refused rather than
    guessed at."""
    if value not in ACCOUNT_TYPES:
        raise HTTPException(
            status_code=422,
            detail="Account type must be one of: " + ", ".join(ACCOUNT_TYPES) + ".",
        )
    return value


def _checked_cash_flow_category(value: str | None) -> str | None:
    """The stored override, or None for "derive it". An empty value clears the
    override; anything else must name a section the statement actually has."""
    if not value:
        return None
    if value not in CASH_FLOW_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail="Cash flow category must be one of: " + ", ".join(CASH_FLOW_CATEGORIES) + ".",
        )
    return value


@router.get("/chart")
async def get_chart(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("view_financial_reports"),
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
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = require_permission("manage_accounting"),
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
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = require_permission("manage_accounting"),
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
        account_type=_checked_account_type(payload.account_type),
        parent_code=payload.parent_code,
        cash_flow_category=_checked_cash_flow_category(payload.cash_flow_category),
    )
    session.add(acc)
    await session.commit()
    return _account_to_dict(acc)


@router.patch("/accounts/{code}")
async def patch_account(
    code: str,
    payload: AccountPatch,
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = require_permission("manage_accounting"),
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
        acc.account_type = _checked_account_type(payload.account_type)
    if payload.parent_code is not None:
        acc.parent_code = payload.parent_code
    if payload.is_active is not None:
        acc.is_active = payload.is_active
    if payload.cash_flow_category is not None:
        acc.cash_flow_category = _checked_cash_flow_category(payload.cash_flow_category)

    await session.commit()
    return _account_to_dict(acc)


@router.get("/import/template", response_class=PlainTextResponse, include_in_schema=False)
async def import_accounting_template(
    _: None = require_permission("manage_accounting"),
):
    return PlainTextResponse(
        "entity_id,event_type,idempotency_key,code,name,account_type,parent_code,is_active\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounting.csv"},
    )


@router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_accounting(
    body: AccBatchImportRequest,
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = require_permission("manage_accounting"),
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
            # An imported line may name a party. Checked here, at the boundary,
            # because an entry whose contact resolves to nothing would post to a
            # control account and then be missing from every statement, with
            # nothing on screen to say why.
            entries = rec.data.get("entries") if isinstance(rec.data, dict) else None
            if isinstance(entries, list):
                await _check_line_contacts(
                    session, company_id, [e for e in entries if isinstance(e, dict)])
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

def _require_iso_date(value: str | None, field: str) -> None:
    """Reject anything that is not a strict YYYY-MM-DD date.

    Reports filter and sort by lexical string compare, so variants
    fromisoformat tolerates (basic 20260105, week dates) would sort outside
    their real period and vanish from date-bounded reports. An unparsable
    filter is refused rather than silently yielding an empty report, which
    reads as "no activity" when it really means "the question was malformed".
    """
    if value is None:
        return
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field} date format. Use YYYY-MM-DD.",
        )
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field} date format. Use YYYY-MM-DD.",
        )


def _require_date_range(date_from: str | None, date_to: str | None) -> None:
    """Check the date filter pair every report takes, in one place.

    Both ends must be strict ISO dates and the range must run forwards. A
    backwards range matches nothing, so answering it produces a confident
    empty report that reads as a quiet period instead of as a question that
    cannot be answered. Refused here so every report refuses it alike.
    """
    _require_iso_date(date_from, "date_from")
    _require_iso_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail=(f"date_from {date_from} is after date_to {date_to}. "
                    "Start the range on or before the date it ends."),
        )


# The longest free-text search the journal accepts. Long enough for a memo or a
# document reference, short enough that the value cannot be used to make the
# server read every entry against a pathological pattern.
_JOURNAL_Q_MAX = 200


@dataclass(frozen=True)
class _JournalFilter:
    """What the journal was asked to narrow itself to, and the rule that does it.

    One home for the matching rule, because the journal, the extended journal and
    both of their exports all narrow through this and a second copy of the rule
    would eventually disagree with the first.
    """

    account: str | None = None
    q: str | None = None
    amount: Decimal | None = None

    @property
    def active(self) -> bool:
        return bool(self.account or self.q or self.amount is not None)

    def matches(self, entry: dict) -> bool:
        """Whether an entry belongs in a filtered journal.

        Every filter given has to match, so adding one always narrows the answer.
        A filter matches on any one of the entry's lines, and the entry is then
        emitted whole: emitting only the matching lines would print half an entry,
        which does not balance and is not a journal.
        """
        lines = entry.get("lines") or []
        if self.account and not any(l.get("account") == self.account for l in lines):
            return False
        if self.q:
            haystack = [entry.get("memo") or "",
                        (entry.get("source_doc") or {}).get("doc_ref") or ""]
            for l in lines:
                haystack += [l.get("account") or "", l.get("name") or ""]
            if not any(self.q in h.lower() for h in haystack):
                return False
        if self.amount is not None and not any(
                to_decimal(l.get("debit") or 0) == self.amount
                or to_decimal(l.get("credit") or 0) == self.amount for l in lines):
            return False
        return True


def _require_q(q: str) -> None:
    """Refuse a search longer than the journal reads."""
    if len(q) > _JOURNAL_Q_MAX:
        raise HTTPException(
            status_code=422,
            detail=(f"Search text is {len(q)} characters, longer than the "
                    f"{_JOURNAL_Q_MAX} the journal searches."),
        )


def _require_amount(value: str) -> Decimal | None:
    """The amount filter as a number, or 422 saying why it is not one.

    A figure that cannot be read is refused rather than ignored, because a
    silently dropped amount filter answers with the whole book under a heading
    that says it was narrowed.
    """
    if not value:
        return None
    try:
        amount = to_decimal(value)
    except (ArithmeticError, TypeError, ValueError):
        amount = None
    # `nan` and `inf` parse as decimals but are not figures: one raises on every
    # comparison, the other matches nothing while claiming to be a filter.
    if amount is None or not amount.is_finite():
        raise HTTPException(
            status_code=422,
            detail=f"Amount filter {value} is not a number.",
        )
    if amount < 0:
        raise HTTPException(
            status_code=422,
            detail=("Amount filter cannot be negative. Journal figures are "
                    "posted as a debit or a credit, both positive."),
        )
    return amount


async def _require_account(
    session: AsyncSession, company_id: uuid.UUID, code: str
) -> None:
    """Check the journal's account filter names a real account.

    Refused as 422 for the reason `_require_contact_filter` gives: the report is
    there, it is the filter that cannot be applied, and a mistyped code would
    otherwise answer with an empty journal that reads as a quiet period.
    """
    if not code:
        return
    found = (await session.execute(
        select(Account.id).where(Account.company_id == company_id, Account.code == code)
    )).scalar_one_or_none()
    if found is None:
        raise HTTPException(
            status_code=422,
            detail=f"No account matches code {code}.",
        )


async def _journal_filter(
    session: AsyncSession, company_id: uuid.UUID,
    account: str | None, q: str | None, amount: str | None,
) -> _JournalFilter:
    """Validate the three journal filters and hand back the one that matches.

    Blank values are dropped before validating, not refused: clearing a filter
    field submits it empty, and answering that with 422 would refuse the act of
    clearing a filter.
    """
    account = (account or "").strip()
    q = (q or "").strip()
    amount = (amount or "").strip()
    await _require_account(session, company_id, account)
    _require_q(q)
    return _JournalFilter(
        account=account or None,
        q=q.lower() or None,
        amount=_require_amount(amount),
    )


# Account types whose balance is credit-normal. Every other type, including the
# unknown type of a code with no chart entry, is reported on the debit side.
_CREDIT_NORMAL_TYPES = frozenset({"liability", "equity", "revenue"})

# Legacy doc_type spellings normalised to their canonical names, so a statement
# row and the AR/AP aging call the same document the same thing.
_DOC_TYPE_ALIASES = {"Invoice": "invoice", "PO": "purchase_order"}


def _is_debit_normal(account_type: str) -> bool:
    """Whether an account's balance is signed positive on the debit side.

    One rule for the general ledger and the account ledger, so a drilldown can
    never contradict the sign of the report row it was opened from. A type the
    chart does not use, including the unknown type of a code that has posted
    lines but no chart entry, keeps the raw debit-minus-credit figure rather
    than having a sign guessed for it.
    """
    return account_type not in _CREDIT_NORMAL_TYPES


async def _contact_row(
    session: AsyncSession, company_id: uuid.UUID, contact_id: str
) -> Projection | None:
    """The contact projection behind an id, or None when the id names no contact."""
    row = await session.get(Projection, (company_id, contact_id))
    return row if row and row.entity_type == "contact" else None


async def _require_contact(
    session: AsyncSession, company_id: uuid.UUID, contact_id: str
) -> Projection:
    """The contact a statement was asked for, or 404 for a contact that is not there."""
    row = await _contact_row(session, company_id, contact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return row


async def _require_contact_filter(
    session: AsyncSession, company_id: uuid.UUID, contact_id: str | None
) -> None:
    """Check a report's contact filter before it can silence the report.

    The empty string is the reports' own bucket for lines with no resolvable
    party, so it is a real filter value rather than a missing contact. Any
    other id has to name a contact: a mistyped one would otherwise return an
    empty subledger, which reads as "this party owes nothing".

    Refused as 422 rather than 404, like the date filters: the account being
    reported on is there, it is the filter that cannot be applied, and 422 is
    the status whose message reaches the reader intact.
    """
    if contact_id is None or contact_id == "":
        return
    if await _contact_row(session, company_id, contact_id) is None:
        raise HTTPException(
            status_code=422,
            detail=f"No contact matches contact_id {contact_id}.",
        )


async def _check_line_contacts(
    session: AsyncSession, company_id: uuid.UUID, entries: list[dict]
) -> None:
    """Refuse a set of journal entry lines if any names a contact that is not there.

    A line's contact is what puts a posting on that party's statement, so an id
    matching no contact would post an entry no statement can ever show and no
    control-account bucket can ever explain. Checked once per distinct contact
    named, and by the same rule for every path that writes lines: manual
    entries, reconciliation, and the batch import.
    """
    named = {e.get("contact") for e in entries if isinstance(e.get("contact"), str) and e.get("contact")}
    if not named:
        return
    missing = []
    for cid in sorted(named):
        if await _contact_row(session, company_id, cid) is None:
            missing.append(cid)
    if missing:
        raise HTTPException(
            status_code=422,
            detail="No contact matches " + ", ".join(missing) + ".",
        )


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


def _validated_line_fx(base: str, line: "ManualJELine", index: int) -> tuple[str, Decimal] | None:
    """(currency, rate) for a line typed in a foreign currency, or None for a
    line already in base currency.

    Every message names the line at fault: an accountant who mistypes a rate on
    the fourth line of an entry should be told which line was refused, not handed
    a rejection they have to search for.
    """
    where = f"Line {index + 1}"
    currency = (line.currency or "").upper()
    if not currency and line.rate is None:
        return None
    if not currency:
        raise HTTPException(
            status_code=422,
            detail=f"{where} has an exchange rate but no currency. A rate needs the currency it converts from.",
        )
    if currency not in ISO_4217_CURRENCIES:
        raise HTTPException(
            status_code=422,
            detail=f"{where}: unknown currency {line.currency}. Use a three-letter ISO 4217 code.",
        )
    if currency == base:
        # The company's own currency converts at 1 by definition. An explicit 1 is
        # accepted and dropped so the stored line looks like every other base-
        # currency line; anything else would silently restate the amount.
        if line.rate is not None and to_decimal(line.rate) != 1:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{where}: {currency} is this company's own currency, so its rate "
                    f"is 1, not {line.rate}."
                ),
            )
        return None
    if line.rate is None:
        raise HTTPException(
            status_code=422,
            detail=f"{where} is in {currency} but has no exchange rate.",
        )
    try:
        return currency, checked_exchange_rate(line.rate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{where}: exchange rate {exc}.") from exc


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

    The document's own figures - its line items, its currency, its rate and its
    total - travel with the ref so the extended journal can name what was bought
    or sold on each posting. The projection row is already loaded whole here, so
    carrying them costs no extra read.
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

    # A credit note carries both ids; its own party is the one the entry belongs
    # to, which is not always the party on the invoice it settles.
    doc_ids = sorted(
        {m["doc_id"] for m in je_meta.values()}
        | {m["cn_id"] for m in je_meta.values() if m.get("cn_id")}
    )
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
        # The party this entry belongs to, resolved from the document that caused
        # it. Every document-driven posting records its doc_id, so no separate
        # party field has to be stored or backfilled for them.
        party_state = doc_states.get(meta["cn_id"], state) if meta.get("cn_id") else state
        contact_id = (party_state.get("contact_id") or party_state.get("customer_id")
                      or party_state.get("supplier_id"))
        doc_type = party_state.get("doc_type") or party_state.get("type") or ""
        refs[je_id] = {
            "doc_id": doc_id,
            "doc_ref": state.get("ref_id") or state.get("doc_number") or doc_id,
            "fx": fx,
            "contact_id": contact_id,
            "doc_type": _DOC_TYPE_ALIASES.get(doc_type, doc_type),
            "is_payment": isinstance(payment_index, int),
            "doc": state,
        }
    return refs


def _line_party(refs: dict[str, dict], je_id: str, entry: dict) -> str:
    """The party a journal entry line belongs to, or "" when none resolves.

    A line may name its own contact, which is how a manual posting to a control
    account reaches that party's statement. Everything else inherits the party of
    the document that caused the entry, resolved from the doc refs every report
    already loads. One rule for the account ledger, the general ledger and the
    statement, so a line can never sit in one party's bucket on one report and
    another's on the next. Lines that resolve to nothing are reported under the
    empty string, so a filtered view can never quietly drop them from the
    account's total.
    """
    named = entry.get("contact")
    if isinstance(named, str) and named:
        return named
    return (refs.get(je_id) or {}).get("contact_id") or ""


def _statement_kind(ref: dict) -> str:
    """What a statement row calls the entry behind it.

    A posting the books made for a document is named after that document; a
    payment against one is a payment whatever the document was; anything a
    person posted by hand is a journal entry, which is exactly what the reader
    needs to know to go looking for it.
    """
    if ref.get("is_payment"):
        return "payment"
    return ref.get("doc_type") or "journal"


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


async def _journal_payload(
    session: AsyncSession, company_id: uuid.UUID,
    date_from: str | None, date_to: str | None,
    filt: _JournalFilter | None = None,
) -> tuple[dict, dict, str]:
    """The journal, its source-doc refs and the base currency.

    The classical journal is the whole of this. The extended journal is this plus
    item detail attached afterwards, so the two can never show different entries,
    different lines or different totals for the same period. The filter is applied
    here for the same reason: both books, and both of their exports, narrow alike.
    """
    _require_date_range(date_from, date_to)
    rows = await _je_rows(session, company_id, include_void=True)
    # Dateless JEs count as pre-period, exactly like the trial balance,
    # general ledger, and per-account ledger: excluded once a start date is
    # set, included otherwise, so period totals cross-foot between reports.
    rows = [
        r for r in rows
        if not (date_from and (not r[2] or r[2] < date_from))
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
        ref = refs.get(je_id)
        # A document-linked entry carries its rate on the source doc, which is
        # one currency for the whole document; a manual entry carries a currency
        # and rate on each line. Manual entries posted before per-line currency
        # existed carry one `fx` for the entry instead, so their lines inherit it
        # here. This is the only place the older shape is read: stored events are
        # immutable, so an entry posted then still has to render now.
        entry_fx = ref["fx"] if ref else state.get("fx")
        lines = []
        # Kept per entry and folded into the running totals only once the entry
        # has passed the filter, so the totals are the totals of the entries
        # shown by construction rather than by a second pass over them.
        entry_debit = Decimal(0)
        entry_credit = Decimal(0)
        for entry in state.get("entries", []):
            code = entry.get("account")
            amounts = _line_amounts(entry)
            # Same rule as every other report: a line with no account code or
            # unparsable amounts is skipped, so the journal never shows a row
            # the trial balance and general ledger do not count.
            if not code or amounts is None:
                continue
            if posted:
                entry_debit += amounts[0]
                entry_credit += amounts[1]
            line_currency = entry.get("fx_currency") or (entry_fx or {}).get("currency")
            line_rate = entry.get("fx_rate") or (entry_fx or {}).get("rate")
            lines.append({
                "account": code,
                "name": account_names.get(code, code),
                "debit": float(amounts[0]),
                "credit": float(amounts[1]),
                # Present only on a foreign-currency line. A base-currency line
                # carries None, which the view renders as an empty cell rather
                # than a guessed figure.
                "fx_debit": entry.get("fx_debit"),
                "fx_credit": entry.get("fx_credit"),
                "fx_currency": line_currency,
                "fx_rate": float(line_rate) if line_rate else None,
            })
        out = {
            "je_id": je_id,
            "ts": ts,
            "memo": state.get("memo", ""),
            "status": state.get("status"),
            "je_type": state.get("je_type"),
            "void_reason": state.get("void_reason"),
            "source_doc": {"doc_id": ref["doc_id"], "doc_ref": ref["doc_ref"]} if ref else None,
            "lines": lines,
            "fx": entry_fx,
        }
        if filt is not None and not filt.matches(out):
            continue
        total_debit += entry_debit
        total_credit += entry_credit
        entries_out.append(out)

    return ({
        "date_from": date_from,
        "date_to": date_to,
        "entries": entries_out,
        "total_debit": to_stored_float(round_money(total_debit, base)),
        "total_credit": to_stored_float(round_money(total_credit, base)),
        # Stated by the payload rather than worked out again by each reader, so
        # the page, the print sheet and the CSV cannot disagree about whether
        # what they are showing is the whole book.
        "filtered": bool(filt and filt.active),
    }, refs, base)


@router.get("/journal")
async def journal(
    date_from: str | None = None,
    date_to: str | None = None,
    account: str | None = None,
    q: str | None = None,
    amount: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Journal: every entry with its lines, source-doc link, and FX info.

    Voided entries stay visible flagged status="void" - a journal is a record, and
    hiding voids would misstate it - but they are excluded from the period totals.

    `account`, `q` and `amount` narrow the entries; a narrowed answer says so in
    `filtered` and its totals are the totals of what it returned.
    """
    filt = await _journal_filter(session, company_id, account, q, amount)
    payload, _refs, _base = await _journal_payload(
        session, company_id, date_from, date_to, filt)
    return payload


# Which side of the books a document's items post to: a sale credits what it
# earned, a purchase debits what it bought, and a credit note reverses the sale
# it cancels. A document type absent here is one the books do not post item by
# item, so its entries are shown exactly as the classical journal shows them.
_ITEM_SIDE: dict[str, str] = {
    "invoice": "credit",
    "credit_note": "debit",
    "bill": "debit",
    "purchase_order": "debit",
}


def _doc_item_lines(doc: dict, base: str) -> list[dict]:
    """A document's items, each with what it is worth in its own currency and in
    the books'.

    Both figures are derived exactly as `celerp/services/auto_je.py` derived them
    when it posted the entry, down to the same rounding and the same skipping of
    lines worth nothing, so a line that footed there foots here. A line the
    arithmetic cannot read abandons the whole document rather than reporting a
    partial set of items as if it were the full one.
    """
    currency = doc.get("currency") or base
    rate = doc.get("conversion_rate") or 1
    out: list[dict] = []
    for li in doc.get("line_items") or []:
        try:
            net = (to_decimal(li.get("line_total") or 0)
                   or to_decimal(li.get("quantity") or 0) * to_decimal(li.get("unit_price") or 0))
            line_total = round_money(net, currency)
        except (ArithmeticError, TypeError, ValueError):
            return []
        if line_total <= 0:
            continue
        out.append({
            "item": line_label(li),
            "quantity": li.get("quantity"),
            "unit_price": li.get("unit_price"),
            "fx_amount": to_stored_float(line_total),
            "amount": to_decimal(to_base(to_stored_float(line_total), rate, base)),
        })
    return out


def _doc_net_posted(doc: dict, base: str) -> Decimal:
    """What a document's items are posted for in one line, in the books' currency:
    its total less its tax, converted the way auto_je converts it."""
    currency = doc.get("currency") or base
    rate = doc.get("conversion_rate") or 1
    try:
        net = round_money(to_decimal(doc.get("total") or 0), currency) - round_money(
            to_decimal(doc.get("tax") or 0), currency)
    except (ArithmeticError, TypeError, ValueError):
        return Decimal(0)
    return to_decimal(to_base(to_stored_float(round_money(net, currency)), rate, base))


def _item_row(line: dict, side: str, doc_line: dict, amount: Decimal) -> dict:
    """One extended-journal row: the posting, narrowed to the one item it is for.

    The foreign figure is the document line's own total, which is the figure the
    supplier or the customer sees, rather than the book amount divided back out.
    """
    other = "credit" if side == "debit" else "debit"
    return {
        **line,
        side: float(amount),
        other: 0.0,
        f"fx_{side}": doc_line["fx_amount"] if line.get("fx_currency") else None,
        f"fx_{other}": None,
        "item": doc_line["item"],
        "quantity": doc_line["quantity"],
        "unit_price": doc_line["unit_price"],
    }


def _expand_item_lines(lines: list[dict], ref: dict | None, base: str) -> tuple[list[dict], str]:
    """The entry's lines with the item each one is for attached, and what became of
    the attempt.

    The status is one of four, because "no item detail" covers four different
    facts and a reader has to be able to tell them apart:

    - `expanded`: the items are on the lines below.
    - `no_items`: there was never item detail to show. A manual entry, a payment
      moving cash against a control account, or a document kind that does not
      carry items.
    - `no_document`: the document the entry was posted for is not in the books
      any more, so there is nothing left to read items from.
    - `untied`: the document is there and it has items, but its lines do not
      account for what was posted, so no item was attached.

    Two shapes are recognised, and the money is checked before anything is
    attached: one posting covering the whole document, which becomes a row per
    item, and one posting per document line in document order, which is already a
    row per item. Anything else is left as the classical journal shows it, because
    item detail that does not tie to the posting is worse than no item detail.

    Where a single posting splits, the rows are the document's own line totals
    converted one by one; converting each and converting their sum can differ by
    a unit, so that difference lands on the last row and the rows always foot to
    the posting they came from. Only a difference that small is absorbed. A wider
    gap is a posting the lines do not account for, most often a document-level
    charge that is not one of its lines, and it refuses rather than inflating an
    item to swallow it.
    """
    if not ref:
        return lines, "no_items"
    doc = ref.get("doc") or {}
    # Checked before the payment exit: an entry pointing at a document that is no
    # longer readable is worth saying out loud whatever the entry was for, and it
    # is the one case here that means something is missing rather than absent.
    if not doc:
        return lines, "no_document"
    if ref.get("is_payment"):
        return lines, "no_items"
    side = _ITEM_SIDE.get(ref.get("doc_type") or "")
    doc_lines = _doc_item_lines(doc, base)
    if not side or not doc_lines:
        return lines, "no_items"
    other = "credit" if side == "debit" else "debit"

    posted = _doc_net_posted(doc, base)
    amounts = [d["amount"] for d in doc_lines]
    residual = posted - sum(amounts, Decimal(0))
    rounding_room = (Decimal(10) ** -currency_dp(base)) * len(doc_lines)
    if posted > 0 and abs(residual) <= rounding_room:
        for i, line in enumerate(lines):
            if to_decimal(line.get(side) or 0) == posted and not to_decimal(line.get(other) or 0):
                amounts[-1] += residual
                rows = [_item_row(line, side, d, a) for d, a in zip(doc_lines, amounts)]
                return lines[:i] + rows + lines[i + 1:], "expanded"

    on_side = [l for l in lines if to_decimal(l.get(side) or 0) > 0]
    head = on_side[:len(doc_lines)]
    if len(head) == len(doc_lines) and all(
            to_decimal(h.get(side) or 0) == d["amount"] for h, d in zip(head, doc_lines)):
        paired = {id(h): d for h, d in zip(head, doc_lines)}
        return [_item_row(l, side, paired[id(l)], paired[id(l)]["amount"]) if id(l) in paired else l
                for l in lines], "expanded"

    return lines, "untied"


@router.get("/extended-journal")
async def extended_journal(
    date_from: str | None = None,
    date_to: str | None = None,
    account: str | None = None,
    q: str | None = None,
    amount: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The journal with the item behind each posting named, where it can be.

    The same entries, the same lines and the same totals as the classical
    journal: nothing is posted, stored or recomputed here. A sale posted as one
    revenue line becomes a row per item sold, a purchase already posted line by
    line gains the item on each, and an entry whose figures do not tie to its
    document is shown as it stands. Every entry carries an `items_status` of
    `expanded`, `no_items`, `no_document` or `untied` (see `_expand_item_lines`),
    so a reader can tell "no item here" from "the item detail was not
    trustworthy" from "the document is gone".

    The filters are the classical journal's, applied to the same entries before
    any item detail is derived, so a filter can never give the two books
    different entry sets.
    """
    filt = await _journal_filter(session, company_id, account, q, amount)
    payload, refs, base = await _journal_payload(
        session, company_id, date_from, date_to, filt)
    for entry in payload["entries"]:
        lines, status = _expand_item_lines(entry["lines"], refs.get(entry["je_id"]), base)
        entry["lines"] = lines
        entry["items_status"] = status
    return payload


class ManualJELine(BaseModel):
    account: str
    debit: float = 0
    credit: float = 0
    # The party this line belongs to. Document-driven postings derive their party
    # from the document, so this is for the entries that have no document: a
    # write-off, an opening balance, an adjustment posted straight to a control
    # account. Without it those lines can only ever sit in the unattributed
    # bucket, and the party's statement would not show a movement its own
    # balance includes.
    contact: str | None = None
    # The currency this line's amounts were typed in, and what one unit of it is
    # worth in base currency. Both absent on an ordinary base-currency line,
    # which is every line of most entries. Carried per line rather than per entry
    # because settling an invoice in one currency with cash in another is one
    # transaction, and the books already store every posting in base currency.
    currency: str | None = None
    rate: float | None = None


class ManualJECreate(BaseModel):
    ts: str  # ISO date "YYYY-MM-DD"
    memo: str = ""
    entries: list[ManualJELine]
    idempotency_token: str


class ManualJEVoidPayload(BaseModel):
    reason: str | None = None


# A ceiling on how much writing one request can ask for, so a hand-built or mis-clicked
# selection cannot turn into an unbounded write loop holding a connection. A larger
# selection is refused with the count and the limit, not silently truncated.
_BULK_VOID_LIMIT = 200


class BulkJEVoidPayload(BaseModel):
    je_ids: list[str]
    reason: str | None = None


@router.post("/journal-entries")
async def create_manual_journal_entry(
    payload: ManualJECreate,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("manage_accounting"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Post a manual journal entry. Validates accounts and balance; the period
    lock is enforced by the event engine on the entry's date."""
    _require_iso_date(payload.ts or "", "entry")
    if len(payload.entries) < 2:
        raise HTTPException(status_code=422, detail="A journal entry needs at least 2 lines.")
    if not payload.idempotency_token:
        raise HTTPException(status_code=422, detail="idempotency_token is required.")
    await _check_line_contacts(
        session, company_id, [{"contact": line.contact} for line in payload.entries])

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
    # Every posting is stored in base currency, so the entry is balanced there,
    # on the same converted figures the author is looking at while typing. A line
    # in a foreign currency is validated and rounded at that currency's precision
    # first, then converted; a line without one is already base currency.
    total_debit = Decimal(0)
    total_credit = Decimal(0)
    entries: list[dict] = []
    for index, line in enumerate(payload.entries):
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
        line_fx = _validated_line_fx(base, line, index)
        amount_currency = line_fx[0] if line_fx else base
        d = round_money(line.debit, amount_currency)
        c = round_money(line.credit, amount_currency)
        if d > 0 and c > 0:
            raise HTTPException(status_code=422, detail="Each line must have an amount on only one side, debit or credit.")
        if d == 0 and c == 0:
            raise HTTPException(status_code=422, detail="Each line needs a debit or credit amount.")
        if line_fx:
            currency, rate = line_fx
            bd = round_money(d * rate, base)
            bc = round_money(c * rate, base)
            stored = {
                "account": line.account,
                "debit": to_stored_float(bd),
                "credit": to_stored_float(bc),
                "fx_debit": to_stored_float(d) if d else None,
                "fx_credit": to_stored_float(c) if c else None,
                "fx_currency": currency,
                "fx_rate": to_stored_float(rate),
            }
        else:
            bd, bc = d, c
            stored = {"account": line.account, "debit": to_stored_float(bd), "credit": to_stored_float(bc)}
        total_debit += bd
        total_credit += bc
        # Only lines that name a party carry the key, so an entry with no party
        # stores exactly what it stored before.
        if line.contact:
            stored["contact"] = line.contact
        entries.append(stored)

    if total_debit != total_credit:
        # The gap is named, not just the two totals: when lines are in different
        # currencies the difference is the exchange difference the author still
        # has to post, and it is the figure they need rather than one to work out.
        gap = abs(total_debit - total_credit)
        short = "credit" if total_debit > total_credit else "debit"
        raise HTTPException(
            status_code=422,
            detail=(
                f"Entry is out of balance in {base}: debits {total_debit} do not equal "
                f"credits {total_credit}. The {short} side is short by {gap}."
            ),
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
        data={
            "memo": payload.memo, "ts": payload.ts, "entries": entries,
            "je_type": "manual", "status": "posted",
        },
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
            # The foreign amounts and the rate that converted them come along: two
            # different rates can convert to the same base figures, and comparing
            # only those would read a corrected rate as an identical retry and
            # silently post nothing.
            out = []
            for l in lines or []:
                amounts = _line_amounts(l) or (Decimal(0), Decimal(0))
                out.append((
                    l.get("account"),
                    round_money(amounts[0], base), round_money(amounts[1], base),
                    l.get("fx_debit"), l.get("fx_credit"),
                    (l.get("fx_currency") or "").upper(),
                    to_decimal(l.get("fx_rate") or 0),
                ))
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


async def _void_one(session, *, company_id, actor_id, entity_id: str, reason: str | None) -> dict:
    """Void one manual journal entry, committing it. Raises the refusal as HTTPException.

    Both the single-entry route and the bulk route go through here, so a rule about
    what may be voided cannot hold on one and not the other.

    The actor arrives as an id rather than the user object because the bulk route
    rolls a refused entry back, and a rollback expires every instance the session
    holds - including the authenticated user, whose next attribute read would then
    try to load lazily and fail outside the async context.
    """
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

    data = je_void_data(reason, state)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.voided",
        data=data,
        actor_id=actor_id,
        location_id=None,
        source="manual",
        idempotency_key=f"{entity_id}:void",
        metadata_={},
    )
    await session.commit()
    return {"je_id": entity_id, "status": "void", "void_reason": reason}


@router.post("/journal-entries/bulk-void")
async def bulk_void_journal_entries(
    payload: BulkJEVoidPayload,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("manage_accounting"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Void several manual journal entries, reporting each one's outcome.

    Every entry is voided by the same code the single-entry route uses, one at a
    time, and each one commits on its own. A refusal is recorded against the entry
    it belongs to and the rest of the selection carries on: refusing four valid
    voids because the fifth sits in a locked period would leave the reader to
    re-tick the four and try again, having been told nothing about which was which.
    """
    je_ids = list(dict.fromkeys(x.strip() for x in payload.je_ids if x.strip()))
    if not je_ids:
        raise HTTPException(status_code=422, detail="No journal entries selected.")
    if len(je_ids) > _BULK_VOID_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=f"Too many journal entries in one request: {len(je_ids)}. "
                   f"The limit is {_BULK_VOID_LIMIT}; void them in smaller batches.",
        )

    actor_id = user.id
    results = []
    for entity_id in je_ids:
        try:
            results.append(await _void_one(
                session, company_id=company_id, actor_id=actor_id,
                entity_id=entity_id, reason=payload.reason,
            ))
        except HTTPException as exc:
            # Every entry starts from a clean session, whatever the one before it
            # left behind. Today's refusals are all raised before anything is
            # written - the period lock is checked ahead of the insert
            # (celerp/events/engine.py:130) and the insert itself is nested
            # (:139) - so this discards nothing. It is here so that the next
            # entry's commit can never carry a refused entry's half-written work
            # into the books along with its own.
            await session.rollback()
            results.append({"je_id": entity_id, "status": "refused", "detail": exc.detail})

    voided = sum(1 for r in results if r["status"] == "void")
    return {"results": results, "voided": voided, "refused": len(results) - voided}


@router.post("/journal-entries/{entity_id}/void")
async def void_manual_journal_entry(
    entity_id: str,
    payload: ManualJEVoidPayload | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("manage_accounting"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Void a manual journal entry. The voided entry stays on the record - accounting
    requires the full trail, so entries are never deleted."""
    return await _void_one(
        session, company_id=company_id, actor_id=user.id,
        entity_id=entity_id, reason=payload.reason if payload else None,
    )


@router.get("/ledger/{account_code}")
async def account_ledger(
    account_code: str,
    date_from: str | None = None,
    date_to: str | None = None,
    contact_id: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Account ledger: posted JE lines for one account, with opening balance,
    running balance, closing balance and source doc links.

    contact_id narrows the account to one party, which is what turns a control
    account into that party's subledger: 1120 filtered to a customer is that
    customer's receivable, and every such line sums back to the control account.
    Lines with no resolvable party are reported under the empty string so a
    filtered view can never quietly exclude them from the account's total.
    """
    _require_date_range(date_from, date_to)
    await _require_contact_filter(session, company_id, contact_id)

    # Fetch account metadata for name + type (sign convention)
    account = (
        await session.execute(
            select(Account).where(Account.company_id == company_id, Account.code == account_code)
        )
    ).scalar_one_or_none()

    posted = await _je_rows(session, company_id)

    # Lines are matched by the literal account code, exactly like the trial
    # balance, journal, and general ledger bucket them, so the drilldown always
    # reconciles with the report row it was opened from. Legacy entries posted
    # directly to a parent code appear on the parent's own ledger.
    match_codes = {account_code}

    # A code with no chart entry still has a ledger while the books hold lines
    # for it: the general ledger lists such codes, and a drilldown must open on
    # a row the report just showed. A code the chart and the books both know
    # nothing about is a question about an account that does not exist, and
    # answering it would invent a chart entry the company never created.
    # The scan ignores the date and contact filters on purpose: a filter that
    # matches nothing is an empty period, not a missing account.
    if account is None and not any(
        entry.get("account") in match_codes
        for _, state, _ in posted
        for entry in state.get("entries", [])
    ):
        raise HTTPException(status_code=404, detail="Account not found")

    refs = await _je_doc_refs(session, company_id, [je_id for je_id, _, _ in posted])

    account_type = account.account_type if account else "unknown"
    debit_normal = _is_debit_normal(account_type)

    def _signed(d: Decimal, c: Decimal) -> Decimal:
        return (d - c) if debit_normal else (c - d)

    # Filter to lines that touch this account, apply date filter
    lines = []
    opening = Decimal(0)
    for je_id, state, ts in posted:
        ref = refs.get(je_id) or {}
        for entry in state.get("entries", []):
            if entry.get("account") not in match_codes:
                continue
            line_contact = _line_party(refs, je_id, entry)
            if contact_id is not None and line_contact != contact_id:
                continue
            amounts = _line_amounts(entry)
            if amounts is None:
                continue
            # Dateless JEs (ts="") count as pre-period, exactly as the trial
            # balance, journal, and general ledger treat them: excluded once a
            # start date is set, included otherwise.
            if date_from and (not ts or ts < date_from):
                opening += _signed(amounts[0], amounts[1])
                continue
            if ts and date_to and ts > date_to:
                continue
            lines.append({
                "date": ts,
                "je_id": je_id,
                "memo": state.get("memo", ""),
                "doc_id": ref.get("doc_id"),
                "doc_ref": ref.get("doc_ref"),
                "contact_id": line_contact,
                "debit": float(amounts[0]),
                "credit": float(amounts[1]),
            })

    # Sort chronologically for running balance
    lines.sort(key=lambda x: (x["date"], x["je_id"]))

    # The running balance continues from what the account already held. Starting
    # a date-filtered view at zero would report a balance that ignores every
    # prior period and never ties to the general ledger's closing figure.
    running = opening
    for line in lines:
        d, c = Decimal(str(line["debit"])), Decimal(str(line["credit"]))
        running += _signed(d, c)
        line["balance"] = float(running)

    base = await _base_currency(session, company_id)
    return {
        "account_code": account_code,
        "account_name": account.name if account else account_code,
        "account_type": account_type,
        # The sign convention is decided here, once; consumers (UI, CSV) must
        # use this flag rather than re-deriving it from account_type.
        "debit_normal": debit_normal,
        "contact_id": contact_id,
        "date_from": date_from,
        "date_to": date_to,
        "opening_balance": to_stored_float(round_money(opening, base)),
        "lines": lines,
        "closing_balance": to_stored_float(round_money(running, base)),
    }


@router.get("/trial-balance")
async def trial_balance(
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Trial balance: one row per account with debit/credit totals.

    Reads posted journal_entry projections. Each journal entry stores
    entries: [{account, debit?, credit?}] in its state.
    """
    _require_date_range(date_from, date_to)
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
    contact_id: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """General ledger summary: opening balance, period debits/credits, and closing
    balance per account. Balances are signed by the account's normal side,
    matching the per-account ledger's running balance. Detail rows live at
    /ledger/{code}.

    contact_id narrows every account to one party, the same filter and the same
    party resolution the account ledger drills down with, so the report and the
    drilldown always show the same figure. Lines with no resolvable party are
    reported under the empty string, so the per-party views plus that one add
    back up to the unfiltered report.
    """
    _require_date_range(date_from, date_to)
    await _require_contact_filter(session, company_id, contact_id)
    posted = await _je_rows(session, company_id)
    # The party filter resolves every entry before the scan; unfiltered runs
    # never pay for the doc lookup.
    party_refs = (
        await _je_doc_refs(session, company_id, [je_id for je_id, _, _ in posted])
        if contact_id is not None else {}
    )
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
            if contact_id is not None and _line_party(party_refs, je_id, entry) != contact_id:
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
        debit_normal = _is_debit_normal(account_type)
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
        "contact_id": contact_id,
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
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Profit and Loss statement for the period.

    Revenue accounts (4xxx) = credit-normal -> positive net credit = revenue.
    COGS accounts (5xxx) = debit-normal -> positive net debit = cost.
    Expense accounts (6xxx) = debit-normal -> positive net debit = expense.
    """
    _require_date_range(date_from, date_to)
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
    _: None = require_permission("view_financial_reports"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Balance sheet as of a given date (default: all posted entries to date)."""
    _require_iso_date(as_of, "as_of")
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


# The control accounts a party's balance lives on: receivable and payable. A
# statement is these two accounts filtered to one party, which is what makes the
# statements sum back to the balance sheet.
_AR_CODE = "1120"
_AP_CODE = "2110"
_CONTROL_CODES = (_AR_CODE, _AP_CODE)


@router.get("/soa/{contact_id}")
async def statement_of_account(
    contact_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Statement of account for one contact: opening balance, dated rows with a
    running balance, closing balance.

    The statement is the receivable and payable control accounts filtered to one
    party, so every customer's statement plus the unattributed bucket sums back
    to the control account and the balance sheet, by construction rather than by
    coincidence. Reading the ledger is also what puts a manual adjustment,
    write-off or opening conversion on the party's statement: anything posted to
    a control account for them shows up, whether a document caused it or a person
    typed it.

    Amounts are the posted figures, which are stored in base currency at each
    entry's own date, so a mixed-currency party reads on one statement with no
    rate applied here. Only posted entries count; drafts and voids never post.

    The sign convention is the party's net position, debits positive: what a
    customer owes reads positive, what the company owes a supplier reads
    negative, and a party that is both nets to one figure.
    """
    _require_date_range(date_from, date_to)
    contact_row = await _require_contact(session, company_id, contact_id)
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

    base = await _base_currency(session, company_id)
    posted = await _je_rows(session, company_id)
    refs = await _je_doc_refs(session, company_id, [je_id for je_id, _, _ in posted])

    # (date, je_id, seq, line) so same-day entries keep the order they were
    # posted in and a replay reads the same way twice.
    events: list[tuple[str, str, int, dict]] = []
    for je_id, state, ts in posted:
        ref = refs.get(je_id) or {}
        for seq, entry in enumerate(state.get("entries", [])):
            if entry.get("account") not in _CONTROL_CODES:
                continue
            if _line_party(refs, je_id, entry) != contact_id:
                continue
            amounts = _line_amounts(entry)
            if amounts is None:
                continue
            events.append((ts, je_id, seq, {
                "date": ts,
                "je_id": je_id,
                "doc_id": ref.get("doc_id"),
                "doc_ref": ref.get("doc_ref") or state.get("memo", ""),
                "kind": _statement_kind(ref),
                "debit": to_stored_float(amounts[0]),
                "credit": to_stored_float(amounts[1]),
                "_net": amounts[0] - amounts[1],
            }))

    # Ascending: a statement's running balance reads down the page.
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    opening = Decimal(0)
    in_range = []
    for date, _je_id, _seq, line in events:
        # Dateless entries count as pre-period, exactly as every other report
        # treats them, so a statement can never disagree with the ledger it is
        # a slice of.
        if date_from and (not date or date < date_from):
            opening += line["_net"]
            continue
        if date and date_to and date > date_to:
            continue
        in_range.append(line)

    running = opening
    rows_out = []
    for line in in_range:
        running += line.pop("_net")
        line["balance"] = to_stored_float(round_money(running, base))
        rows_out.append(line)

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


def _opening_je_id(bank_id: uuid.UUID) -> str:
    """The journal entry that carries a bank account's opening balance.

    Written when the account is created and read by the health check below, from
    this one place, so the two can never disagree about which entry backs which
    opening balance.
    """
    return f"je:opening:{bank_id}"


async def _bank_dicts(
    session: AsyncSession, company_id: uuid.UUID, banks: list[BankAccount],
) -> list[dict]:
    """Bank accounts with their balance and the health of their opening balance.

    The balance is the ledger's, and only the ledger's. Creating an account with
    an opening balance posts that balance as a journal entry, so adding the column
    on top of the entries would count it twice. An opening balance with no entry
    behind it is reported as `opening_unbacked` rather than folded back into the
    figure, which would put the bank screen back out of step with the books.
    """
    rows = await _je_rows(session, company_id)
    posted_ids = {je_id for je_id, _state, _ts in rows}
    out = []
    for b in banks:
        net = Decimal(0)
        for _je_id, state, _ts in rows:
            for entry in state.get("entries", []):
                if entry.get("account") == b.chart_account_code:
                    amounts = _line_amounts(entry)
                    if amounts is None:
                        continue
                    net += amounts[0] - amounts[1]
        d = _bank_to_dict(b)
        d["balance"] = float(net)
        d["opening_unbacked"] = bool(b.opening_balance) and _opening_je_id(b.id) not in posted_ids
        out.append(d)
    return out


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
    _: None = require_permission("manage_accounting"),
) -> dict:
    q = select(BankAccount).where(BankAccount.company_id == company_id)
    if not include_inactive:
        q = q.where(BankAccount.is_active.is_(True))
    q = q.order_by(BankAccount.created_at)
    rows = (await session.execute(q)).scalars().all()
    items = await _bank_dicts(session, company_id, list(rows))
    return {"items": items, "total": len(items)}


@router.get("/bank-accounts/{bank_id}")
async def get_bank_account(
    bank_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("manage_accounting"),
) -> dict:
    b = (
        await session.execute(
            select(BankAccount).where(BankAccount.id == bank_id, BankAccount.company_id == company_id)
        )
    ).scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Bank account not found")
    return (await _bank_dicts(session, company_id, [b]))[0]


@router.post("/bank-accounts")
async def create_bank_account(
    payload: BankAccountCreate,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("manage_accounting"),
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
        je_id = _opening_je_id(bank.id)
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
    return (await _bank_dicts(session, company_id, [bank]))[0]


@router.patch("/bank-accounts/{bank_id}")
async def patch_bank_account(
    bank_id: uuid.UUID,
    payload: BankAccountPatch,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("manage_accounting"),
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
    return (await _bank_dicts(session, company_id, [b]))[0]


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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    # A bank line settled straight to a control account belongs to a party, and
    # this is the only chance to say which: nothing downstream can infer it.
    contact: str | None = None


class StmtLineSplitPayload(BaseModel):
    splits: list[dict]  # [{account_code, amount, memo, contact}]


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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _m: None = require_permission("manage_accounting"),
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

    # The bank side has no party by nature; the offset side carries the one the
    # operator named, so a bank line settled to a control account lands on that
    # party's statement instead of in the unattributed bucket.
    offset = {"account": payload.account_code, "debit": other_debit, "credit": other_credit}
    if payload.contact:
        offset["contact"] = payload.contact
    entries = [
        {"account": bank.chart_account_code, "debit": bank_debit, "credit": bank_credit},
        offset,
    ]
    await _check_line_contacts(db, company_id, entries)

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
    _m: None = require_permission("manage_accounting"),
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
        split = {
            "account": s["account_code"],
            "debit": amt if sl.amount < 0 else 0.0,
            "credit": amt if sl.amount >= 0 else 0.0,
        }
        if s.get("contact"):
            split["contact"] = s["contact"]
        entries.append(split)
    await _check_line_contacts(db, company_id, entries)

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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _m: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
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


# ---------------------------------------------------------------------------
# Cash flow statement
# ---------------------------------------------------------------------------

_CASH_PARENT = "1110"  # Cash and Cash Equivalents; bank accounts are seeded beneath it
_NON_CURRENT_ASSET_FLOOR = 1200
_NON_CURRENT_LIABILITY_FLOOR = 2200


def _code_number(code: str) -> int:
    """Leading digits of an account code, for range tests. Codes carry suffixes
    ("1130-P"), so the numeric part is read rather than the whole string."""
    m = re.match(r"\d+", code or "")
    return int(m.group()) if m else 0


def _derived_cash_flow_category(account_type: str, code: str) -> str:
    """Default classification for the account on the far side of a cash movement.

    Working capital and trading accounts are operating; long-lived assets are
    investing; borrowings and owner capital are financing. Correct for the seeded
    chart, and overridable per account where a company's own chart differs.
    """
    if account_type in ("revenue", "expense", "cogs"):
        return "operating"
    num = _code_number(code)
    if account_type == "asset":
        return "investing" if num >= _NON_CURRENT_ASSET_FLOOR else "operating"
    if account_type == "liability":
        return "financing" if num >= _NON_CURRENT_LIABILITY_FLOOR else "operating"
    if account_type == "equity":
        return "financing"
    return "operating"


def _cash_flow_category(acc: Account | None, code: str) -> str:
    """The account's own classification where it carries one, the derived default
    where it does not."""
    if acc is not None and acc.cash_flow_category in CASH_FLOW_CATEGORIES:
        return acc.cash_flow_category
    return _derived_cash_flow_category(acc.account_type if acc else "asset", code)


def _split_cash_movement(
    cash_delta: Decimal, contras: list[tuple[str, Decimal]], base: str,
) -> list[tuple[str, Decimal]]:
    """Apportion one entry's cash movement across the accounts it moved against.

    An entry may touch cash once and several other accounts at once (one payment
    settling several invoices, a bill paid net of a discount), so the movement is
    split in proportion to each contra leg. The rounding residual lands on the
    largest leg, ties broken by account code, so the same entry always splits the
    same way on replay.
    """
    total = sum((abs(v) for _, v in contras), Decimal(0))
    if not contras or total == 0:
        return []
    out: list[tuple[str, Decimal]] = []
    for code, val in contras:
        out.append((code, round_money(cash_delta * (abs(val) / total), base)))
    residual = cash_delta - sum((v for _, v in out), Decimal(0))
    if residual != 0:
        biggest = max(range(len(out)), key=lambda i: (abs(contras[i][1]), contras[i][0]))
        out[biggest] = (out[biggest][0], out[biggest][1] + residual)
    return out


@router.get("/cash-flow")
async def cash_flow(
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cash flow statement, direct and indirect, over the same posted entries.

    Direct reads every entry that moved cash and sorts the movement by what it
    moved against. Indirect starts from the period's profit and adjusts for the
    movement in every other non-cash account.

    The two agree by construction rather than by tolerance: a balanced entry set
    means the change in cash is exactly the negative of the change in everything
    else, which is what the indirect side computes. There is no exchange-rate
    reconciling item because every posting is stored in base currency at its own
    date, so a cash balance is never restated after the fact.

    Cash comes from the ledger. A bank account holding an opening balance that no
    journal entry carries is listed in `unbacked_bank_openings`, so the gap between
    that account and the books is reported rather than closed behind the reader's
    back with a figure the ledger does not support.
    """
    _require_date_range(date_from, date_to)

    accounts = (
        await session.execute(select(Account).where(Account.company_id == company_id))
    ).scalars().all()
    account_map = {a.code: a for a in accounts}

    banks = (
        await session.execute(select(BankAccount).where(BankAccount.company_id == company_id))
    ).scalars().all()
    cash_codes = {b.chart_account_code for b in banks}
    cash_codes |= {a.code for a in accounts
                   if a.code == _CASH_PARENT or a.parent_code == _CASH_PARENT}

    base = await _base_currency(session, company_id)
    posted = await _je_rows(session, company_id)

    opening_cash = Decimal(0)
    by_category: dict[str, dict[str, Decimal]] = {c: {} for c in ("operating", "investing", "financing")}
    non_cash_movement: dict[str, Decimal] = {}
    profit_movement = Decimal(0)
    period_cash = Decimal(0)

    for _je_id, state, ts in posted:
        cash_delta = Decimal(0)
        contras: list[tuple[str, Decimal]] = []
        for entry in state.get("entries", []):
            code = entry.get("account")
            amounts = _line_amounts(entry)
            if not code or amounts is None:
                continue
            signed = amounts[0] - amounts[1]
            if code in cash_codes:
                cash_delta += signed
            else:
                contras.append((code, signed))
        if cash_delta == 0 and not contras:
            continue
        # Dateless entries count as pre-period, matching every other report.
        if date_from and (not ts or ts < date_from):
            opening_cash += cash_delta
            continue
        if ts and date_to and ts > date_to:
            continue
        period_cash += cash_delta
        if cash_delta != 0:
            for code, share in _split_cash_movement(cash_delta, sorted(contras), base):
                acc = account_map.get(code)
                cat = _cash_flow_category(acc, code)
                by_category[cat][code] = by_category[cat].get(code, Decimal(0)) + share
        for code, signed in contras:
            acc = account_map.get(code)
            atype = acc.account_type if acc else "unknown"
            if atype in ("revenue", "expense", "cogs"):
                profit_movement += signed
            else:
                non_cash_movement[code] = non_cash_movement.get(code, Decimal(0)) + signed

    def _lines(cat: str) -> list[dict]:
        # Sorted by code: stable across replays, unlike dict encounter order.
        return [
            {
                "code": code,
                "name": account_map[code].name if code in account_map else code,
                "amount": to_stored_float(round_money(amount, base)),
            }
            for code, amount in sorted(by_category[cat].items())
            if amount != 0
        ]

    direct_sections = {}
    direct_total = Decimal(0)
    for cat in ("operating", "investing", "financing"):
        lines = _lines(cat)
        subtotal = sum((to_decimal(l["amount"]) for l in lines), Decimal(0))
        direct_total += subtotal
        direct_sections[cat] = {"lines": lines, "total": to_stored_float(round_money(subtotal, base))}

    # Revenue and expenses are credit-normal and debit-normal respectively, so the
    # summed debit-credit movement across them is the negative of the profit.
    net_profit = -profit_movement
    adjustments = [
        {
            "code": code,
            "name": account_map[code].name if code in account_map else code,
            # Cash moves opposite to a non-cash balance: stock bought (a debit)
            # consumes cash, a supplier balance taken on (a credit) preserves it.
            "amount": to_stored_float(round_money(-amount, base)),
        }
        for code, amount in sorted(non_cash_movement.items())
        if amount != 0
    ]
    indirect_total = net_profit - sum(non_cash_movement.values(), Decimal(0))

    closing_cash = opening_cash + period_cash
    posted_ids = {je_id for je_id, _s, _t in posted}
    unbacked = [
        {
            "bank_account_id": str(b.id),
            "bank_name": b.bank_name,
            "chart_account_code": b.chart_account_code,
            "opening_balance": to_stored_float(round_money(Decimal(str(b.opening_balance)), base)),
        }
        for b in banks
        if b.opening_balance and _opening_je_id(b.id) not in posted_ids
    ]
    return {
        "date_from": date_from,
        "date_to": date_to,
        "cash_accounts": sorted(cash_codes),
        # Named per bank account, not a bare count: fixing one means knowing which.
        "unbacked_bank_openings": sorted(unbacked, key=lambda u: u["chart_account_code"]),
        "opening_cash": to_stored_float(round_money(opening_cash, base)),
        "closing_cash": to_stored_float(round_money(closing_cash, base)),
        "net_change": to_stored_float(round_money(period_cash, base)),
        "direct": {
            **direct_sections,
            "total": to_stored_float(round_money(direct_total, base)),
        },
        "indirect": {
            "net_profit": to_stored_float(round_money(net_profit, base)),
            "adjustments": adjustments,
            "total": to_stored_float(round_money(indirect_total, base)),
        },
        # Exact, not approximate: a reconciling item here would mean an entry
        # whose sides do not sum to zero, which is a data fault worth surfacing.
        "balanced": (round_money(direct_total, base) == round_money(period_cash, base)
                     and round_money(indirect_total, base) == round_money(period_cash, base)),
    }


class PeriodLockPayload(BaseModel):
    lock_date: str | None  # ISO date or None to unlock


class CloseYearPayload(BaseModel):
    fiscal_year_end: str  # ISO date, e.g. "2025-12-31"


@router.get("/period-lock")
async def get_period_lock(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("manage_accounting"),
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
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    settings = dict(company.settings or {})
    if payload.lock_date:
        _require_iso_date(payload.lock_date, "lock")
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
    _: None = require_permission("manage_accounting"),
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

    # Emit the closing JE. Cycle-aware: re-closing the same year after an
    # unlock-edit cycle must post a NEW residual entry, not silently dedupe
    # into the first close's keys and report success while posting nothing.
    from celerp.events.engine import emit_event
    from celerp.models.ledger import LedgerEntry
    from celerp.services.je_keys import je_idempotency_key

    _cycle = sum(
        1 for k in (await session.execute(
            select(LedgerEntry.idempotency_key).where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.idempotency_key.like(f"je:{year_end}:fiscal.close%"),
            )
        )).scalars().all()
        if k.endswith(":c")
    )
    _cycle_tag = f":{_cycle}" if _cycle else ""
    je_id = f"je:close:{year_end}{_cycle_tag}"
    idem_create = je_idempotency_key(year_end, f"fiscal.close{_cycle_tag}", "c")
    idem_posted = je_idempotency_key(year_end, f"fiscal.close{_cycle_tag}", "p")

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
