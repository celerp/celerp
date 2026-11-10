# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import sys
import uuid

import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, PrimaryKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


# The asset a projection belongs to (an asset's movements carry it), as the index on it
# reads it: a query that filters on this expression uses ix_projections_asset.
ASSET_ID = sa.literal_column("(projections.state ->> 'asset_id')")
# Every character Python reads as a space (str.isspace()). A writer stores a number without
# them around it and numbering compares numbers without them, so both trim this one set.
NUMBER_SPACE = "".join(c for c in map(chr, range(sys.maxunicode + 1)) if c.isspace())


def canonical_number(number: sa.ColumnElement) -> sa.ColumnElement:
    """The canonical form of ``number``, a stored number or a bound candidate: lowercase as the
    database lowers it, without NUMBER_SPACE around it. Numbering compares numbers only in this
    form, so one engine decides both sides of every comparison."""
    return sa.func.lower(sa.func.btrim(number, sa.literal_column(f"'{NUMBER_SPACE}'", sa.Text)))


def key_number(key: sa.ColumnElement) -> sa.ColumnElement:
    """The number the storage key ``key`` holds: the text after its first colon, or the whole key
    when it has none. The writer that checks a key and every reader parse it here."""
    return sa.func.substr(key, sa.func.strpos(key, sa.literal_column("':'")) + sa.literal_column("1"))


KEY_NUMBER = key_number(sa.literal_column("projections.entity_id", sa.Text))
# The numbers a Document or List shows, keeps (an imported one's own number) and is stored
# under, in their canonical form, as the ix_projections_canonical_number_* indexes read them.
NUMBER_SHOWN = canonical_number(sa.literal_column("projections.state ->> 'ref_id'", sa.Text))
NUMBER_KEPT = canonical_number(sa.literal_column("projections.state ->> 'doc_number'", sa.Text))
NUMBER_KEYED = canonical_number(KEY_NUMBER)


class Projection(Base):
    __tablename__ = "projections"
    __table_args__ = (
        PrimaryKeyConstraint("company_id", "entity_id"),
        sa.Index("ix_projections_asset", "company_id", "entity_type", sa.text("(state ->> 'asset_id')")),
        sa.Index("ix_projections_canonical_number_shown", "company_id", NUMBER_SHOWN),
        sa.Index("ix_projections_canonical_number_kept", "company_id", NUMBER_KEPT),
        sa.Index("ix_projections_canonical_number_key", "company_id", NUMBER_KEYED),
    )

    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    location_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), ForeignKey("locations.id"), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    is_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_on_memo: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_on_marketplace: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_sync_to_shopify: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_in_production: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_expired: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consignment_flag: Mapped[str | None] = mapped_column(String(16), nullable=True)
