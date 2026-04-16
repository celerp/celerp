# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class MarketplaceConfig(Base):
    __tablename__ = "marketplace_configs"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_marketplace_company_name"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
