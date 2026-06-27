# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Regression test for https://github.com/celerp/celerp/issues/189.

The data-backfill migrations must not cast the `json` columns (ledger.data,
projections.state) to `jsonb` server-side: on a SQL_ASCII cluster that decode of
a \\uXXXX escape fails with "Unicode escape value could not be translated to the
server's encoding SQL_ASCII", aborting the upgrade.

This reproduces the real production shape — a SQL_ASCII database with `json`
(not jsonb) columns and accented content stored as ASCII \\u escapes — and proves
the migrations now run and preserve the data. The server is reused from the
`mig_db` fixture, so it runs wherever the rest of the suite runs.
"""

from __future__ import annotations

import importlib
import json
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


def _url_on_db(src: URL, dbname: str) -> URL:
    return URL.create(
        "postgresql+psycopg2",
        username=src.username,
        password=src.password,
        host=src.host,
        port=src.port,
        database=dbname,
    )


@pytest.fixture()
def sql_ascii_db(mig_db):
    """A throwaway SQL_ASCII database with json (not jsonb) projections+ledger tables,
    created on the same server the test suite already uses."""
    src = mig_db.engine.url
    name = f"sqlascii_{uuid.uuid4().hex[:8]}"

    admin = create_engine(_url_on_db(src, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            try:
                c.execute(text(
                    f'CREATE DATABASE "{name}" ENCODING \'SQL_ASCII\' '
                    f"TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C'"
                ))
            except Exception as e:  # no createdb privilege (e.g. locked-down CI)
                pytest.skip(f"cannot create a SQL_ASCII database: {e}")
    finally:
        admin.dispose()

    eng = create_engine(_url_on_db(src, name))
    with eng.begin() as conn:
        assert conn.execute(text("SHOW server_encoding")).scalar() == "SQL_ASCII"
        conn.execute(text("""
            CREATE TABLE projections (
                entity_id TEXT NOT NULL,
                company_id UUID NOT NULL,
                entity_type TEXT NOT NULL,
                state JSON NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT '2026-01-01',
                PRIMARY KEY (company_id, entity_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE ledger (
                id SERIAL PRIMARY KEY,
                company_id UUID NOT NULL,
                entity_id TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'doc',
                event_type TEXT NOT NULL,
                data JSON NOT NULL,
                ts TIMESTAMPTZ NOT NULL DEFAULT '2026-03-04T10:00:00Z'
            )
        """))
    yield eng
    eng.dispose()

    admin = create_engine(_url_on_db(src, "postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _run_upgrade(conn, module_name: str) -> None:
    full = f"celerp.migrations.versions.{module_name}"
    mock_op = MagicMock()
    mock_op.get_bind.return_value = conn
    sys.modules.pop(full, None)
    with patch.dict("sys.modules", {"alembic.op": mock_op}):
        mig = importlib.import_module(full)
        with patch.object(mig, "op", mock_op):
            mig.upgrade()


def test_old_jsonb_cast_would_fail_on_sql_ascii(sql_ascii_db):
    """Guardrail: confirm the environment really reproduces the bug — a server-side
    json->jsonb cast of accented content blows up here."""
    cid = str(uuid.uuid4())
    state = json.dumps({"sku": "A", "name": "Crème Brûlée"})  # ensure_ascii → \u escapes
    with sql_ascii_db.begin() as conn:
        conn.execute(text("""
            INSERT INTO projections (entity_id, company_id, entity_type, state)
            VALUES ('item:1', CAST(:cid AS uuid), 'item', CAST(:s AS json))
        """), {"cid": cid, "s": state})
    with pytest.raises(Exception) as ei:
        with sql_ascii_db.begin() as conn:
            conn.execute(text("SELECT (state::jsonb)->>'name' FROM projections"))
    assert "SQL_ASCII" in str(ei.value)


def test_projection_backfill_runs_and_preserves_accents(sql_ascii_db):
    cid = str(uuid.uuid4())
    state = json.dumps({"sku": "A", "name": "Crème Brûlée", "sell_by": "kg"})
    with sql_ascii_db.begin() as conn:
        conn.execute(text("""
            INSERT INTO projections (entity_id, company_id, entity_type, state)
            VALUES ('item:1', CAST(:cid AS uuid), 'item', CAST(:s AS json))
        """), {"cid": cid, "s": state})
        # the failing migration from the report
        _run_upgrade(conn, "v7w8x9y0z1a2_backfill_purchase_unit_defaults")

    with sql_ascii_db.connect() as conn:
        got = conn.execute(text("SELECT state FROM projections")).scalar()
    got = got if isinstance(got, dict) else json.loads(got)
    assert got["name"] == "Crème Brûlée"          # accent preserved
    assert got["purchase_conversion_factor"] == 1  # backfilled
    assert got["purchase_unit"] == "kg"            # = sell_by


def test_ledger_backfill_runs_and_preserves_accents(sql_ascii_db):
    cid = str(uuid.uuid4())
    data = json.dumps({"contact_name": "José Müller", "amount": 100})  # no payment_date
    with sql_ascii_db.begin() as conn:
        conn.execute(text("""
            INSERT INTO ledger (company_id, entity_id, event_type, data)
            VALUES (CAST(:cid AS uuid), 'doc:1', 'doc.payment.received', CAST(:d AS json))
        """), {"cid": cid, "d": data})
        _run_upgrade(conn, "n8o9p0q1r2s3_backfill_payment_date")

    with sql_ascii_db.connect() as conn:
        got = conn.execute(text("SELECT data FROM ledger")).scalar()
    got = got if isinstance(got, dict) else json.loads(got)
    assert got["contact_name"] == "José Müller"   # accent preserved
    assert got["payment_date"] == "2026-03-04"    # backfilled from ts