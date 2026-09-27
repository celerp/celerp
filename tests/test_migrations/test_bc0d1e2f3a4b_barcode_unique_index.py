# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The barcode revision upgrades any data unchanged and leaves no barcode unique index.

Barcode uniqueness is enforced where a write introduces a code, so this revision must
never refuse a database because of the codes it already holds."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import create_engine, text

from celerp.inventory_codes import BARCODE_UNIQUE_INDEX, LEGACY_BARCODE_UNIQUE_INDEX

from .conftest import run_migration_ops

MODULE = "bc0d1e2f3a4b_barcode_unique_index"
INDEX = BARCODE_UNIQUE_INDEX
LEGACY_INDEX = LEGACY_BARCODE_UNIQUE_INDEX

_CREATE_LEGACY = (
    f"CREATE UNIQUE INDEX {LEGACY_INDEX} "
    "ON projections (company_id, (state ->> 'barcode')) "
    "WHERE entity_type = 'item' AND NULLIF(state ->> 'barcode', '') IS NOT NULL"
)
_CREATE_CURRENT = (
    f"CREATE UNIQUE INDEX {INDEX} "
    "ON projections (company_id, (state ->> 'barcode')) "
    "WHERE entity_type = 'item' AND NULLIF(state ->> 'barcode', '') IS NOT NULL "
    "AND lower(COALESCE(state ->> 'status', '')) <> 'merged'"
)


def _index_names(engine) -> set[str]:
    # Scoped to this fixture's isolated schema: pg_indexes spans every schema, so an
    # unscoped match would see an identically named index another worker built.
    with engine.connect() as conn:
        return {
            row[0]
            for row in conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
            ))
        }


def _all_states(engine) -> dict[str, dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT entity_id, state FROM projections")).all()
    return {eid: s if isinstance(s, dict) else json.loads(s) for eid, s in rows}


def test_upgrade_succeeds_on_duplicate_and_oversized_codes_and_preserves_data(mig_db):
    cid = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:1", {"sku": "A", "barcode": "7508", "status": "available"})
    mig_db.insert_item(cid, "item:2", {"sku": "B", "barcode": "7508", "status": "available"})
    mig_db.insert_item(cid, "item:long-bc", {"sku": "C", "barcode": "1" * 65})
    mig_db.insert_item(cid, "item:long-sku", {"sku": "S" * 256, "barcode": "222"})
    mig_db.insert_item(cid, "item:comma", {"sku": "X,Y", "barcode": "333"})
    before = _all_states(mig_db.engine)

    run_migration_ops(mig_db.engine, MODULE)

    assert _all_states(mig_db.engine) == before
    assert _index_names(mig_db.engine).isdisjoint({INDEX, LEGACY_INDEX})


def test_upgrade_drops_existing_indexes_and_is_idempotent(mig_db):
    cid = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:1", {"sku": "A", "barcode": "12345"})
    with mig_db.engine.begin() as conn:
        conn.execute(text(_CREATE_LEGACY))
        conn.execute(text(_CREATE_CURRENT))
    assert {INDEX, LEGACY_INDEX} <= _index_names(mig_db.engine)

    run_migration_ops(mig_db.engine, MODULE)
    assert _index_names(mig_db.engine).isdisjoint({INDEX, LEGACY_INDEX})

    run_migration_ops(mig_db.engine, MODULE)
    assert _index_names(mig_db.engine).isdisjoint({INDEX, LEGACY_INDEX})
    assert mig_db.get_state(cid, "item:1")["barcode"] == "12345"


def test_downgrade_leaves_no_barcode_index(mig_db):
    cid = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:1", {"sku": "A", "barcode": "7508"})
    mig_db.insert_item(cid, "item:2", {"sku": "B", "barcode": "7508"})
    with mig_db.engine.begin() as conn:
        conn.execute(text(_CREATE_LEGACY.replace("CREATE UNIQUE INDEX", "CREATE INDEX")))

    run_migration_ops(mig_db.engine, MODULE, "downgrade")

    assert _index_names(mig_db.engine).isdisjoint({INDEX, LEGACY_INDEX})
    assert mig_db.get_state(cid, "item:2")["barcode"] == "7508"


def _create_sql_ascii_projections(engine) -> None:
    with engine.begin() as conn:
        assert conn.execute(text("SHOW server_encoding")).scalar() == "SQL_ASCII"
        conn.execute(text("""
            CREATE TABLE projections (
                entity_id TEXT NOT NULL,
                company_id UUID NOT NULL,
                entity_type TEXT NOT NULL,
                state JSON NOT NULL,
                PRIMARY KEY (company_id, entity_id)
            )
        """))


def _insert_sql_ascii_item(conn, cid: str, entity_id: str, state: dict) -> None:
    conn.execute(
        text(
            "INSERT INTO projections VALUES "
            "(:entity_id, CAST(:cid AS uuid), 'item', CAST(:state AS json))"
        ),
        {"entity_id": entity_id, "cid": cid, "state": json.dumps(state)},
    )


def test_sql_ascii_upgrade_succeeds_on_duplicates_with_unrelated_unicode(sql_ascii_fresh_db):
    _, sync_url = sql_ascii_fresh_db
    engine = create_engine(sync_url)
    try:
        _create_sql_ascii_projections(engine)
        cid = str(uuid.uuid4())
        with engine.begin() as conn:
            _insert_sql_ascii_item(
                conn, cid, "item:1",
                {"sku": "A", "barcode": "7508", "name": "Crème Brûlée"},
            )
            _insert_sql_ascii_item(
                conn, cid, "item:2",
                {"sku": "B", "barcode": "7508", "status": "available", "name": "Müller"},
            )

        run_migration_ops(engine, MODULE)
        run_migration_ops(engine, MODULE)

        assert _index_names(engine).isdisjoint({INDEX, LEGACY_INDEX})
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM projections")).scalar_one() == 2
    finally:
        engine.dispose()


def test_sql_ascii_removes_barcode_expression_indexes(sql_ascii_fresh_db):
    _, sync_url = sql_ascii_fresh_db
    engine = create_engine(sync_url)
    try:
        _create_sql_ascii_projections(engine)
        with engine.begin() as conn:
            _insert_sql_ascii_item(
                conn, str(uuid.uuid4()), "item:1",
                {"sku": "A", "barcode": "12345", "status": "available"},
            )
            conn.execute(text(_CREATE_LEGACY))
            conn.execute(text(_CREATE_CURRENT))

        run_migration_ops(engine, MODULE)

        assert _index_names(engine).isdisjoint({INDEX, LEGACY_INDEX})
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM projections")).scalar_one() == 1
    finally:
        engine.dispose()
