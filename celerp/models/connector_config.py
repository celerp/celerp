# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""ConnectorConfig + OutboundQueue - persistent state for connector sync."""
from __future__ import annotations

import json
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from celerp.models.base import Base


def _default_directions(context) -> dict[str, str]:
    from celerp.connectors.registry import get

    try:
        return dict(get(context.get_current_parameters()["connector"]).default_directions)
    except KeyError:
        return {}


class ConnectorConfig(Base):
    """Per-connector, per-company sync configuration.

    ``entity_directions`` sets, for each kind of record the connector carries, which
    way it syncs (see ConnectorBase.entity_flows). ``first_sync`` is the connection's
    first-sync record (ConnectorFirstSync), loaded with it; nothing runs on a schedule,
    and webhooks deliver nothing, until it completed. ``orders_from`` is the earliest
    order date the connection imports.
    ``connect_lapsed_at`` is set while Celerp Connect has ended and syncing is paused.
    ``webhooks_dirty`` is set while the store's webhooks may not match the directions;
    ``webhooks_version`` changes with every change that asks for them to be reconciled,
    so a reconcile that read an older configuration never clears the mark."""
    __tablename__ = "connector_configs"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    connector: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    entity_directions: Mapped[dict] = mapped_column(
        sa.JSON, nullable=False, default=_default_directions
    )
    sync_frequency: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="realtime", server_default="realtime")
    daily_sync_hour: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=2, server_default="2")
    webhook_ids_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    last_daily_sync_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    orders_from: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    connect_lapsed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    webhooks_dirty: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default=sa.false())
    webhooks_version: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default="0")

    first_sync = relationship(
        "ConnectorFirstSync",
        primaryjoin="and_(foreign(ConnectorConfig.company_id) == ConnectorFirstSync.company_id, "
                    "foreign(ConnectorConfig.connector) == ConnectorFirstSync.connector)",
        viewonly=True, uselist=False, lazy="selectin",
    )

    __table_args__ = (
        sa.UniqueConstraint("company_id", "connector", name="uq_connector_config"),
    )

    @property
    def first_sync_state(self) -> str | None:
        """The first sync's state (FIRST_SYNC_STATES), None when it has no record."""
        return self.first_sync.state if self.first_sync is not None else None

    @property
    def links_holding_store_values(self) -> int:
        """Items the first sync linked that still hold values it took from the store."""
        return self.first_sync.links_holding_store_values if self.first_sync is not None else 0

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
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending", server_default="pending")
    retry_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
