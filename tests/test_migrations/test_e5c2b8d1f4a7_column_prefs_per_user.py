# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Column setup moves to each user: the company-wide visible-column lists an older version
kept per category are copied into every user's own item list setup once, and a setup a user
already saved survives every replay."""

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
        conn.execute(text("CREATE TABLE user_companies (user_id UUID NOT NULL, company_id UUID NOT NULL)"))
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


def _join(engine, cid) -> str:
    user = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO user_companies (user_id, company_id) VALUES (:u, :c)"), {"u": user, "c": cid})
    return str(user)


_SCHEMA = [{"key": key} for key in ("sku", "name", "cost_price", "notes")]


def test_company_wide_column_lists_become_each_users_item_list_setup(companies_db):
    """The all-items list gives the order and hides every column it left out; a category's
    list decides that category's own columns and adds them to the order."""
    older = _put(companies_db, {
        "column_prefs": {"__all__": ["name", "sku"], "gems": ["sku", "carat"], "rings": ["metal"]},
        "item_schema": _SCHEMA, "currency": "THB",
        "category_schemas": {"gems": [{"key": "carat"}, {"key": "clarity"}], "rings": [{"key": "metal"}]}})
    first, second = _join(companies_db, older), _join(companies_db, older)
    stranger = _join(companies_db, _put(companies_db, {"currency": "USD"}))

    run_migration_ops(companies_db, MODULE)
    run_migration_ops(companies_db, MODULE)

    after = _get(companies_db, older)
    assert set(after["column_prefs"]) == {first, second} and stranger not in after["column_prefs"]
    layout = after["column_prefs"][first]["inventory"]
    assert layout == after["column_prefs"][second]["inventory"]
    assert layout["order"] == ["name", "sku", "carat", "metal"]
    vis = layout["vis"]
    assert (vis["name"], vis["sku"], vis["cost_price"], vis["notes"]) == (True, True, False, False)
    assert (vis["carat"], vis["clarity"], vis["metal"]) == (True, False, True)
    assert after["currency"] == "THB" and after["item_schema"] == _SCHEMA


def test_a_category_list_alone_hides_no_shared_column(companies_db):
    cid = _put(companies_db, {"column_prefs": {"gems": ["carat", "sku"]}, "item_schema": _SCHEMA,
                              "category_schemas": {"gems": [{"key": "carat"}, {"key": "clarity"}]}})
    user = _join(companies_db, cid)
    run_migration_ops(companies_db, MODULE)
    assert _get(companies_db, cid)["column_prefs"] == {user: {"inventory": {
        "order": ["carat", "sku"], "vis": {"carat": True, "clarity": False, "sku": True}}}}


def test_a_setup_a_user_already_saved_stays_and_other_companies_are_untouched(companies_db):
    mine = {"inventory": {"vis": {"sku": False}, "order": ["name", "sku"]}, "customers": {"vis": {"name": True}}}
    cid = _put(companies_db, {"column_prefs": {"__all__": ["sku"]}, "item_schema": _SCHEMA})
    user, colleague = _join(companies_db, cid), _join(companies_db, cid)
    with companies_db.begin() as conn:
        conn.execute(text("UPDATE companies SET settings = CAST(:s AS json) WHERE id = :i"),
                     {"i": cid, "s": json.dumps({"column_prefs": {"__all__": ["sku"], user: mine}, "item_schema": _SCHEMA})})
    untouched = _put(companies_db, {"currency": "USD"})
    nobody = _put(companies_db, {"column_prefs": {"__all__": ["sku"]}})

    run_migration_ops(companies_db, MODULE)
    run_migration_ops(companies_db, MODULE)

    prefs = _get(companies_db, cid)["column_prefs"]
    assert prefs[user] == mine
    assert prefs[colleague]["inventory"]["order"] == ["sku"] and prefs[colleague]["inventory"]["vis"]["name"] is False
    assert _get(companies_db, untouched) == {"currency": "USD"}
    assert _get(companies_db, nobody) == {"column_prefs": {}}


def test_downgrade_keeps_each_users_setup(companies_db):
    cid = _put(companies_db, {"column_prefs": {"__all__": ["sku"]}, "item_schema": _SCHEMA})
    _join(companies_db, cid)
    run_migration_ops(companies_db, MODULE)
    before = _get(companies_db, cid)
    run_migration_ops(companies_db, MODULE, "downgrade")
    assert _get(companies_db, cid) == before
