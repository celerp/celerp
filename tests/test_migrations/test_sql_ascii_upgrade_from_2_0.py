# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Real 2.0-era SQL_ASCII database upgrade through the production migration path."""

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

BASE_2_0_REVISION = "d5e6f7a8b9c0"


def _index_names(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
        ))
    }


def test_sql_ascii_2_0_database_migrates_to_head_without_json_expression_indexes(
    sql_ascii_fresh_db,
):
    async_url, sync_url = sql_ascii_fresh_db
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = async_url
    try:
        command.upgrade(build_alembic_config(), BASE_2_0_REVISION)

        engine = create_engine(sync_url)
        cid = str(uuid.uuid4())
        try:
            with engine.begin() as conn:
                assert conn.execute(text("SHOW server_encoding")).scalar() == "SQL_ASCII"
                settings = {
                    "manufacturing": {"hours_per_day": "7.5", "note": "Müller"},
                    "role_permissions": {"view_payments": "operator"},
                    "company_note": "Crème Brûlée",
                }
                conn.execute(
                    text(
                        "INSERT INTO companies "
                        "(id, name, slug, settings, is_active, created_at) "
                        "VALUES (CAST(:id AS uuid), 'Legacy', :slug, CAST(:settings AS json), true, NOW())"
                    ),
                    {
                        "id": cid,
                        "slug": f"legacy-{cid[:8]}",
                        "settings": json.dumps(settings),
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO projections "
                        "(company_id, entity_id, entity_type, state, version, updated_at) "
                        "VALUES (CAST(:cid AS uuid), 'item:legacy', 'item', CAST(:state AS json), 1, NOW())"
                    ),
                    {
                        "cid": cid,
                        "state": json.dumps({
                            "sku": "ABC",
                            "barcode": "12345",
                            "rfid_epc": "E200001",
                            "name": "Legacy Item",
                            "status": "available",
                        }),
                    },
                )

                # Accounting tables are module-owned, not core-Alembic-owned.
                # Mimic a 2.0 installation with accounting enabled so c9 is
                # exercised by the same real migration pass.
                if conn.execute(text("SELECT to_regclass('accounts')")).scalar() is None:
                    conn.execute(text("""
                        CREATE TABLE accounts (
                            id UUID PRIMARY KEY,
                            company_id UUID NOT NULL,
                            code VARCHAR(32) NOT NULL,
                            name TEXT NOT NULL,
                            account_type VARCHAR(32) NOT NULL,
                            parent_code VARCHAR(32),
                            is_active BOOLEAN NOT NULL DEFAULT TRUE,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            CONSTRAINT uq_account_company_code UNIQUE (company_id, code)
                        )
                    """))
                conn.execute(
                    text(
                        "INSERT INTO accounts "
                        "(id, company_id, code, name, account_type, parent_code, is_active, created_at) "
                        "VALUES (gen_random_uuid(), CAST(:cid AS uuid), '6000', 'Expenses', "
                        "'expense', NULL, true, NOW())"
                    ),
                    {"cid": cid},
                )
        finally:
            engine.dispose()

        _migrate_to_head(async_url)

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head_rev()
                migrated_settings = conn.execute(
                    text("SELECT settings FROM companies WHERE id = CAST(:cid AS uuid)"),
                    {"cid": cid},
                ).scalar_one()
                migrated_settings = (
                    migrated_settings
                    if isinstance(migrated_settings, dict)
                    else json.loads(migrated_settings)
                )
                assert migrated_settings["company_note"] == "Crème Brûlée"
                assert migrated_settings["manufacturing"] == {"note": "Müller"}
                assert set(migrated_settings["role_grants"]["view_payments"]) == {
                    "operator", "manager", "admin", "owner"
                }
                assert "role_permissions" not in migrated_settings

                work_center = conn.execute(
                    text(
                        "SELECT hours_per_day FROM work_centers "
                        "WHERE company_id = CAST(:cid AS uuid) AND is_default"
                    ),
                    {"cid": cid},
                ).scalar_one()
                assert work_center == 7.5

                state = conn.execute(
                    text(
                        "SELECT state FROM projections "
                        "WHERE company_id = CAST(:cid AS uuid) AND entity_id = 'item:legacy'"
                    ),
                    {"cid": cid},
                ).scalar_one()
                state = state if isinstance(state, dict) else json.loads(state)
                assert state["name"] == "Legacy Item"

                assert conn.execute(
                    text(
                        "SELECT count(*) FROM accounts "
                        "WHERE company_id = CAST(:cid AS uuid) AND code = '6970'"
                    ),
                    {"cid": cid},
                ).scalar_one() == 1

                forbidden = {
                    BARCODE_UNIQUE_INDEX,
                    LEGACY_BARCODE_UNIQUE_INDEX,
                    RFID_EPC_UNIQUE_INDEX,
                }
                assert _index_names(conn).isdisjoint(forbidden)

                # main.py calls create_all on every boot. Existing tables must
                # not cause SQLAlchemy to recreate the intentionally skipped indexes.
                Base.metadata.create_all(conn, tables=[Projection.__table__])
                assert _index_names(conn).isdisjoint(forbidden)

                snapshot = (
                    migrated_settings,
                    work_center,
                    state,
                    conn.execute(
                        text(
                            "SELECT count(*) FROM accounts "
                            "WHERE company_id = CAST(:cid AS uuid) AND code = '6970'"
                        ),
                        {"cid": cid},
                    ).scalar_one(),
                )
        finally:
            engine.dispose()

        _migrate_to_head(async_url)

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                settings2 = conn.execute(
                    text("SELECT settings FROM companies WHERE id = CAST(:cid AS uuid)"),
                    {"cid": cid},
                ).scalar_one()
                settings2 = settings2 if isinstance(settings2, dict) else json.loads(settings2)
                state2 = conn.execute(
                    text(
                        "SELECT state FROM projections "
                        "WHERE company_id = CAST(:cid AS uuid) AND entity_id = 'item:legacy'"
                    ),
                    {"cid": cid},
                ).scalar_one()
                state2 = state2 if isinstance(state2, dict) else json.loads(state2)
                snapshot2 = (
                    settings2,
                    conn.execute(
                        text(
                            "SELECT hours_per_day FROM work_centers "
                            "WHERE company_id = CAST(:cid AS uuid) AND is_default"
                        ),
                        {"cid": cid},
                    ).scalar_one(),
                    state2,
                    conn.execute(
                        text(
                            "SELECT count(*) FROM accounts "
                            "WHERE company_id = CAST(:cid AS uuid) AND code = '6970'"
                        ),
                        {"cid": cid},
                    ).scalar_one(),
                )
                assert snapshot2 == snapshot
                assert _index_names(conn).isdisjoint({
                    BARCODE_UNIQUE_INDEX,
                    LEGACY_BARCODE_UNIQUE_INDEX,
                    RFID_EPC_UNIQUE_INDEX,
                })
        finally:
            engine.dispose()
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved
