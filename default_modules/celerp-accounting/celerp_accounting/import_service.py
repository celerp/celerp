# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Accounting writes shared by the HTTP routes and the migration sink.

The batch journal import and chart-of-accounts creation live here once; the
routes and the migration sink both call them. Nothing here commits: the caller
owns the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import refusal
from celerp.events.engine import emit_event
from celerp.importers.results import ImportOutcome
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.company_lock import lock_chart
from celerp_accounting.ledger_accounts import require_money_account
from celerp_accounting.models import Account, BankAccount
from ui.i18n import t
from celerp_accounting.chart_rules import (
    check_new_account,
    checked_account_code,
    checked_account_name,
    checked_account_type,
)

JOURNAL_CREATED = "acc.journal_entry.created"


class AccImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


async def contact_row(session: AsyncSession, company_id: uuid.UUID, contact_id: str) -> Projection | None:
    """The contact projection behind an id, or None when the id names no contact."""
    row = await session.get(Projection, (company_id, contact_id))
    return row if row and row.entity_type == "contact" else None


async def check_line_contacts(
    session: AsyncSession, company_id: uuid.UUID, entries: list[dict]
) -> None:
    """Refuse a set of journal entry lines if any names a contact that is not there.

    A line's contact is what puts a posting on that party's statement, so an id
    matching no contact would post an entry no statement can ever show and no
    control-account bucket can ever explain. Checked once per distinct contact
    named, and by the same rule for every path that writes lines: manual
    entries, reconciliation, the batch import and migration.
    """
    named = {e.get("contact") for e in entries if isinstance(e.get("contact"), str) and e.get("contact")}
    if not named:
        return
    missing = []
    for cid in sorted(named):
        if await contact_row(session, company_id, cid) is None:
            missing.append(cid)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=t("error.contacts_not_found", names=", ".join(missing)),
        )


