# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Label template data model.

LabelTemplate stores reusable print layouts.
Each template defines:
  - format: paper size (e.g. "A4", "40x30mm", "custom")
  - orientation: portrait / landscape
  - width_mm / height_mm: explicit dimensions for "custom" format
  - fields: list of field specs [{key, label, x, y, fontSize, bold, type}]
            type ∈ {"text", "barcode", "qr"}
  - copies: default print quantity

Stored per-company via company_id foreign key.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class LabelTemplate(Base):
    """Reusable label print template."""

    __tablename__ = "label_templates"
    __table_args__ = (
        sa.Index("idx_label_template_company", "company_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("companies.id"), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    format: Mapped[str] = mapped_column(sa.String(50), nullable=False, default="40x30mm")
    orientation: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="portrait")
    width_mm: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    height_mm: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    fields: Mapped[list] = mapped_column(sa.JSON, nullable=False, default=list)
    copies: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def as_dict(self) -> dict:
        return {
            "id": str(self.id),
            "company_id": str(self.company_id),
            "name": self.name,
            "format": self.format,
            "orientation": self.orientation,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "fields": self.fields or [],
            "copies": self.copies,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
