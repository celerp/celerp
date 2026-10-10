# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Migration e4f5a6b7c8d9 stamps the connector key on records imported before it.

The real upgrade runs through an alembic Operations context. It writes the key to the
projection and to the event that created it, so a rebuild keeps it, leaves records that
already carry a key alone, and adds the lookup index. Running it twice changes nothing.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from .conftest import run_migration_ops
from .test_x7y8z9a0b1c2_add_projection_created_at import sync_db  # noqa: F401

MIG = "e4f5a6b7c8d9_backfill_connector_idempotency_key"


def _record(conn, cid: str, entity_type: str, event_type: str, state: dict) -> str:
    eid = f"{entity_type}:{uuid.uuid4()}"
    conn.execute(text(
        "INSERT INTO ledger (entity_id, company_id, entity_type, event_type, data) "
        "VALUES (:e, :c, :t, :ev, CAST(:d AS jsonb))"
    ), {"e": eid, "c": cid, "t": entity_type, "ev": event_type, "d": json.dumps(state)})
    conn.execute(text(
        "INSERT INTO projections (entity_id, company_id, entity_type, state) "
        "VALUES (:e, :c, :t, CAST(:d AS jsonb))"
    ), {"e": eid, "c": cid, "t": entity_type, "d": json.dumps(state)})
    return eid


def _keys(engine, eid: str) -> tuple:
    with engine.connect() as conn:
        p = conn.execute(text("SELECT state ->> 'idempotency_key' FROM projections WHERE entity_id = :e"),
                         {"e": eid}).scalar_one()
        l = conn.execute(text("SELECT data ->> 'idempotency_key' FROM ledger WHERE entity_id = :e"),
                         {"e": eid}).scalar_one()
    return p, l


def _index_exists(engine) -> bool:
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_projections_connector_key' "
            "AND schemaname = current_schema()"
        )).scalar_one() == 1


def test_the_upgrade_stamps_the_key_on_the_projection_and_its_event(sync_db):  # noqa: F811
    cid = str(uuid.uuid4())
    with sync_db.begin() as conn:
        order = _record(conn, cid, "doc", "doc.created", {"woocommerce_order_id": 77})
        customer = _record(conn, cid, "contact", "crm.contact.created", {"attributes": {"xero_id": "X-9"}})
        keyed = _record(conn, cid, "doc", "doc.created",
                        {"shopify_order_id": 5, "idempotency_key": "shopify:order:already"})
        manual = _record(conn, cid, "doc", "doc.created", {"doc_number": "INV-1"})

    run_migration_ops(sync_db, MIG)

    assert _keys(sync_db, order) == ("woocommerce:order:77", "woocommerce:order:77")
    assert _keys(sync_db, customer) == ("xero:contact:X-9", "xero:contact:X-9")
    assert _keys(sync_db, keyed) == ("shopify:order:already", "shopify:order:already")
    assert _keys(sync_db, manual) == (None, None)
    assert _index_exists(sync_db)


def test_running_the_upgrade_again_changes_nothing_and_downgrade_drops_the_index(sync_db):  # noqa: F811
    cid = str(uuid.uuid4())
    with sync_db.begin() as conn:
        order = _record(conn, cid, "doc", "doc.created", {"quickbooks_invoice_id": "Q1"})

    run_migration_ops(sync_db, MIG)
    run_migration_ops(sync_db, MIG)
    assert _keys(sync_db, order) == ("quickbooks:invoice:Q1", "quickbooks:invoice:Q1")

    run_migration_ops(sync_db, MIG, "downgrade")
    assert not _index_exists(sync_db)
