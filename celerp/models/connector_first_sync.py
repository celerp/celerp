# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A connection's first sync: the reviewed plan a person started, and how far it got."""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base

# pending -> starting -> running -> completed | failed ("Needs attention").
FIRST_SYNC_STATES = ("pending", "starting", "running", "completed", "failed")


class ConnectorFirstSync(Base):
    """One row per connection. ``state`` is ``completed`` only once every resource in
    ``progress`` completed; that is the only record that the first sync succeeded.
    ``progress`` holds, per resource, its state, cursor and counts; ``undo`` what the
    sync linked and created."""
    __tablename__ = "connector_first_syncs"

    company_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    connector: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending", server_default="pending")
    started_by: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    watermark: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    directions: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    match: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    preview_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    # The store products the person reviewed: remote key -> the item it links to, None
    # for one it creates. The first sync acts on these only.
    reviewed: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    batch_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    progress: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict, server_default="{}")
    undo: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default=sa.false())
    next_attempt_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
    )

    __table_args__ = (
        # Compared as text so Postgres prints it back the same way after a restore.
        sa.CheckConstraint(
            "state::text IN ('pending', 'starting', 'running', 'completed', 'failed')",
            name="ck_connector_first_sync_state",
        ),
    )

    @property
    def links_holding_store_values(self) -> int:
        """The items it linked that still hold values it took from the store: a link
        whose undo has not run and that replaced at least one of the item's values."""
        links = (self.undo or {}).get("links") or []
        return sum(1 for link in links if "outcome" not in link and link.get("restore"))
