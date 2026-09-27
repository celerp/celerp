# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Real UTF8 database upgrades through the production migration path with duplicate
physical codes, from both histories a 2.5.x installation can have:

- older than the barcode revision, with duplicate codes already in its items;
- already past the barcode and RFID / EPC revisions, carrying the unique indexes
  those revisions used to create.

Both converge on head with every item unchanged and no physical-code unique index.
"""

from __future__ import annotations

import json
import os
import uuid

from alembic import command
from sqlalchemy import create_engine, text

from celerp.alembic_config import build_alembic_config
from celerp.cli import _migrate_to_head
from celerp.inventory_codes import (
    BARCODE_UNIQUE_INDEX,
    LEGACY_BARCODE_UNIQUE_INDEX,
    RFID_EPC_UNIQUE_INDEX,
)
from celerp.models.base import Base
from celerp.models.projections import Projection

from .conftest import head_rev

BEFORE_BARCODE_REVISION = "b3c4d5e6f7a8"
BEFORE_HEAD_REVISION = "j7e8f9a0b1c2"
FORBIDDEN = {BARCODE_UNIQUE_INDEX, LEGACY_BARCODE_UNIQUE_INDEX, RFID_EPC_UNIQUE_INDEX}


def _index_names(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
        ))
    }


def _seed_company(conn) -> str:
    cid = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
            "VALUES (CAST(:id AS uuid), 'Legacy', :slug, CAST('{}' AS json), true, NOW())"
        ),
        {"id": cid, "slug": f"legacy-{cid[:8]}"},
    )
    return cid


def _seed_item(conn, cid: str, entity_id: str, state: dict) -> None:
    conn.execute(
        text(
            "INSERT INTO projections "
            "(company_id, entity_id, entity_type, state, version, updated_at) "
            "VALUES (CAST(:cid AS uuid), :eid, 'item', CAST(:state AS json), 1, NOW())"
        ),
        {"cid": cid, "eid": entity_id, "state": json.dumps(state)},
    )


_COMPARED = ("sku", "name", "barcode", "rfid_epc")


def _item_states(conn, cid: str) -> dict[str, dict]:
    """Identity and physical-code fields per item. Later revisions legitimately add
    other keys to item state, so only the fields this upgrade must preserve compare."""
    rows = conn.execute(
        text("SELECT entity_id, state FROM projections WHERE company_id = CAST(:cid AS uuid)"),
        {"cid": cid},
    ).all()
    states = {eid: s if isinstance(s, dict) else json.loads(s) for eid, s in rows}
    return {eid: {k: s[k] for k in _COMPARED if k in s} for eid, s in states.items()}


def _assert_converged(sync_url: str, cid: str, expected: dict[str, dict]) -> None:
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head_rev()
            assert _item_states(conn, cid) == expected
            assert _index_names(conn).isdisjoint(FORBIDDEN)
            # main.py runs create_all on every boot; the model must not bring them back.
            Base.metadata.create_all(conn, tables=[Projection.__table__])
            assert _index_names(conn).isdisjoint(FORBIDDEN)
    finally:
        engine.dispose()


def test_pre_barcode_database_with_duplicate_codes_upgrades_to_head(fresh_db):
    async_url, sync_url = fresh_db
    os.environ["DATABASE_URL"] = async_url  # restored by fresh_db
    command.upgrade(build_alembic_config(), BEFORE_BARCODE_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert conn.execute(text("SHOW server_encoding")).scalar() == "UTF8"
            cid = _seed_company(conn)
            _seed_item(conn, cid, "item:a", {"sku": "A", "barcode": "7508", "status": "available", "name": "Émeraude"})
            _seed_item(conn, cid, "item:b", {"sku": "B", "barcode": "7508", "status": "available"})
            _seed_item(conn, cid, "item:c", {"sku": "C", "rfid_epc": "E2001", "status": "available"})
            _seed_item(conn, cid, "item:d", {"sku": "D", "barcode": "E2001", "status": "available"})
            expected = _item_states(conn, cid)
    finally:
        engine.dispose()

    _migrate_to_head(async_url)
    _assert_converged(sync_url, cid, expected)

    _migrate_to_head(async_url)
    _assert_converged(sync_url, cid, expected)


def test_database_carrying_the_unique_indexes_upgrades_to_head(fresh_db):
    async_url, sync_url = fresh_db
    os.environ["DATABASE_URL"] = async_url  # restored by fresh_db
    command.upgrade(build_alembic_config(), BEFORE_HEAD_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            cid = _seed_company(conn)
            _seed_item(conn, cid, "item:a", {"sku": "A", "barcode": "7508", "rfid_epc": "E1", "status": "available"})
            for name, field in (
                (BARCODE_UNIQUE_INDEX, "barcode"),
                (LEGACY_BARCODE_UNIQUE_INDEX, "barcode"),
                (RFID_EPC_UNIQUE_INDEX, "rfid_epc"),
            ):
                conn.execute(text(
                    f"CREATE UNIQUE INDEX {name} ON projections "
                    f"(company_id, (state ->> '{field}')) "
                    f"WHERE entity_type = 'item' AND NULLIF(state ->> '{field}', '') IS NOT NULL"
                ))
            assert FORBIDDEN <= _index_names(conn)
            expected = _item_states(conn, cid)
    finally:
        engine.dispose()

    _migrate_to_head(async_url)
    _assert_converged(sync_url, cid, expected)
