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
from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select, text, update

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
    assert set(mutation_observer.CENSUS.routes["POST /observed"]) == {"protected", "company from statement"}


async def test_a_write_naming_no_company_is_judged_for_the_requests_company_and_says_so(session, auth):
    cid = auth["company_id"]
    location = Location(company_id=cid, name="Before", type="warehouse")
    session.add(location)
    await session.commit()
    with serving("PATCH", "/observed"):
        _authorize(session, auth)
        await lock_company(session, cid)
        await session.execute(update(Location), [{"id": location.id, "name": "After"}])
        await session.commit()
    assert _kinds() == []
    assert mutation_observer.CENSUS.routes["PATCH /observed"] == {"protected": 1, "company from request": 1}


async def test_a_bulk_insert_is_judged_for_every_company_it_writes(session, auth):
    other = uuid.uuid4()
    session.add(Company(id=other, name="Other", slug=f"other-{other.hex[:8]}", settings={}))
    await session.commit()
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        await session.execute(insert(Location), [
            {"id": uuid.uuid4(), "company_id": c, "name": "Bulk", "type": "warehouse"} for c in (cid, other)])
        await session.commit()
    assert _kinds() == ["cross-company"]
    assert mutation_observer.CENSUS.findings[0].company == str(other)


async def test_a_write_under_a_refused_authority_never_counts(session, auth):
    with serving("POST", "/observed"):
        cid = auth["company_id"]
        authorize_request(session, cid, auth["user_id"], "viewer")
        with pytest.raises(HTTPException):
            await lock_company(session, cid)
        await _event(session, cid)
        with pytest.raises(HTTPException):
            await session.commit()
        await session.rollback()
    assert _kinds() == [] and mutation_observer.CENSUS.routes == {}


