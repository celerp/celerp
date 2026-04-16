# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
# DocShareToken model — owned by celerp-docs module.
# Canonical definition here; imported by celerp-docs module package.

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class DocShareToken(Base):
    """Public share link for a document. One active token per document."""

    __tablename__ = "doc_share_tokens"
    __table_args__ = (Index("ix_doc_share_token", "token", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
