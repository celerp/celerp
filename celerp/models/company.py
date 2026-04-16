# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    settings: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_location_company_name"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    address: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("company_id", "email", name="uq_user_company_email"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(64), default="user", nullable=False)
    auth_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    # Password reset token (short-lived, 15min TTL)
    reset_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reset_token_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
