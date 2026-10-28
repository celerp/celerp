# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The lease that makes one execution at a time the writer of a connection's resource.

A lease row names the holder and a generation that changes with every new holder; every
transaction of a running execution checks it (celerp.connectors.execution). The holder's
heartbeat lives in its own row so renewing it never waits for the holder's own writes."""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class ConnectorExecutionLease(Base):
    """Who holds one resource of a connection, and which generation of holder it is."""
    __tablename__ = "connector_execution_leases"

    company_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    connector: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    resource: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    generation: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default="0")
    holder: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)


class ConnectorExecutionHeartbeat(Base):
    """Until when the holder's lease is valid, and the reruns asked for while it runs."""
    __tablename__ = "connector_execution_heartbeats"

    company_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    connector: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    resource: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    holder: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    rerun_requested: Mapped[list] = mapped_column(sa.JSON, nullable=False, default=list, server_default="[]")
