# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Private payloads for in-flight subscription emails."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class SubscriptionEmailAttempt(Base):
    __tablename__ = "subscription_email_attempts"
    __table_args__ = (
        sa.UniqueConstraint("company_id", "doc_id", "attempt_number",
                            name="uq_subscription_email_document_attempt"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True)
    doc_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    attempt_number: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    payload_json: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=sa.func.now())
