# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""ConnectorConfig + OutboundQueue - persistent state for connector sync."""
from __future__ import annotations

import json
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class ConnectorConfig(Base):
    """Per-connector, per-company sync configuration."""
    __tablename__ = "connector_configs"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    connector: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="both")
    sync_frequency: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="realtime")
    daily_sync_hour: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=2)
    webhook_ids_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    last_daily_sync_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("company_id", "connector", name="uq_connector_config"),
    )

    @property
    def webhook_ids(self) -> list[str]:
        if not self.webhook_ids_json:
            return []
        return json.loads(self.webhook_ids_json)

    @webhook_ids.setter
    def webhook_ids(self, ids: list[str]) -> None:
        self.webhook_ids_json = json.dumps(ids) if ids else None


class OutboundQueue(Base):
    """Queue for failed or pending outbound sync pushes."""
    __tablename__ = "outbound_queue"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    connector: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")
    retry_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    MAX_RETRIES = 5
    BACKOFF_MINUTES = [1, 5, 15, 60, 240]  # exponential-ish
