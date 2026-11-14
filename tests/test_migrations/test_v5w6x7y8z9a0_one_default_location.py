# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The one-default-location revision keeps the oldest default of a company that holds
more than one, leaves every other company as it was, and can run again."""

from __future__ import annotations

import json
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


async def _start(async_url: str) -> None:
    """What every startup runs for the cleared defaults: the tables the models add, then the
    notice in its own session."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from celerp.db import create_tables
    from celerp.services.location_default_notice import run_location_default_notice

    engine = create_async_engine(async_url, poolclass=NullPool)
    try:
        await create_tables(engine)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await run_location_default_notice(session)
            await session.commit()
    finally:
        await engine.dispose()


def _notices(engine, cid: uuid.UUID) -> list[tuple[str, str, dict]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT body, action_url, i18n FROM notifications WHERE company_id = :c"), {"c": cid}).all()]


def _runtime(engine) -> dict | None:
    with engine.connect() as conn:
        return conn.execute(text("SELECT value FROM system_runtime_state WHERE id = 1")).scalar()


def test_a_company_whose_defaults_were_cleared_is_told_once():
    """A company that held three defaults keeps the oldest and gets one notice naming the
    location kept and the ones cleared, pointing to Locations; a later start tells it nothing
    more, and a company that held one default is told nothing."""
    import asyncio

    with throwaway_db("onedefnotice") as (async_url, sync_url):
        upgrade_to(sync_url, PREVIOUS)
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                legacy = _company(conn, "legacy")
                _location(conn, legacy, "Main", True, 30)
                _location(conn, legacy, "Second", True, 10)
                _location(conn, legacy, "Third", True, 5)
                _location(conn, legacy, "Shelf", False, 40)
                other = _company(conn, "other")
                _location(conn, other, "Only", True, 5)
            with engine.begin() as conn:
                conn.execute(text("UPDATE system_runtime_state SET value = '{\"star_prompt_dismissed\": true}'"))
            upgrade_to(sync_url, "head")
            asyncio.run(_start(async_url))
            assert _runtime(engine) == {"star_prompt_dismissed": True}
            asyncio.run(_start(async_url))

            told = _notices(engine, legacy)
            assert len(told) == 1, told
            body, action_url, i18n = told[0]
            assert body == ("An update allows one default location per company, so Main stays the "
                            "default. No longer default: Second, Third. To make another location the "
                            "default, set it in Locations.")
            assert action_url == "/settings/inventory?tab=locations"
            assert i18n == {"title": "notice.location_default_cleared.title",
                            "body": "notice.location_default_cleared.body",
                            "params": {"kept": "Main", "cleared": "Second, Third"}}
            assert _notices(engine, other) == []
        finally:
            engine.dispose()


def test_a_company_that_cannot_be_told_stays_recorded_and_a_gone_location_is_not_named():
    """With no runtime state row the revision makes one; a company whose kept location was since
    deleted is told nothing, and an entry that cannot be told is kept for a later start, which
    also runs with no runtime state row at all."""
    import asyncio

    with throwaway_db("onedefgone") as (async_url, sync_url):
        upgrade_to(sync_url, PREVIOUS)
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM system_runtime_state"))
            with engine.begin() as conn:
                gone = _company(conn, "gone")
                kept = _location(conn, gone, "Main", True, 30)
                cleared = _location(conn, gone, "Second", True, 10)
            upgrade_to(sync_url, "head")
            record = {str(gone): {"kept": str(kept), "cleared": [str(cleared)]}}
            assert _runtime(engine) == {"location_default_cleared": record}

            broken = {str(uuid.uuid4()): {"kept": str(uuid.uuid4())}}
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM locations WHERE id = :i"), {"i": kept})
            with engine.begin() as conn:
                conn.execute(text("UPDATE system_runtime_state SET value = CAST(:v AS json)"),
                             {"v": json.dumps({"location_default_cleared": {**record, **broken}})})
            asyncio.run(_start(async_url))
            assert _notices(engine, gone) == []
            assert _runtime(engine) == {"location_default_cleared": broken}

            with engine.begin() as conn:
                conn.execute(text("DELETE FROM system_runtime_state"))
            asyncio.run(_start(async_url))
            assert _runtime(engine) is None
        finally:
            engine.dispose()
