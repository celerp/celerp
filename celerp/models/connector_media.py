# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The short-lived links a store fetches product images from (celerp.services.connector_media)."""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class ConnectorMediaToken(Base):
    """One image link: the stored image of one company it opens, until ``expires_at``.
    Only the token's sha256 is kept, so a database copy opens no image."""
    __tablename__ = "connector_media_tokens"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    company_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    mime: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, index=True)
