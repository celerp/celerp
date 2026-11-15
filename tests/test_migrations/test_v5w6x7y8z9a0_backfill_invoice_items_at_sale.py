# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Migration v5w6x7y8z9a0 records what each issued invoice sold.

An issued invoice with no record gets its items' category and attributes as they stand, on its
projection and on its first event, so a rebuild keeps them. Drafts, voids, other documents and
an invoice that already holds a record are left alone, and a rerun changes nothing.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from .conftest import run_migration_ops
from .test_x7y8z9a0b1c2_add_projection_created_at import sync_db  # noqa: F401

MIG = "v5w6x7y8z9a0_backfill_invoice_items_at_sale"


def _record(engine, cid: str, entity_type: str, state: dict, *events: str) -> str:
    eid = f"{entity_type}:{uuid.uuid4()}"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO projections (entity_id, company_id, entity_type, state) "
                          "VALUES (:e, :c, :t, CAST(:d AS jsonb))"),
                     {"e": eid, "c": cid, "t": entity_type, "d": json.dumps(state)})
        for event in events or (f"{entity_type}.created",):
            conn.execute(text("INSERT INTO ledger (entity_id, company_id, entity_type, event_type, data) "
                              "VALUES (:e, :c, :t, :ev, CAST(:d AS jsonb))"),
                         {"e": eid, "c": cid, "t": entity_type, "ev": event, "d": json.dumps({})})
    return eid


def _sold(engine, eid: str) -> tuple:
    with engine.connect() as conn:
        state = conn.execute(text("SELECT state -> 'items_at_sale' FROM projections WHERE entity_id = :e"),
                             {"e": eid}).scalar_one()
        events = [r[0] for r in conn.execute(text(
            "SELECT data -> 'items_at_sale' FROM ledger WHERE entity_id = :e ORDER BY id"), {"e": eid}).all()]
    return state, events


def test_the_upgrade_records_what_each_issued_invoice_sold(sync_db):  # noqa: F811
    cid = str(uuid.uuid4())
    item = _record(sync_db, cid, "item", {"category": "membership", "attributes": {"membership_term": "Lifetime"},
                                          "name": "Life"})
    lines = {"line_items": [{"item_id": item, "quantity": 1}, {"name": "free text"}]}
    issued = _record(sync_db, cid, "doc", {"doc_type": "invoice", "status": "final", **lines},
                     "doc.created", "doc.finalized")
    draft = _record(sync_db, cid, "doc", {"doc_type": "invoice", "status": "draft", **lines})
    void = _record(sync_db, cid, "doc", {"doc_type": "invoice", "status": "void", **lines})
    quote = _record(sync_db, cid, "doc", {"doc_type": "quotation", "status": "final", **lines})
    kept = _record(sync_db, cid, "doc", {"doc_type": "invoice", "status": "paid", "items_at_sale": {}, **lines})
    sold = {item: {"category": "membership", "attributes": {"membership_term": "Lifetime"}}}

    run_migration_ops(sync_db, MIG)
    run_migration_ops(sync_db, MIG)

    assert _sold(sync_db, issued) == (sold, [sold, None])
    for doc in (draft, void, quote):
        assert _sold(sync_db, doc) == (None, [None])
    assert _sold(sync_db, kept) == ({}, [None])
