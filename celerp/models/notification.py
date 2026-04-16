# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Notification model - generic in-app notifications.

Supports company-wide (user_id=None) and per-user notifications.
Categories: ai, backup, system, subscription, import, connector.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id"), nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    action_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(
        String(16), default="medium", nullable=False,
    )  # "low" | "medium" | "high"
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
