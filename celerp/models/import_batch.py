# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
# ImportBatch model — owned by celerp-inventory module.
# Canonical definition here; imported by celerp-inventory module package.

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class ImportBatch(Base):
    """One item import that created items, for Import History and Undo.

    A semantic import is written in bounded chunks; ``operation_key`` names the
    logical import so every chunk adds to this one row. It is unique per company
    and cleared on Undo, so the same source can later be imported as a new row.
    A raw event batch has no operation key and records one row per call.
    """

    __tablename__ = "import_batches"
    __table_args__ = (
        Index("idx_import_batch_company", "company_id"),
        Index("uq_import_batch_company_operation", "company_id", "operation_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_ids: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    idempotency_keys: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    operation_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    undone_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True)
