# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A company upgraded from 2.5.x keeps every module it used: the module list 2.5.x
wrote but never read is removed once, and a choice made after the upgrade survives
every later replay of the migration."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from .conftest import run_migration_ops

MODULE = "t7i8j9k0l1m2_company_modules_from_2_5"


def test_revision_follows_unmatched_refunds_on_a_single_head():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    script = ScriptDirectory.from_config(build_alembic_config())
    assert len(script.get_heads()) == 1
    assert script.get_revision("t7i8j9k0l1m2").down_revision == "s6h7c8d9e0f1"


@pytest.fixture()
def companies_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migmods_{uuid.uuid4().hex[:8]}"
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


def test_a_company_that_toggled_one_module_on_2_5_keeps_every_module(companies_db):
    from celerp.modules.registry import uses_module

    toggled = _put(companies_db, {"enabled_modules": ["celerp-labels"], "currency": "THB"})
    switched_off = _put(companies_db, {"enabled_modules": []})
    untouched = _put(companies_db, {"currency": "USD"})
    assert not uses_module(_get(companies_db, toggled), "celerp-accounting")

    run_migration_ops(companies_db, MODULE)

    assert _get(companies_db, toggled) == {"currency": "THB"}
    assert _get(companies_db, switched_off) == {}
    assert _get(companies_db, untouched) == {"currency": "USD"}
    for cid in (toggled, switched_off):
        settings = _get(companies_db, cid)
        assert uses_module(settings, "celerp-accounting") and uses_module(settings, "celerp-labels")


def test_a_choice_made_after_the_upgrade_survives_a_replay(companies_db):
    run_migration_ops(companies_db, MODULE)
    chosen = _put(companies_db, {"enabled_modules": ["celerp-labels"]})

    run_migration_ops(companies_db, MODULE)

    assert _get(companies_db, chosen) == {"enabled_modules": ["celerp-labels"]}
