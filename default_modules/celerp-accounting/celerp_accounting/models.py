# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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
    bank_type: Mapped[str] = mapped_column(String(32), nullable=False)  # checking|savings|credit_card
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    opening_balance: Mapped[float] = mapped_column(Numeric(20, 4), default=0.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class ReconciliationSession(Base):
    """A bank reconciliation session (point-in-time statement balance check)."""

    __tablename__ = "reconciliation_sessions"
    __table_args__ = (
        Index("idx_recon_bank_account", "bank_account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)
    statement_date: Mapped[str] = mapped_column(String(16), nullable=False)  # ISO date "YYYY-MM-DD"
    statement_balance: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)  # open|completed
    reconciled_je_ids: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    # CSV import metadata
    csv_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    csv_row_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    auto_matched_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    manual_matched_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    created_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    tolerance: Mapped[float] = mapped_column(Numeric(20, 4), default=1.00, nullable=False)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BankStatementLine(Base):
    """A single line from an imported bank statement CSV."""

    __tablename__ = "bank_statement_lines"
    __table_args__ = (
        Index("idx_stmt_line_recon", "reconciliation_id"),
        Index("idx_stmt_line_company", "company_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reconciliation_sessions.id"), nullable=False)
    line_date: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    raw_balance: Mapped[float | None] = mapped_column(Numeric(20, 4), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="unmatched", nullable=False)  # unmatched|matched|created|skipped
    matched_je_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attachment_ids: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    raw_csv_row: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class ReconciliationRule(Base):
    """Automation rule: auto-categorise matching bank statement lines."""

    __tablename__ = "reconciliation_rules"
    __table_args__ = (
        Index("idx_recon_rule_bank", "bank_account_id"),
        Index("idx_recon_rule_company", "company_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)
    match_field: Mapped[str] = mapped_column(String(32), nullable=False, default="description")
    match_pattern: Mapped[str] = mapped_column(String(256), nullable=False)
    match_type: Mapped[str] = mapped_column(String(16), nullable=False, default="contains")  # contains|exact|starts_with
    target_account_code: Mapped[str] = mapped_column(String(32), nullable=False)
    default_memo: Mapped[str | None] = mapped_column(String(256), nullable=True)
    default_tax: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    times_applied: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
