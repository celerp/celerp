# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The write observer (mutation_observer.py) on real PostgreSQL, without business routes.

Each control serves a synthetic request, writes company data one way, and checks what
the observer judged: a write without the company lock, after a narrower lock, for another
company, or under a lock that a savepoint rollback or an earlier transaction released is
reported with its diagnostics; a write under the lock, a read, a write that matched
nothing and a write that was rolled back are not; SQL the observer cannot classify is
flagged, never taken as a read."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text, update

import mutation_observer
from mutation_observer import serving
from celerp.events.engine import emit_event
from celerp.models.company import Company, Location, User
from celerp.models.projections import Projection
from celerp.services.company_lock import lock_chart, lock_company
from celerp.services.permissions import authorize_request

pytestmark = pytest.mark.asyncio


def _kinds() -> list[str]:
    return [f.kind for f in mutation_observer.CENSUS.findings]


def _authorize(session, auth) -> uuid.UUID:
    authorize_request(session, auth["company_id"], auth["user_id"], "admin")
    return auth["company_id"]


async def _event(session, cid) -> None:
    await emit_event(session, company_id=cid, entity_id=f"contact:{uuid.uuid4()}", entity_type="contact",
                     event_type="crm.contact.created", data={"name": "Observed", "contact_type": "both"},
                     actor_id=None, location_id=None, source="test",
                     idempotency_key=f"test:{uuid.uuid4()}", metadata_={})


async def test_orm_insert_without_the_lock_is_reported(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        session.add(Location(company_id=cid, name="Unlocked", type="warehouse"))
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert (finding.kind, finding.table, finding.company, finding.route) == (
        "unprotected", "locations", str(cid), "POST /observed")
    assert finding.transaction and finding.sql.startswith("INSERT")


async def test_autoflush_write_without_the_lock_is_reported(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        session.add(Location(company_id=cid, name="Autoflushed", type="warehouse"))
        await session.execute(select(Location.id).where(Location.company_id == cid))
        await session.commit()
    assert _kinds() == ["unprotected"]


async def test_core_update_without_the_lock_is_reported(session, auth):
    with serving("PATCH", "/observed"):
        cid = _authorize(session, auth)
        await session.execute(update(Company).where(Company.id == cid).values(name="Core"))
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert (finding.kind, finding.table, finding.company) == ("unprotected", "companies", str(cid))


async def test_event_without_the_lock_is_reported_where_it_was_written(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await _event(session, cid)
        await session.commit()
    assert set(_kinds()) == {"unprotected"}
    assert any(w.startswith("celerp/events/engine.py") for f in mutation_observer.CENSUS.findings for w in f.where)


async def test_lock_of_one_company_does_not_cover_another(session, auth):
    other = uuid.uuid4()
    session.add(Company(id=other, name="Other", slug=f"other-{other.hex[:8]}", settings={}))
    await session.commit()
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        session.add(Location(company_id=other, name="Elsewhere", type="warehouse"))
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert (finding.kind, finding.company) == ("cross-company", str(other))


@pytest.mark.parametrize("narrower", ["chart", "projection"])
async def test_a_narrower_lock_before_the_company_lock_is_reported(session, auth, narrower):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        if narrower == "chart":
            await lock_chart(session, cid)
        else:
            await session.execute(select(Projection.entity_id).where(Projection.company_id == cid).with_for_update())
        await lock_company(session, cid)
        await _event(session, cid)
        await session.commit()
    assert set(_kinds()) == {"lock-order"}
    assert all(f.earlier_locks for f in mutation_observer.CENSUS.findings)


async def test_a_lock_rolled_back_with_its_savepoint_protects_nothing_after(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        nested = await session.begin_nested()
        await lock_company(session, cid)
        await nested.rollback()
        await _event(session, cid)
        await session.commit()
    assert set(_kinds()) == {"unprotected"}


async def test_the_previous_transactions_lock_is_not_inherited(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        await session.commit()
        await _event(session, cid)
        await session.commit()
    assert set(_kinds()) == {"unprotected"}


async def test_a_savepoint_under_a_held_lock_is_protected(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        nested = await session.begin_nested()
        await session.execute(select(1))
        await nested.rollback()
        async with session.begin_nested():
            await _event(session, cid)
        await session.commit()
    assert _kinds() == []
    assert mutation_observer.CENSUS.routes["POST /observed"]["protected"] >= 1


async def test_a_locked_write_is_protected(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        await _event(session, cid)
        await session.commit()
    assert _kinds() == []
    assert set(mutation_observer.CENSUS.routes["POST /observed"]) == {"protected"}


async def test_reads_and_writes_that_change_nothing_are_not_writes(session, auth):
    with serving("GET", "/observed"):
        cid = _authorize(session, auth)
        await session.execute(select(Company.name).where(Company.id == cid))
        await session.execute(update(Location).where(Location.company_id == cid, Location.name == "none").values(name="x"))
        await session.commit()
    assert _kinds() == [] and mutation_observer.CENSUS.routes == {}


async def test_a_write_rolled_back_never_counts(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        nested = await session.begin_nested()
        await _event(session, cid)
        await nested.rollback()
        await session.commit()
    assert _kinds() == []


async def test_writes_outside_a_request_are_not_judged(session, auth):
    cid = auth["company_id"]
    await _event(session, cid)
    await session.commit()
    assert _kinds() == [] and mutation_observer.CENSUS.routes == {}


@pytest.mark.parametrize("form", ["text", "function", "writable-cte"])
async def test_sql_it_cannot_classify_is_flagged_never_read(session, auth, form):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        if form == "text":
            await session.execute(text("UPDATE companies SET name = 'Raw' WHERE id = :i"), {"i": cid})
        elif form == "function":
            await session.execute(select(func.pg_notify("observed", "x")))
        else:
            written = update(Company).where(Company.id == cid).values(name="CTE").returning(Company.id).cte()
            await session.execute(select(written.c.id))
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert finding.kind == "unknown-sql" and finding.detail


async def test_a_table_without_company_data_must_be_named(session, auth):
    with serving("PATCH", "/observed"):
        _authorize(session, auth)
        await session.execute(update(User).where(User.id == auth["user_id"]).values(name="Renamed"))
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert (finding.kind, finding.table) == ("unowned-table", "users")