async def test_the_previous_requests_lock_is_not_inherited_on_a_reused_session(session, auth):
    with serving("POST", "/first"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        await _event(session, cid)
        await session.commit()
    with serving("POST", "/second"):
        _authorize(session, auth)
        await _event(session, cid)
        await session.commit()
    assert {f.route for f in mutation_observer.CENSUS.findings} == {"POST /second"}
    assert set(_kinds()) == {"unprotected"}


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


@pytest.mark.parametrize("form", ["text", "driver", "writable-cte"])
async def test_raw_sql_and_cte_writes_without_the_lock_are_reported(session, auth, form):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        if form == "text":
            await session.execute(text("UPDATE companies SET name = 'Raw' WHERE id = :i"), {"i": cid})
        elif form == "driver":
            await (await session.connection()).exec_driver_sql(
                f"UPDATE companies SET name = 'Driver' WHERE id = '{cid}'")
        else:
            written = update(Company).where(Company.id == cid).values(name="CTE").returning(Company.id).cte()
            await session.execute(select(written.c.id))
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert (finding.kind, finding.table, finding.company) == ("unprotected", "companies", str(cid))
    assert finding.company_from == ("statement" if form == "writable-cte" else "request")


@pytest.mark.parametrize("form", ["function", "procedural", "explain-analyze"])
async def test_sql_it_cannot_classify_is_flagged_never_read(session, auth, form):
    with serving("POST", "/observed"):
        _authorize(session, auth)
        if form == "function":
            await session.execute(select(func.txid_current()))
        elif form == "procedural":
            await session.execute(text("DO $$ BEGIN PERFORM 1; END $$"))
        else:
            await session.execute(text("EXPLAIN ANALYZE SELECT 1"))
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert finding.kind == "unknown-sql" and finding.detail and finding.fingerprint


async def test_statements_whose_effects_are_reviewed_are_not_flagged(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        await session.execute(text("SET LOCAL lock_timeout = 0"))
        await session.execute(text("SELECT pg_notify('events', :p)"), {"p": "x"})
        await session.execute(text("SELECT id FROM ledger WHERE company_id = CAST(:c AS uuid)"), {"c": cid})
        await session.execute(text("EXPLAIN SELECT 1"))
        await session.execute(select(func.set_config("application_name", "observed", True)))
        await session.execute(select(Location.id).where(Location.company_id == cid).with_for_update(read=True))
        await session.commit()
    assert _kinds() == []


async def test_one_unknown_statement_shape_is_one_finding():
    totals = mutation_observer.Totals()
    for value in ("a", "b"):
        census = mutation_observer.Census(test="t")
        op = mutation_observer.classify(None, f"DO $$ BEGIN RAISE NOTICE '{value}'; END $$")
        census.findings.append(mutation_observer.Finding(
            "unknown-sql", "POST /observed", None, None, "DO", op.detail, fingerprint=op.fingerprint))
        totals.add(census)
    finding, = totals.findings.values()
    assert finding["count"] == 2


async def test_a_table_without_company_data_must_be_named(session, auth):
    await session.execute(text("CREATE TEMP TABLE observed_unnamed (id integer PRIMARY KEY, note text)"))
    with serving("PATCH", "/observed"):
        _authorize(session, auth)
        await session.execute(text("INSERT INTO observed_unnamed VALUES (1, 'x')"))
        await session.execute(update(User).where(User.id == auth["user_id"]).values(name="Renamed"))
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert (finding.kind, finding.table) == ("unowned-table", "observed_unnamed")
    assert "users" in mutation_observer.INFRASTRUCTURE


@pytest.mark.parametrize("shape", ["column", "reference", "partition"])
async def test_a_table_the_tests_make_holds_company_data_as_its_definition_says(session, auth, shape):
    """A table made while the tests run is company data when it has a company_id column,
    references company data, or is a partition of such a table (made in the test's
    transaction, so it is gone after it)."""
    made = {
        "column": ["CREATE TABLE observed_made (id integer PRIMARY KEY, company_id uuid)"],
        "reference": ["CREATE TABLE observed_made (id integer PRIMARY KEY, "
                      "location_id uuid REFERENCES locations(id))"],
        "partition": ["CREATE TABLE observed_parent (id integer, company_id uuid) PARTITION BY LIST (id)",
                      "CREATE TABLE observed_made PARTITION OF observed_parent FOR VALUES IN (1)"],
    }[shape]
    for ddl in made:
        await session.execute(text(ddl))
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await session.execute(text("INSERT INTO observed_made (id) VALUES (1)"))
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert (finding.kind, finding.table, finding.company) == ("unprotected", "observed_made", str(cid))


async def test_a_row_under_company_data_is_company_data(session, auth):
    """A table without a company_id column whose rows hang off company data (a message of
    a company's conversation) is judged as the company's write."""
    from celerp.models.ai import AIConversation, AIMessage
    conversation = AIConversation(company_id=auth["company_id"], user_id=auth["user_id"], title="Observed")
    session.add(conversation)
    await session.commit()
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        session.add(AIMessage(conversation_id=conversation.id, role="user", content="hello"))
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert (finding.kind, finding.table, finding.company) == ("unprotected", "ai_messages", str(cid))


async def test_a_route_of_an_included_router_is_counted_under_the_path_the_inventory_lists(tmp_path):
    import json

    import httpx
    from fastapi import APIRouter, FastAPI

    inner, outer, app = APIRouter(), APIRouter(), FastAPI()

    @inner.get("/things/{thing_id}")
    async def thing(thing_id: str) -> dict:
        return {"id": thing_id}

    outer.include_router(inner, prefix="/in")
    app.include_router(outer, prefix="/pre")
    transport = httpx.ASGITransport(app=mutation_observer.observed(app, "api"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/pre/in/things/5")).status_code == 200
    assert mutation_observer.CENSUS.routes["api GET /pre/in/things/{thing_id}"] == {"requests": 1}
    mutation_observer.write_routes(str(tmp_path / "routes.json"), [app])
    row, = [r for r in json.loads((tmp_path / "routes.json").read_text()) if r["path"].startswith("/pre/")]
    assert row["path"] == "/pre/in/things/{thing_id}" and row["source"].startswith(f"{__file__}:")


@pytest.mark.parametrize("who", ["test", "request"])
async def test_a_change_the_test_left_unflushed_is_not_the_next_requests_write(session, auth, who):
    """A test edits a row on the session it then hands to a request; whichever statement
    of the request flushes that edit, the edit is the test's. The same edit made by the
    request is the request's."""
    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    place = Location(company_id=auth["company_id"], name="Handed over", type="warehouse")
    session.add(place)
    await session.commit()

    @app.post("/observed")
    async def endpoint() -> dict:
        _authorize(session, auth)
        if who == "request":
            place.name = who
        await session.execute(select(Location.id).where(Location.company_id == auth["company_id"]))
        await session.commit()
        return {}

    if who == "test":
        place.name = who
    transport = httpx.ASGITransport(app=mutation_observer.observed(app, "api"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/observed")).status_code == 200
    assert [(f.kind, f.table) for f in mutation_observer.CENSUS.findings] == (
        [] if who == "test" else [("unprotected", "locations")])


async def test_a_request_starting_meanwhile_leaves_another_requests_edit_to_it(session, auth):
    """Settling the sessions a request inherits never reaches into a request being served
    at the same time: its unflushed edit stays its own write."""
    import asyncio

    import httpx
    from fastapi import FastAPI

    app, added, other_began = FastAPI(), asyncio.Event(), asyncio.Event()

    @app.post("/first")
    async def first() -> dict:
        _authorize(session, auth)
        await session.execute(select(Location.id).where(Location.company_id == auth["company_id"]))
        session.add(Location(company_id=auth["company_id"], name="Meanwhile", type="warehouse"))
        added.set()
        await other_began.wait()
        await session.commit()
        return {}

    @app.post("/second")
    async def second() -> dict:
        other_began.set()
        return {}

    transport = httpx.ASGITransport(app=mutation_observer.observed(app, "api"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async def later():
            await added.wait()
            return await client.post("/second")
        replies = await asyncio.gather(client.post("/first"), later())
    assert [r.status_code for r in replies] == [200, 200]
    assert [(f.kind, f.route) for f in mutation_observer.CENSUS.findings] == [("unprotected", "api POST /first")]


async def test_a_cached_statement_is_judged_for_the_company_it_names_now(session, auth):
    """SQLAlchemy reuses the statement it compiled first; the WHERE company is the one
    bound to this execution, not the first execution's."""
    other = uuid.uuid4()
    session.add(Company(id=other, name="Other", slug=f"other-{other.hex[:8]}", settings={}))
    for c in (other, auth["company_id"]):
        session.add(Location(company_id=c, name="Cached", type="warehouse"))
    await session.commit()
    for c in (other, auth["company_id"]):
        with serving("DELETE", "/observed"):
            _authorize(session, auth)
            await lock_company(session, c)
            await session.execute(delete(Location).where(Location.company_id == c, Location.name == "Cached"))
            await session.commit()
    assert _kinds() == []
    assert mutation_observer.CENSUS.routes["DELETE /observed"]["protected"] == 2


async def test_a_narrower_lock_left_by_an_earlier_request_is_not_this_requests(session, auth):
    with serving("GET", "/first"):
        cid = _authorize(session, auth)
        await lock_chart(session, cid)
    with serving("POST", "/second"):
        _authorize(session, auth)
        await lock_company(session, cid)
        await _event(session, cid)
        await session.commit()
    assert _kinds() == []


async def test_a_company_lock_left_by_an_earlier_request_does_not_protect_this_one(session, auth):
    with serving("GET", "/first"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
    with serving("POST", "/second"):
        _authorize(session, auth)
        await _event(session, cid)
        await session.commit()
    assert {(f.kind, f.route) for f in mutation_observer.CENSUS.findings} == {("unprotected", "POST /second")}


async def test_a_write_is_judged_on_the_session_that_ran_it(session, auth):
    """Two sessions on one connection (the tests' maintenance sessions join the test's
    connection): a write is the writing session's, whichever began last."""
    from sqlalchemy.ext.asyncio import AsyncSession
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await session.execute(select(1))
        bystander = AsyncSession(bind=session.bind, join_transaction_mode="create_savepoint")
        await bystander.execute(select(1))
        session.add(Location(company_id=cid, name="Mine", type="warehouse"))
        await session.flush()
        await bystander.close()  # its savepoint is the inner one, so it ends first
        await session.commit()
    assert _kinds() == ["unprotected"]
