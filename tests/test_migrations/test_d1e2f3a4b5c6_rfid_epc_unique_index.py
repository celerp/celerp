# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from celerp.inventory_codes import RFID_EPC_UNIQUE_INDEX

from .conftest import run_migration_ops

MODULE = "d1e2f3a4b5c6_rfid_epc_unique_index"


def _index_exists(engine) -> bool:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM pg_indexes "
                "WHERE indexname = :n AND schemaname = current_schema()"
            ),
            {"n": RFID_EPC_UNIQUE_INDEX},
        ).scalar_one() > 0


def test_utf8_creates_rfid_index_and_enforces_uniqueness(mig_db):
    cid = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:1", {"sku": "A", "rfid_epc": "A1B2"})
    run_migration_ops(mig_db.engine, MODULE)
    assert _index_exists(mig_db.engine)

    with pytest.raises(IntegrityError):
        mig_db.insert_item(cid, "item:2", {"sku": "B", "rfid_epc": "A1B2"})


def test_sql_ascii_removes_rfid_expression_index(sql_ascii_fresh_db):
    _, sync_url = sql_ascii_fresh_db
    engine = create_engine(sync_url)
    try:
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
            cid = str(uuid.uuid4())
            conn.execute(
                text("INSERT INTO projections VALUES ('item:1', CAST(:cid AS uuid), 'item', CAST(:s AS json))"),
                {"cid": cid, "s": '{"sku":"A","rfid_epc":"A1B2"}'},
            )
            conn.execute(text(
                f"CREATE UNIQUE INDEX {RFID_EPC_UNIQUE_INDEX} "
                "ON projections (company_id, (state ->> 'rfid_epc')) "
                "WHERE entity_type = 'item' AND NULLIF(state ->> 'rfid_epc', '') IS NOT NULL"
            ))

        run_migration_ops(engine, MODULE)
        assert not _index_exists(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM projections")).scalar_one() == 1
    finally:
        engine.dispose()
