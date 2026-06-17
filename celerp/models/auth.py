# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

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


class SystemRuntimeState(Base):
    """Singleton row: cluster-wide runtime flags (draining, worker count, etc.).

    Accessed via ``get_runtime_state()`` / ``set_draining()`` helpers.
    Single row, id=1.  ``value`` is a JSON dict - always replace the full dict
    when updating to avoid mutation-tracking issues.
    """

    __tablename__ = "system_runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
