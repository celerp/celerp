# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

import sqlalchemy as sa
from pydantic import AfterValidator, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


def length(column) -> int:
    """The characters a text column holds: the one limit every request field and reader check
    of a value stored in it reads."""
    return column.expression.type.length


def _storable(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("has a character text cannot hold (a lone surrogate)") from None
    if "\x00" in value:
        raise ValueError("has a NUL character, which text cannot hold")
    return value


def stored_text(column, **field):
    """A request field stored in ``column``: at most the characters it holds and none a database
    cannot store (NUL, a lone surrogate); anything else is refused naming the field, before any write."""
    return Annotated[str, Field(max_length=length(column), **field), AfterValidator(_storable)]


class Account(Base):
    """Chart of accounts entry. Stored in DB (not as projections) — stable financial schema."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_account_company_code"),
        Index("idx_account_company", "company_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)  # asset|liability|equity|revenue|expense|cogs
    parent_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # operating|investing|financing for the cash flow statement. Null means "the
    # nearest parent account's section, else operating"; the seeded chart sets it on
    # the headers whose accounts are not operating.
    cash_flow_category: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # True when an importer made the code up (the source account had none): an internal
    # id, so users see the account by its name (accounting_roles.account_label).
    code_generated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa.false(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class BankAccount(Base):
    """Bank account linked to a chart-of-accounts entry (sub-account under 1110)."""

    __tablename__ = "bank_accounts"
    __table_args__ = (
        Index("idx_bank_account_company", "company_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    chart_account_code: Mapped[str] = mapped_column(String(32), nullable=False)  # account code in chart (e.g. "1111")
    bank_name: Mapped[str] = mapped_column(String(128), nullable=False)
    account_number: Mapped[str] = mapped_column(String(64), nullable=False)  # stored masked
    bank_type: Mapped[str] = mapped_column(String(32), nullable=False)  # checking|savings|credit_card|cash
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    # The label the books-currency correction replaced, so the correction can be undone
    # (set_bank_currency); None when the last change was not that correction.
    relabelled_from: Mapped[str | None] = mapped_column(String(8), nullable=True)
    opening_balance: Mapped[float] = mapped_column(Numeric(20, 4), default=0.0, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=sa.true())
    # The CSV or XLSX column layout its statements were last imported with, and the header row
    # it was confirmed on, so the next file with the same header reads without asking.
    statement_column_map: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class ReconciliationSession(Base):
    """One bank statement: its period, opening and closing balance, and the entries it cleared."""

    __tablename__ = "reconciliation_sessions"
    __table_args__ = (
        Index("idx_recon_bank_account", "bank_account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)
    statement_date: Mapped[str] = mapped_column(String(16), nullable=False)  # ISO date "YYYY-MM-DD"
    statement_balance: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    period_start: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ISO date
    opening_balance: Mapped[float | None] = mapped_column(Numeric(20, 4), nullable=True)
    source_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False, server_default="open")  # open|completed
    reconciled_je_ids: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False, server_default="[]")
    # CSV import metadata
    csv_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    csv_row_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False, server_default="0")
    manual_matched_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False, server_default="0")
    created_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False, server_default="0")
    tolerance: Mapped[float] = mapped_column(Numeric(20, 4), default=1.00, nullable=False, server_default="1.00")
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BankStatementLine(Base):
    """One line of an imported bank statement, identified on its bank by its fingerprint."""

    __tablename__ = "bank_statement_lines"
    __table_args__ = (
        Index("idx_stmt_line_recon", "reconciliation_id"),
        Index("idx_stmt_line_company", "company_id"),
        Index("uq_stmt_line_fingerprint", "company_id", "bank_account_id", "fingerprint", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reconciliation_sessions.id"), nullable=False)
    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(16), nullable=False)
    line_date: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    raw_balance: Mapped[float | None] = mapped_column(Numeric(20, 4), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The id the bank gave the line (OFX FITID), kept as read: the evidence a line read again is this one.
    fitid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="unmatched", nullable=False, server_default="unmatched")  # unmatched|matched|posted|skipped
    counterparty_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    counterparty_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The one suggestion store: {kind, je_ids, account_code, contact_id, target_bank_id,
    # paired_line_id, docs, source, reason, rule_id, evidence}; kind is match, post, transfer,
    # pay_doc, cash, suspense or no_entry. Written only by statement_lines.write_suggestion.
    suggestion: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    matched_je_ids: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False, server_default="[]")
    # The question asked of the client: {asked, note, answer, contact, files, answered, withdrawn}.
    client_query: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    attachment_ids: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False, server_default="[]")
    raw_csv_row: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class ReconciliationRule(Base):
    """A rule that suggests how matching statement lines settle; null bank means every bank."""

    __tablename__ = "reconciliation_rules"
    __table_args__ = (
        Index("idx_recon_rule_bank", "bank_account_id"),
        Index("idx_recon_rule_company", "company_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bank_accounts.id"), nullable=True)
    match_field: Mapped[str] = mapped_column(String(32), nullable=False, default="description", server_default="description")  # description|reference|counterparty_account|counterparty_key
    match_pattern: Mapped[str] = mapped_column(String(256), nullable=False)
    match_type: Mapped[str] = mapped_column(String(16), nullable=False, default="contains", server_default="contains")  # contains|exact|starts_with
    target_account_code: Mapped[str] = mapped_column(String(32), nullable=False)
    default_memo: Mapped[str | None] = mapped_column(String(256), nullable=True)
    match_amount: Mapped[float | None] = mapped_column(Numeric(20, 4), nullable=True)
    contact_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_bank_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bank_accounts.id"), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), default="user", nullable=False, server_default="user")  # user|answer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=sa.true())
    times_applied: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