async def import_journal_records(
    session: AsyncSession,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    records: Sequence[AccImportRecord],
) -> ImportOutcome:
    """Create imported journal entries once per per-company idempotency key and entity."""
    outcome = ImportOutcome()
    keys = [r.idempotency_key for r in records]
    existing_keys = set((await session.execute(
        select(LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.idempotency_key.in_(keys),
        )
    )).scalars().all())

    create_entity_ids = [r.entity_id for r in records if r.event_type == JOURNAL_CREATED]
    existing_entities: set[str] = set()
    if create_entity_ids:
        existing_entities = set((await session.execute(
            select(Projection.entity_id).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(create_entity_ids),
            )
        )).scalars().all())

    for rec in records:
        if rec.event_type != JOURNAL_CREATED:
            outcome.add(rec.entity_id, "rejected", f"{rec.entity_id}: event type {rec.event_type!r} is not import-safe")
            continue
        if rec.idempotency_key in existing_keys or rec.entity_id in existing_entities:
            outcome.add(rec.entity_id, "skipped")
            continue
        try:
            # An imported line may name a party. Checked here, at the boundary,
            # because an entry whose contact resolves to nothing would post to a
            # control account and then be missing from every statement, with
            # nothing on screen to say why.
            entries = rec.data.get("entries") if isinstance(rec.data, dict) else None
            if isinstance(entries, list):
                await check_line_contacts(session, company_id, [e for e in entries if isinstance(e, dict)])
            entry = await emit_event(
                session,
                company_id=company_id,
                entity_id=rec.entity_id,
                entity_type="journal_entry",
                event_type=rec.event_type,
                data=rec.data,
                actor_id=actor_id,
                location_id=None,
                source=rec.source,
                idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing_keys.add(rec.idempotency_key)
            existing_entities.add(rec.entity_id)
            # A concurrent import of the same file can write the row first.
            outcome.add(rec.entity_id, "skipped" if getattr(entry, "was_deduped", False) else "created")
        except Exception as exc:
            outcome.add(rec.entity_id, "failed", f"{rec.entity_id}: {exc}")
    return outcome


async def create_chart_account(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    code: str,
    name: str,
    account_type: str,
    parent_code: str | None,
    cash_flow_category: str | None = None,
    is_active: bool = True,
    code_generated: bool = False,
) -> Account:
    """Add one chart-of-accounts row; an account code already in use is refused, and
    so is a parent the new account cannot sit under. ``code_generated`` marks a code an
    importer made up because the source account had none."""
    # Under the chart lock, so a second add of the same code waits for the first and
    # then sees it, instead of failing on the unique index.
    await lock_chart(session, company_id)
    existing = (await session.execute(
        select(Account.id).where(Account.company_id == company_id, Account.code == code)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=refusal(
            "chart.code_exists", f"Account code {code} is already used. Choose a different code.", code=code))
    await check_new_account(session, company_id, account_type=account_type, parent_code=parent_code)
    acc = Account(
        id=uuid.uuid4(),
        company_id=company_id,
        code=code,
        name=name,
        account_type=account_type,
        parent_code=parent_code,
        cash_flow_category=cash_flow_category,
        is_active=is_active,
        code_generated=code_generated,
    )
    session.add(acc)
    return acc


async def add_posting_account(
    session: AsyncSession, company_id: uuid.UUID, *, code: str, name: str, account_type: str,
) -> None:
    """A top-level account added through ``ChartAccess.add_account``, checked as an
    account added in Settings is."""
    await create_chart_account(session, company_id, code=checked_account_code(code),
                               name=checked_account_name(name),
                               account_type=checked_account_type(account_type), parent_code=None)
    await session.flush()


async def next_bank_account_code(session: AsyncSession, company_id: uuid.UUID, parent_code: str | None) -> str:
    """The first free code numbered beneath ``parent_code``, with no limit on how many:
    1111 to 1119 under 1110; 1015-1 to 1015-9 under a code not ending in 0; BANK-1 to
    BANK-9 for a bank with no parent account. After the ninth come 1119-0010,
    1119-0011, ... (likewise 1015-9-0010, BANK-9-0010): the code keeps its parent's
    stem, so range tests still read it, and the zero-padded suffix keeps every code
    sorting in the order the accounts were added. Past 9,999 the codes stay unique
    and valid but no longer sort in that order."""
    if not parent_code:
        stem = "BANK-"
    elif parent_code.isdigit() and parent_code.endswith("0"):
        stem = parent_code[:-1]
    else:
        stem = f"{parent_code}-"
    used = set((await session.execute(
        select(Account.code).where(Account.company_id == company_id, Account.code.like(f"{stem}%"))
    )).scalars().all())
    n = 1
    while _bank_code(stem, n) in used:
        n += 1
    return _bank_code(stem, n)


def _bank_code(stem: str, n: int) -> str:
    """The *n*th automatic bank code beneath ``stem`` (see next_bank_account_code)."""
    return f"{stem}{n}" if n < 10 else f"{stem}9-{n:04d}"


async def add_bank_account(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    code: str,
    parent_code: str | None,
    account_name: str,
    bank_name: str,
    account_number: str,
    bank_type: str,
    currency: str,
    opening_balance: float,
) -> BankAccount:
    """A bank account and, when its chart code is new, its chart row under ``parent_code``.
    An existing chart code must be an active asset account."""
    existing_acc = (await session.execute(
        select(Account.id).where(Account.company_id == company_id, Account.code == code)
    )).scalar_one_or_none()
    if existing_acc:
        await require_money_account(session, company_id, code)
    else:
        await create_chart_account(
            session, company_id, code=code, name=account_name, account_type="asset", parent_code=parent_code,
        )
    bank = BankAccount(
        id=uuid.uuid4(),
        company_id=company_id,
        chart_account_code=code,
        bank_name=bank_name,
        account_number=account_number,
        bank_type=bank_type,
        currency=currency,
        opening_balance=opening_balance,
    )
    session.add(bank)
    return bank
