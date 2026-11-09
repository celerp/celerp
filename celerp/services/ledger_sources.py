# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Which ledger entries belong to a record (a Document or a List).

One rule, for every reader and writer: an entry belongs to the record its source keys name
exactly, as written in its ledger metadata. An entry id is never split or prefix-matched to
decide it: a number is free text, so one record's number can begin with another's.
"""
from __future__ import annotations

import re

from sqlalchemy import or_, select

SOURCE_KEYS = ("doc_id", "cn_id", "dn_id", "from_doc_id", "list_id")


# A write-off's cost corrections recorded by 2.8.0 named their scope as the List: the List id,
# ":cost:", the 24-hex restatement identity and the lot, under entry id
# je:auto:{scope}:writeoff_cost:0. The ledger is append-only, so those entries are read
# back to the List their scope began with (source_meta) rather than rewritten; the List is all
# before the last such marker, so a List whose own number holds ":cost:" keeps it. Nothing else
# writes that shape: a correction now names its List exactly and keeps its scope in the entry id.
_LEGACY_COST_SCOPE = re.compile(r"(?P<list_id>.+):cost:[0-9a-f]{24}:.+")


def source_meta(entity_id: str, meta: dict) -> dict:
    """An entry's ledger metadata with each source key naming its record: the source keys as
    written, except a 2.8.0 write-off cost correction, whose list_id is read back to its List."""
    list_id = meta.get("list_id")
    if (isinstance(list_id, str) and entity_id == f"je:auto:{list_id}:writeoff_cost:0"
            and (legacy := _LEGACY_COST_SCOPE.fullmatch(list_id))):
        return {**meta, "list_id": legacy["list_id"]}
    return meta


def names_record(entity_id: str, meta: dict, record_id: str) -> bool:
    """Whether a ledger entry's source keys (source_meta) name ``record_id``."""
    source = source_meta(entity_id, meta)
    return any(source.get(key) == record_id for key in SOURCE_KEYS)


def linked_events(record_id: str, dialect_name: str):
    """The ledger rows that may name ``record_id``: every source key matched with the native
    indexed PG containment op, plus the 2.8.0 cost corrections by their entry id. A superset;
    names_record settles which of them name it."""
    from celerp.models.ledger import LedgerEntry

    if dialect_name == "postgresql":
        from sqlalchemy import cast
        from sqlalchemy.dialects.postgresql import JSONB

        metadata = cast(LedgerEntry.metadata_, JSONB)
        named = [metadata.contains({key: record_id}) for key in SOURCE_KEYS]
    else:
        named = [LedgerEntry.metadata_[key].as_string() == record_id for key in SOURCE_KEYS]
    legacy = LedgerEntry.entity_id.startswith(f"je:auto:{record_id}:cost:", autoescape=True)
    return or_(*named, legacy)


async def record_entry_ids(session, company_id, record_id: str, trigger: str | None = None) -> list[str]:
    """The ids of the journal entries ``record_id`` posted (only those created under ``trigger``
    when given), sorted."""
    from celerp.models.ledger import LedgerEntry

    rows = (await session.execute(select(LedgerEntry.entity_id, LedgerEntry.metadata_).where(
        LedgerEntry.company_id == company_id,
        LedgerEntry.event_type == "acc.journal_entry.created",
        linked_events(record_id, session.get_bind().dialect.name),
    ))).all()
    return sorted({entity_id for entity_id, meta in rows
                   if (trigger is None or (meta or {}).get("trigger") == trigger)
                   and names_record(entity_id, meta or {}, record_id)})
