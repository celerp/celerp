# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class SessionRegistry(Base):
    """Active JTI registry - one row per issued (non-expired) access token."""

    __tablename__ = "session_registry"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserAuthState(Base):
    """Per-user auth state: nonce for token invalidation, eviction IP for UX."""

    __tablename__ = "user_auth_state"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    nonce: Mapped[str] = mapped_column(String(64))
    evicted_by_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
