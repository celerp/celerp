# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for migration n9b0c1d2e3f4: every document and List line stored before line ids
existed gets the id derived from its record id and position, the same id a replay of the
record derives, and a line that has an id keeps it."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

_MOD = "n9b0c1d2e3f4_fill_line_ids"
_KEPT = "11111111-1111-4111-8111-111111111111"


def _insert(mig_db, entity_type: str, state: dict) -> tuple[str, str]:
    cid, eid = str(uuid.uuid4()), f"{entity_type}:{uuid.uuid4().hex[:8]}"
    with mig_db.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO projections (entity_id, company_id, entity_type, state)"
            " VALUES (:eid, :cid, :t, CAST(:state AS jsonb))"
        ), {"eid": eid, "cid": cid, "t": entity_type, "state": json.dumps(state)})
    return cid, eid


def _idless_lines(mig_db) -> int:
    with mig_db.engine.connect() as conn:
        return conn.execute(text(
            "SELECT count(*) FROM projections p, jsonb_array_elements(p.state->'line_items') li"
            " WHERE p.entity_type IN ('doc','list') AND coalesce(li->>'line_id', '') = ''")).scalar()


def _row_versions(mig_db) -> dict:
    """Each row's Postgres row version: it changes only when the row is written."""
    with mig_db.engine.connect() as conn:
        return dict(conn.execute(text("SELECT entity_id, xmin::text FROM projections")).all())


def check_backfill_leaves_no_idless_line(mig_db, records: list[tuple[str, str]]) -> None:
    """After the upgrade no document or List line is without an id, each derived id is
    ``derived_line_id(entity_id, position)``, and a second run writes no row."""
    from celerp.services.document_lines import derived_line_id
    mig_db.run_upgrade(_MOD)
    assert _idless_lines(mig_db) == 0
    for cid, eid in records:
        for i, li in enumerate(mig_db.get_state(cid, eid)["line_items"]):
            assert li["line_id"] == (_KEPT if li.get("kept") else derived_line_id(eid, i)), (eid, i, li)
    before = _row_versions(mig_db)
    mig_db.run_upgrade(_MOD)
    assert _row_versions(mig_db) == before


def test_backfill_gives_every_document_and_list_line_an_id(mig_db):
    records = [
        _insert(mig_db, "doc", {"doc_type": "invoice", "line_items": [
            {"sku": "A", "quantity": 1}, {"description": "GIA Certificate", "line_id": ""},
            {"sku": "B", "line_id": _KEPT, "kept": True}, {"sku": "C", "line_id": "not-a-uuid"}]}),
        _insert(mig_db, "list", {"list_type": "quotation", "line_items": [{"sku": "D"}]}),
        _insert(mig_db, "doc", {"doc_type": "memo", "line_items": [{"sku": "E", "line_id": _KEPT, "kept": True}]}),
    ]
    assert _idless_lines(mig_db) == 4
    check_backfill_leaves_no_idless_line(mig_db, records)


def test_backfill_leaves_other_records_alone(mig_db):
    cid, eid = _insert(mig_db, "item", {"sku": "X", "line_items": [{"sku": "inner"}]})
    mig_db.run_upgrade(_MOD)
    assert mig_db.get_state(cid, eid) == {"sku": "X", "line_items": [{"sku": "inner"}]}
