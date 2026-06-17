# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for migration o9p0q1r2s3t4: backfill item price fields from legacy format."""

from __future__ import annotations

import json
import os
import uuid

import pytest
import sqlalchemy as sa


# ---------------------------------------------------------------------------
# Minimal sync Postgres schema for migration testing (alembic op.get_bind() is
# sync). Runs against Postgres so the migration's real jsonb_set path is tested.
# ---------------------------------------------------------------------------

@pytest.fixture()
def sync_db():
    """Fresh, isolated Postgres schema with just the projections table."""
    from sqlalchemy import create_engine, text

    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migtest_{uuid.uuid4().hex[:8]}"

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()

    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE projections (
                entity_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                state JSONB NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                location_id TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT '2026-01-01',
                is_available BOOLEAN,
                is_on_memo BOOLEAN,
                is_on_marketplace BOOLEAN,
                is_in_production BOOLEAN,
                is_expired BOOLEAN,
                expires_at TIMESTAMPTZ,
                consignment_flag TEXT,
                PRIMARY KEY (company_id, entity_id)
            )
        """))
    yield engine
    engine.dispose()

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _insert_item(conn, company_id: str, entity_id: str, state: dict) -> None:
    conn.execute(sa.text("""
        INSERT INTO projections (entity_id, company_id, entity_type, state)
        VALUES (:entity_id, :company_id, 'item', CAST(:state AS jsonb))
    """), {"entity_id": entity_id, "company_id": company_id, "state": json.dumps(state)})


def _get_state(conn, company_id: str, entity_id: str) -> dict:
    row = conn.execute(sa.text("""
        SELECT state FROM projections
        WHERE company_id = :company_id AND entity_id = :entity_id
    """), {"company_id": company_id, "entity_id": entity_id}).fetchone()
    assert row is not None
    # psycopg2 returns JSONB as a dict; tolerate a str just in case.
    return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def test_promotes_cost_price(sync_db):
    """Item with price_type=cost_price / new_price but no cost_price gets promoted."""
    company_id = str(uuid.uuid4())
    entity_id = f"item:demo-{uuid.uuid4()}"
    with sync_db.begin() as conn:
        _insert_item(conn, company_id, entity_id, {
            "sku": "DEMO-001",
            "price_type": "cost_price",
            "new_price": 8500.0,
        })

    from celerp.migrations.versions.o9p0q1r2s3t4_backfill_item_price_fields import upgrade
    with sync_db.begin() as conn:

        class _MockOp:
            @staticmethod
            def get_bind():
                return conn

        import alembic.op as alembic_op_module
        orig = alembic_op_module.get_bind
        alembic_op_module.get_bind = _MockOp.get_bind
        try:
            upgrade()
        finally:
            alembic_op_module.get_bind = orig

    with sync_db.connect() as conn:
        state = _get_state(conn, company_id, entity_id)

    assert state["cost_price"] == 8500.0, "cost_price should be promoted from new_price"


def test_skips_item_already_having_price(sync_db):
    """Item that already has cost_price set must not be overwritten."""
    company_id = str(uuid.uuid4())
    entity_id = f"item:demo-{uuid.uuid4()}"
    with sync_db.begin() as conn:
        _insert_item(conn, company_id, entity_id, {
            "sku": "DEMO-002",
            "price_type": "cost_price",
            "new_price": 100.0,
            "cost_price": 999.0,  # already correct
        })

    from celerp.migrations.versions.o9p0q1r2s3t4_backfill_item_price_fields import upgrade
    with sync_db.begin() as conn:
        import alembic.op as alembic_op_module
        orig = alembic_op_module.get_bind
        alembic_op_module.get_bind = lambda: conn
        try:
            upgrade()
        finally:
            alembic_op_module.get_bind = orig

    with sync_db.connect() as conn:
        state = _get_state(conn, company_id, entity_id)

    assert state["cost_price"] == 999.0, "existing cost_price must not be overwritten"


def test_skips_item_without_legacy_fields(sync_db):
    """Normal item (no price_type/new_price in state) is untouched."""
    company_id = str(uuid.uuid4())
    entity_id = f"item:{uuid.uuid4()}"
    with sync_db.begin() as conn:
        _insert_item(conn, company_id, entity_id, {
            "sku": "NORMAL-001",
            "cost_price": 500.0,
            "retail_price": 750.0,
        })

    from celerp.migrations.versions.o9p0q1r2s3t4_backfill_item_price_fields import upgrade
    with sync_db.begin() as conn:
        import alembic.op as alembic_op_module
        orig = alembic_op_module.get_bind
        alembic_op_module.get_bind = lambda: conn
        try:
            upgrade()
        finally:
            alembic_op_module.get_bind = orig

    with sync_db.connect() as conn:
        state = _get_state(conn, company_id, entity_id)

    assert state == {"sku": "NORMAL-001", "cost_price": 500.0, "retail_price": 750.0}


def test_handles_retail_price_type(sync_db):
    """price_type=retail_price is also promoted correctly."""
    company_id = str(uuid.uuid4())
    entity_id = f"item:demo-{uuid.uuid4()}"
    with sync_db.begin() as conn:
        _insert_item(conn, company_id, entity_id, {
            "sku": "DEMO-003",
            "price_type": "retail_price",
            "new_price": 1200.0,
        })

    from celerp.migrations.versions.o9p0q1r2s3t4_backfill_item_price_fields import upgrade
    with sync_db.begin() as conn:
        import alembic.op as alembic_op_module
        orig = alembic_op_module.get_bind
        alembic_op_module.get_bind = lambda: conn
        try:
            upgrade()
        finally:
            alembic_op_module.get_bind = orig

    with sync_db.connect() as conn:
        state = _get_state(conn, company_id, entity_id)

    assert state["retail_price"] == 1200.0
