# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""ConnectorSource - the store a company's imported connector records came from."""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class ConnectorSource(Base):
    """One row per company and connector. Written only by the connector layer
    once the store is confirmed, and kept across disconnects so a later
    connection is checked against the store the records came from."""
    __tablename__ = "connector_sources"

    company_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    connector: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    store_handle: Mapped[str] = mapped_column(sa.Text, nullable=False)
    bound_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
