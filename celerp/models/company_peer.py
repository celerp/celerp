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
    # When the connected company's installation last reached this one with the key.
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

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


class CompanyPeerAttention(Base):
    """One record a connected company sent that this company did not apply, with the
    reason, until that record is applied. The firm view lists them per company."""
    __tablename__ = "company_peer_attention"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    source_company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    label: Mapped[str] = mapped_column(sa.Text, nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    fix: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        sa.UniqueConstraint("company_id", "source_company_id", "kind", "entity_type", "source_entity_id",
                            name="uq_company_peer_attention_record"),
        sa.Index("ix_company_peer_attention_company", "company_id"),
    )


class CompanyPeerDelivery(Base):
    """The last revision of one record this company sent to a connected company with one
    key, or one record that company sent here and this company declined, so neither is
    sent or applied again until the record changes. ``refused`` holds the reason when it
    was not accepted."""
    __tablename__ = "company_peer_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    grant_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    entity: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    revision: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    refused: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        sa.UniqueConstraint("company_id", "grant_id", "entity", "entity_id",
                            name="uq_company_peer_delivery_record"),
    )


class CompanyPeerAccount(Base):
    """One account of a connected company and the account of this company it posts to,
    linked by the other side's account id so a rename on either side changes nothing.
    `how` is copy (linked by the copy the firm opened from), match (same code, type,
    name and currency on the first record), chosen (picked by a user), or added (made
    by sync because this company had no such account)."""
    __tablename__ = "company_peer_accounts"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    source_company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    other_account_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    other_code: Mapped[str] = mapped_column(sa.Text, nullable=False)
    other_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    account_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    code: Mapped[str] = mapped_column(sa.Text, nullable=False)
    how: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    added_from: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        sa.UniqueConstraint("company_id", "source_company_id", "other_account_id",
                            name="uq_company_peer_account_link"),
        sa.Index("ix_company_peer_accounts_company", "company_id"),
    )
