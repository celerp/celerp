# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The models and the migrations describe the same columns and indexes.

An install and an upgrade build core tables from the migrations, while the test
suite builds them from the models (``Base.metadata.create_all``). Where the two
disagree on whether a column may be null or has a default, a write that works in
one fails in the other: a NOT NULL column whose migration gives it a default but
whose model does not refuses a plain INSERT on the suite's database, and the
reverse refuses it in production. Every column of every table the migrations
create must agree with its model on both.

Where they disagree on an index, the suite reads through an index an upgraded
install does not have, or has under the right name on the wrong columns, and no
test sees the install fall back to scanning the table. Every index of every table
both build must have the same definition on both: its columns or expressions, its
uniqueness and its where clause."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from alembic_support import apply_migrations as _apply_migrations
from celerp.models.base import Base

from .conftest import throwaway_db


def _schema(sync_url: str) -> tuple[dict, dict]:
    """(columns, indexes) of the database: each column's (nullable, has a default) and each
    index's definition, keyed by (table, column or index name)."""
    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            columns = conn.execute(text(
                "SELECT table_name, column_name, is_nullable, column_default IS NOT NULL "
                "FROM information_schema.columns WHERE table_schema = 'public'")).all()
            indexes = conn.execute(text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")).all()
    finally:
        eng.dispose()
    return ({(t, c): (nullable, has_default) for t, c, nullable, has_default in columns},
            {(t, i): d.replace("public.", "") for t, i, d in indexes})


@pytest.fixture(scope="module")
def schemas() -> tuple[tuple[dict, dict], tuple[dict, dict]]:
    """The schema every migration builds, and the one the models build."""
    with throwaway_db("migcols") as (async_url, sync_url):
        _apply_migrations(async_url)
        migrated = _schema(sync_url)
    with throwaway_db("modelcols") as (_, sync_url):
        eng = create_engine(sync_url)
        try:
            Base.metadata.create_all(eng)
        finally:
            eng.dispose()
        modelled = _schema(sync_url)
    return migrated, modelled


def test_every_migrated_column_matches_its_model(schemas):
    (migrated, _), (modelled, _) = schemas
    drift = sorted(
        (table, column, migrated[table, column], modelled[table, column])
        for table, column in migrated.keys() & modelled.keys()
        if migrated[table, column] != modelled[table, column]
    )
    assert drift == [], drift


# Indexes of a table both build that only one side has. A unique column the model declares
# gets Postgres's <table>_<column>_key while the migration named its constraint, and the rest
# are indexes the migrations build that the models do not declare. The list only shrinks:
# a new index goes into the migration and the model alike.
_ONE_SIDED = {
    ("companies", "companies_slug_key"), ("companies", "uq_company_slug"),
    ("users", "users_email_key"), ("users", "uq_user_email"),
    ("companies", "ix_companies_is_active"),
    ("projections", "ix_projections_company_entity_type"), ("projections", "ix_projections_connector_key"),
    ("session_registry", "ix_session_registry_expiry"), ("session_registry", "ix_session_registry_user_id"),
}


def test_every_migrated_index_matches_its_model(schemas):
    (_, migrated), (_, modelled) = schemas
    both = {table for table, _ in migrated} & {table for table, _ in modelled}
    differ = sorted((key, migrated[key], modelled[key]) for key in migrated.keys() & modelled.keys()
                    if migrated[key] != modelled[key])
    one_sided = {key for key in migrated.keys() ^ modelled.keys() if key[0] in both}
    assert differ == [], differ
    assert sorted(one_sided - _ONE_SIDED) == [] and sorted(_ONE_SIDED - one_sided) == [], (
        sorted(one_sided - _ONE_SIDED), sorted(_ONE_SIDED - one_sided))
