# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The one-default-location revision keeps the oldest default of a company that holds
more than one, leaves every other company as it was, and can run again."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from .conftest import run_migration_ops, throwaway_db, upgrade_to

PREVIOUS = "u4v5w6x7y8z9"
REVISION = "v5w6x7y8z9a0"


def _company(conn, name: str) -> uuid.UUID:
    cid = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO companies (id, name, slug, settings, created_at) "
        "VALUES (:id, :n, :s, '{}', now())"), {"id": cid, "n": name, "s": f"{name}-{cid.hex[:6]}"})
    return cid


def _location(conn, cid: uuid.UUID, name: str, is_default: bool, age_days: int) -> uuid.UUID:
    lid = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO locations (id, company_id, name, type, is_default, created_at) "
        "VALUES (:id, :c, :n, 'warehouse', :d, now() - make_interval(days => :a))"),
        {"id": lid, "c": cid, "n": name, "d": is_default, "a": age_days})
    return lid


def _defaults(engine) -> dict[uuid.UUID, set[uuid.UUID]]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT company_id, id FROM locations WHERE is_default")).all()
    found: dict[uuid.UUID, set[uuid.UUID]] = {}
    for cid, lid in rows:
        found.setdefault(cid, set()).add(lid)
    return found


def test_upgrade_keeps_the_oldest_default_and_runs_again():
    with throwaway_db("onedefault") as (_, sync_url):
        upgrade_to(sync_url, PREVIOUS)
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                legacy = _company(conn, "legacy")
                oldest = _location(conn, legacy, "Main", True, 30)
                _location(conn, legacy, "Second", True, 10)
                plain = _location(conn, legacy, "Shelf", False, 40)
                other = _company(conn, "other")
                other_default = _location(conn, other, "Only", True, 5)
            upgrade_to(sync_url, REVISION)
            assert _defaults(engine) == {legacy: {oldest}, other: {other_default}}

            run_migration_ops(engine, f"{REVISION}_one_default_location")
            assert _defaults(engine) == {legacy: {oldest}, other: {other_default}}

            with engine.connect() as conn:
                assert conn.execute(text(
                    "SELECT 1 FROM pg_indexes WHERE indexname = 'uq_location_one_default'")).scalar_one() == 1
                assert conn.execute(text("SELECT is_default FROM locations WHERE id = :i"),
                                    {"i": plain}).scalar_one() is False
            with pytest.raises(Exception, match="uq_location_one_default"), engine.begin() as conn:
                _location(conn, other, "Another", True, 0)
        finally:
            engine.dispose()
