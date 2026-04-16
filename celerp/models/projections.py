# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, PrimaryKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class Projection(Base):
    __tablename__ = "projections"
    __table_args__ = (PrimaryKeyConstraint("company_id", "entity_id"),)

    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    location_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), ForeignKey("locations.id"), nullable=True)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    is_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_on_memo: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_on_marketplace: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_in_production: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_expired: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consignment_flag: Mapped[str | None] = mapped_column(String(16), nullable=True)
