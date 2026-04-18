# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""SyncRun - audit trail for connector sync operations."""
from __future__ import annotations

import json
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class SyncRun(Base):
    """Records each connector sync execution for audit and UI display."""
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    connector: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    entity: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    errors_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # success | partial | failed

    @property
    def errors(self) -> list[str]:
        if not self.errors_json:
            return []
        return json.loads(self.errors_json)
