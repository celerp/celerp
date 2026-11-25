# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Column setup moves to each user: the company-wide visible-column lists an older version
kept per category are removed once, and every user's own setup survives every replay."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from .conftest import run_migration_ops

MODULE = "e5c2b8d1f4a7_column_prefs_per_user"


@pytest.fixture()
def companies_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migcols_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE companies (id UUID PRIMARY KEY, settings JSON NOT NULL)"))
    yield engine
    engine.dispose()
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _put(engine, settings: dict) -> uuid.UUID:
    cid = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO companies (id, settings) VALUES (:i, CAST(:s AS json))"),
                     {"i": cid, "s": json.dumps(settings)})
    return cid


def _get(engine, cid) -> dict:
    with engine.connect() as conn:
        value = conn.execute(text("SELECT settings FROM companies WHERE id = :i"), {"i": cid}).scalar()
    return value if isinstance(value, dict) else json.loads(value)


def test_company_wide_column_lists_go_and_each_users_setup_stays(companies_db):
    user = str(uuid.uuid4())
    mine = {"inventory": {"vis": {"sku": False}, "order": ["name", "sku"]}}
    older = _put(companies_db, {"column_prefs": {"__all__": ["sku", "name"], "gems": ["sku"]}, "currency": "THB"})
    mixed = _put(companies_db, {"column_prefs": {"__all__": ["sku"], user: mine}})
    untouched = _put(companies_db, {"currency": "USD"})

    run_migration_ops(companies_db, MODULE)
    run_migration_ops(companies_db, MODULE)

    assert _get(companies_db, older) == {"column_prefs": {}, "currency": "THB"}
    assert _get(companies_db, mixed) == {"column_prefs": {user: mine}}
    assert _get(companies_db, untouched) == {"currency": "USD"}
