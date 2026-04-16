# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class LedgerEntry(Base):
    __tablename__ = "ledger"
    __table_args__ = (
        Index("idx_ledger_company_entity", "company_id", "entity_id"),
        Index("idx_ledger_company_event_type", "company_id", "event_type"),
        Index("idx_ledger_company_ts", "company_id", "ts"),
        Index("idx_ledger_entity_type", "entity_type"),
        Index("idx_ledger_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True)
    location_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), ForeignKey("locations.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", sa.JSON, nullable=True)
    ts: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
