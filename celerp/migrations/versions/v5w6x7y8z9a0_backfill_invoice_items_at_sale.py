# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Record what each issued invoice sold.

Revision ID: v5w6x7y8z9a0
Revises: u4v5w6x7y8z9

An invoice now keeps each item's category and attributes as they stood when it was first issued
(``items_at_sale``, written by the event that issues it), so what it sold reads the same however
the item is edited later. Invoices issued before this revision have none: each gets its items
as they stand now, the nearest record there is, on its projection and on its first ledger event,
so a rebuild gives the same answer. An invoice that already holds one is left alone, and a rerun
changes nothing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from celerp.migrations._json_compat import decode_json_value, update_ledger_data, update_projection_state
from celerp.migrations._locks import lock_tables

revision = "v5w6x7y8z9a0"
down_revision = "u4v5w6x7y8z9"
branch_labels = None
depends_on = None

KEY = "items_at_sale"
_UNISSUED = {"draft", "void"}


def _issued_without(state: dict) -> bool:
    return (state.get("doc_type") == "invoice" and (state.get("status") or "draft") not in _UNISSUED
            and KEY not in state)


def _item_ids(state: dict) -> set[str]:
    return {li["item_id"] for li in state.get("line_items") or [] if isinstance(li, dict) and li.get("item_id")}


def upgrade() -> None:
    conn = op.get_bind()
    lock_tables(conn, "ledger", "projections")
    wanted: dict[tuple[str, str], set[str]] = {}

    def collect(state, row):
        if _issued_without(state):
            wanted[(str(row["company_id"]), row["entity_id"])] = _item_ids(state)
        return False

    update_projection_state(conn, collect, where="entity_type = 'doc'", extra_cols=("company_id", "entity_id"))
    if not wanted:
        return
    items: dict[tuple[str, str], dict] = {}
    by_company: dict[str, set[str]] = {}
    for (cid, _), ids in wanted.items():
        by_company.setdefault(cid, set()).update(ids)
    for cid, ids in by_company.items():
        rows = conn.execute(sa.text(
            "SELECT entity_id, state FROM projections WHERE CAST(company_id AS text) = :cid "
            "AND entity_type = 'item' AND entity_id = ANY(:ids)"), {"cid": cid, "ids": sorted(ids)}).all()
        for eid, state in rows:
            state = decode_json_value(state) or {}
            items[(cid, eid)] = {"category": state.get("category"), "attributes": state.get("attributes") or {}}
    sold = {key: {i: items[(key[0], i)] for i in sorted(ids) if (key[0], i) in items} for key, ids in wanted.items()}

    def stamp(data, row):
        key = (str(row["company_id"]), row["entity_id"])
        if key not in sold or KEY in data:
            return False
        data[KEY] = sold[key]
        return True

    update_projection_state(conn, stamp, where="entity_type = 'doc'", extra_cols=("company_id", "entity_id"))
    first = [r[0] for r in conn.execute(sa.text(
        "SELECT DISTINCT ON (company_id, entity_id) id FROM ledger WHERE entity_type = 'doc' "
        "AND entity_id = ANY(:ids) ORDER BY company_id, entity_id, id"),
        {"ids": sorted({eid for _, eid in sold})}).all()]
    update_ledger_data(conn, stamp, where="id = ANY(:ids)", params={"ids": first},
                       extra_cols=("company_id", "entity_id"))


def downgrade() -> None:
    """Nothing to undo: the stored record is read only by builds that know it."""
