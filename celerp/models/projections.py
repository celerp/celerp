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
# Every character Python reads as a space (str.isspace()). A number is stored without them
# around it (strip_number), the only place this set is used: SQL never names it.
NUMBER_SPACE = "".join(c for c in map(chr, range(sys.maxunicode + 1)) if c.isspace())
# The ASCII spaces SQL trims, written as escapes so every statement and index definition is the
# same ASCII text in every server encoding and after a dump restored into any other.
_ASCII_SPACE = sa.literal_column(r"E' \t\n\r\f\x0b'", sa.Text)


def strip_number(number: str) -> str:
    """``number`` as a record stores it: without NUMBER_SPACE around it. Every writer of a
    number field stores it so, and the candidate a check reads is stripped the same, since it is
    the value that would be stored."""
    return number.strip(NUMBER_SPACE)


# The fields a Document or List holds a number in. Every write of one goes through
# require_number_free and stores the number it returns.
NUMBER_FIELDS = ("ref_id", "doc_number")


def strip_numbers(state: dict) -> bool:
    """Give each number ``state`` holds (NUMBER_FIELDS) as a record stores it (strip_number);
    True when one changed. The Document and List projection applies it to every record an event
    writes, so a number an earlier version saved padded in its history is stored stripped by
    every replay, and the upgrade's backfill stores what a replay does."""
    padded = {f: strip_number(v) for f in NUMBER_FIELDS
              if isinstance(v := state.get(f), str) and v != strip_number(v)}
    state.update(padded)
    return bool(padded)


def strip_key(key: str) -> str:
    """``key`` as a record is stored under it: the number it holds (key_number, the text after
    its first colon or the whole key) stripped (strip_number), so a key written now never
    carries spaces the database's trim does not reach."""
    head, colon, number = key.partition(":")
    return head + colon + strip_number(number) if colon else strip_number(key)


def canonical_number(number: sa.ColumnElement) -> sa.ColumnElement:
    """The canonical form of ``number``, a stored number or a bound candidate: lowercase as the
    database lowers it, without ASCII spaces around it. Numbering compares numbers only in this
    form, so one engine decides both sides of every comparison. Stored numbers and candidates
    are already stripped (strip_number), so the trim matters only for a storage key, which is
    never rewritten: one an earlier version saved padded with a space past ASCII is matched only
    once that space is gone. On a SQL_ASCII database lower() folds ASCII letters only, on both
    sides alike, so there an accented capital and its small letter are two numbers."""
    return sa.func.lower(sa.func.btrim(number, _ASCII_SPACE))


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
        # Built on PostgreSQL only, the database numbering runs on: btrim and strpos are its own.
        *(sa.Index(f"ix_projections_canonical_number_{name}", "company_id", number).ddl_if(dialect="postgresql")
          for name, number in (("shown", NUMBER_SHOWN), ("kept", NUMBER_KEPT), ("key", NUMBER_KEYED))),
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
