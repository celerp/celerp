# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""One revocable company integration grant, independent of user sessions."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from celerp.models.base import Base


class CompanyPeerGrant(Base):
    __tablename__ = "company_peer_grants"
    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    source_company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    scopes: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Days a key lasts when issued, rotated or extended.
    lifetime_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=30)
    # The key that replaced this one; set when the sending company rotated it.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint("length(token_hash) = 64", name="ck_company_peer_grant_hash_length"),
    )


class CompanyPeerIdentity(Base):
    __tablename__ = "company_peer_identities"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    source_company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target_entity_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    last_revision: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    last_payload_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("company_id", "source_company_id", "entity_type", "source_key_hash",
                            name="uq_company_peer_external_identity"),
        sa.Index("ix_company_peer_identities_company", "company_id"),
    )
