# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The models and the migrations describe the same columns.

An install and an upgrade build core tables from the migrations, while the test
suite builds them from the models (``Base.metadata.create_all``). Where the two
disagree on whether a column may be null or has a default, a write that works in
one fails in the other: a NOT NULL column whose migration gives it a default but
whose model does not refuses a plain INSERT on the suite's database, and the
reverse refuses it in production. Every column of every table the migrations
create must agree with its model on both."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from celerp.cli import _apply_migrations
from celerp.models.base import Base

from .conftest import throwaway_db


def _columns(sync_url: str) -> dict[tuple[str, str], tuple[str, bool]]:
    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(
                "SELECT table_name, column_name, is_nullable, column_default IS NOT NULL "
                "FROM information_schema.columns WHERE table_schema = 'public'")).all()
    finally:
        eng.dispose()
    return {(t, c): (nullable, has_default) for t, c, nullable, has_default in rows}


def test_every_migrated_column_matches_its_model():
    with throwaway_db("migcols") as (async_url, sync_url):
        _apply_migrations(async_url)
        migrated = _columns(sync_url)
    with throwaway_db("modelcols") as (_, sync_url):
        eng = create_engine(sync_url)
        try:
            Base.metadata.create_all(eng)
        finally:
            eng.dispose()
        modelled = _columns(sync_url)
    drift = sorted(
        (table, column, migrated[table, column], modelled[table, column])
        for table, column in migrated.keys() & modelled.keys()
        if migrated[table, column] != modelled[table, column]
    )
    assert drift == [], drift
