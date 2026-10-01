# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class PaymentClosure(Base):
    """A request to Celerp Cloud to close one company's online payments that has not
    been settled yet.

    Saved in its own transaction before Cloud is asked to freeze the company's payments,
    so it survives a crash at any later point. Once the reset that asked for it has
    committed or rolled back, the company is looked up: gone means Cloud is told to
    close the payments for good, still there means Cloud is told to reopen them. The
    row is removed once Cloud confirms either. Until then Cloud keeps the company's
    payments frozen. Not company data: it must outlive the company it names, so the
    company is a plain value, not a reference."""

    __tablename__ = "payment_closures"

    operation_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True)
    target_company: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
