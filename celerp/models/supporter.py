# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class SupporterBadge(Base):
    """A GitHub-star supporter badge claimed by an app user.

    The relay's founders registry is authoritative for anything public (the wall,
    the issue-label bot, support standing); this is the LOCAL copy used only to
    render the badge next to the user's name. One badge per user (the claimer).
    The stored `credential` (the relay-issued HS256 JWT) is kept so the user can
    opt out later via the relay.
    """

    __tablename__ = "supporter_badges"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    github_login: Mapped[str] = mapped_column(String(255), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False)  # founding | early | supporter
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    credential: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
