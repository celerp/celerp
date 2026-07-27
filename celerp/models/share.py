# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
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
    # Auto-revoke: the link stops resolving after this instant. NULL = no expiry.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Manual revoke: set = link is off. The row (and its token) persists so a
    # document's share URL is stable - sharing again reactivates the same link.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def is_active(row: DocShareToken, now: datetime | None = None) -> bool:
    """A link resolves while it is not revoked and not past its expiry instant.
    Single source of the 'active' rule (row form); the SQL form is active_filter()."""
    now = now or datetime.now(timezone.utc)
    if row.revoked_at is not None:
        return False
    return row.expires_at is None or row.expires_at > now


def active_filter(now: datetime | None = None):
    """SQLAlchemy predicate matching the same rule as is_active(), for existence
    queries (has_active_share) instead of a loaded row."""
    now = now or datetime.now(timezone.utc)
    return sa.and_(
        DocShareToken.revoked_at.is_(None),
        sa.or_(DocShareToken.expires_at.is_(None), DocShareToken.expires_at > now),
    )
