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

import inspect
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select, text, update

import mutation_observer
import mutation_read_notes
from mutation_observer import serving
from celerp.connectors.ownership import lock_connector_key
from celerp.credentials import issue_token_pair
from celerp.events.engine import emit_event
from celerp.models.auth import SessionRegistry
from celerp.models.company import Company, Location, User
from celerp.models.migration import MigrationCleanupTask, MigrationRun
from celerp.models.projections import Projection
from celerp.services.company_lock import (
    judge_without_company_lock, lock_chart, lock_company, lock_company_for_deletion)
from celerp.services.migrations import request_cancel, run_cleanup_task
from celerp.services.permissions import authorize_request
from celerp.services.session_tracker import invalidate_sessions, lock_auth_state, register_token

pytestmark = [pytest.mark.asyncio, pytest.mark.observer_control]


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


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
async def test_a_locked_write_made_for_a_read_request_is_reported(session, auth, method):
    with serving(method, "/observed"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        session.add(Location(company_id=cid, name="Read", type="warehouse"))
        await session.commit()
    assert _kinds() == ([] if method == "POST" else ["read-route-write"])


@pytest.mark.parametrize("outer, inner", [("GET", "POST"), ("POST", "GET"), ("POST", "POST")])
async def test_a_write_made_for_a_read_request_through_another_request_is_reported(session, auth, outer, inner):
    """A page load that calls the API: the outermost read on the way is the request the
    write is made for, however the inner request is sent."""
    with serving(outer, "/page"), serving(inner, "/api"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        session.add(Location(company_id=cid, name="Nested", type="warehouse"))
        await session.commit()
    routes = [f.route for f in mutation_observer.CENSUS.findings if f.kind == "read-route-write"]
    assert routes == {"GET": ["GET /page -> POST /api"], "POST": []}[outer] + (
        ["GET /api"] if outer == "POST" and inner == "GET" else [])


async def test_a_durable_write_counts_for_every_request_it_is_made_for(session, auth):
    """A UI route that writes by calling the API: the census counts the write for it as well."""
    with serving("POST", "/page"), serving("POST", "/api"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        session.add(Location(company_id=cid, name="Through", type="warehouse"))
        await session.commit()
    routes = mutation_observer.CENSUS.routes
    assert routes["POST /api"].get("protected") and routes["POST /page"].get("writes through")
    assert "protected" not in routes["POST /page"] and "writes through" not in routes["POST /api"]


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_a_write_to_infrastructure_made_for_a_read_request_is_reported(session, auth, method):
    """A table no company owns (a login's supporter badge) is not judged by the company
    lock, and still a read request may not write it."""
    from celerp.models.supporter import SupporterBadge
    with serving(method, "/observed"):
        session.add(SupporterBadge(user_id=auth["user_id"], github_login="observed", tier="supporter",
                                   ordinal=1, credential="x"))
        await session.commit()
    assert [(f.kind, f.table) for f in mutation_observer.CENSUS.findings] == (
        [("read-route-write", "supporter_badges")] if method == "GET" else [])


async def test_a_ui_request_names_itself_to_the_api_it_calls_over_http(session, auth):
    """Over HTTP the API request names the UI request it is made for (outgoing_headers),
    and the observer judges its writes as that request's."""
    seen = {}

    async def api(scope, receive, send):
        seen["read_route"] = mutation_observer._read_route(mutation_observer._REQUEST.get())

    async def ui(scope, receive, send):
        headers = [(k.encode(), v.encode()) for k, v in mutation_observer.outgoing_headers().items()]
        token = mutation_observer._REQUEST.set(None)  # another task: nothing carries over but the header
        try:
            await mutation_observer.observed(api, "api")(
                {"type": "http", "method": "POST", "path": "/lists", "headers": headers}, receive, send)
        finally:
            mutation_observer._REQUEST.reset(token)

    await mutation_observer.observed(ui, "ui")({"type": "http", "method": "GET", "path": "/lists/new"}, None, None)
    assert seen["read_route"] == "ui GET /lists/new -> api POST /lists"


async def test_every_http_request_the_apps_make_names_the_request_it_is_made_for(session, auth):
    """The suite's hook sends the header from any httpx client, not only the UI's API client."""
    sent = []
    transport = httpx.MockTransport(lambda request: sent.append(request.headers) or httpx.Response(204))

    async def ui(scope, receive, send):
        async with httpx.AsyncClient(transport=transport) as c:
            await c.post("http://api/api/labels/templates")

    await mutation_observer.observed(ui, "ui")({"type": "http", "method": "GET", "path": "/labels"}, None, None)
    async with httpx.AsyncClient(transport=transport) as c:
        await c.get("http://api/outside")
    assert mutation_observer.OUTER_HEADER in sent[0] and mutation_observer.OUTER_HEADER not in sent[1]


async def test_a_read_request_that_writes_nothing_durable_is_not_reported(session, auth):
    with serving("GET", "/observed"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        session.add(Location(company_id=cid, name="Rolled back", type="warehouse"))
        await session.flush()
        await session.rollback()
    assert _kinds() == []


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


@pytest.mark.parametrize("narrower", ["advisory", "projection"])
async def test_a_narrower_lock_before_the_company_lock_is_reported(session, auth, narrower):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        if narrower == "advisory":
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                                  {"k": f"external-link:{cid}"})
        else:
            await session.execute(select(Projection.entity_id).where(Projection.company_id == cid).with_for_update())
        await lock_company(session, cid)
        await _event(session, cid)
        await session.commit()
    assert set(_kinds()) == {"lock-order"}
    assert all(f.earlier_locks for f in mutation_observer.CENSUS.findings)


@pytest.mark.parametrize("taken_under", ["written", "deleting", "another"])
async def test_a_narrower_lock_is_judged_against_the_lock_of_the_company_written(session, auth, taken_under):
    """A narrower lock taken under the written company's lock, or its deletion lock, is in
    order, even when the request's own company is not locked; one taken under another
    company's lock and before the written company's is not."""
    written = uuid.uuid4()
    session.add(Company(id=written, name="Written", slug=f"written-{written.hex[:8]}", settings={}))
    await session.commit()
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        if taken_under == "deleting":
            assert await lock_company_for_deletion(session, written)
        else:
            await lock_company(session, written if taken_under == "written" else cid)
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"narrower:{cid}"})
        await lock_company(session, written)
        await _event(session, written)
        await session.commit()
    assert set(_kinds()) == ({"lock-order"} if taken_under == "another" else set())


@pytest.mark.parametrize("exclusive", [False, True])
async def test_the_connector_fences_rank_before_the_company_lock(session, auth, exclusive):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await lock_connector_key(session, "shopify", exclusive=exclusive)
        await lock_company(session, cid)
        await _event(session, cid)
        await session.commit()
    assert _kinds() == []


@pytest.mark.parametrize("taken,reported", [("schema", False), ("claim", False), ("names", True)])
async def test_the_schema_key_and_a_restore_claim_rank_before_the_company_lock(session, auth, taken, reported):
    from celerp.db import share_schema
    from celerp.services.company_backup import CLAIM
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        if taken == "schema":
            assert await share_schema(session)
        else:
            await session.execute(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                                  {"k": f"{CLAIM}{uuid.uuid4()}" if taken == "claim" else f"restore-names:{cid}"})
        await lock_company(session, cid)
        await _event(session, cid)
        await session.commit()
    assert set(_kinds()) == ({"lock-order"} if reported else set())


async def test_a_connector_fence_after_the_company_lock_is_reported(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        await lock_connector_key(session, "shopify")
        await _event(session, cid)
        await session.commit()
    assert set(_kinds()) == {"lock-order"}
    assert {f.detail for f in mutation_observer.CENSUS.findings} == {
        "connector-runtime after the company lock", "connector-owner:shopify after the company lock"}


@pytest.mark.parametrize("no_key", [False, True])
async def test_a_login_locked_for_update_under_the_company_lock_is_reported(session, auth, no_key):
    with serving("PATCH", "/observed"):
        cid = _authorize(session, auth)
        await lock_company(session, cid)
        await session.execute(select(User.id).where(User.id == auth["user_id"]).with_for_update(key_share=no_key))
        await _event(session, cid)
        await session.commit()
    assert [(f.kind, f.detail) for f in mutation_observer.CENSUS.findings] == (
        [] if no_key else [("lock-order", "a login locked FOR UPDATE under the company lock")])


async def test_an_ownership_fence_without_the_runtime_fence_is_reported(session, auth):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await session.execute(text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:k, 0))"),
                              {"k": "connector-owner:shopify"})
        await lock_company(session, cid)
        await _event(session, cid)
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert (finding.kind, finding.detail) == ("lock-order", "connector-owner:shopify before connector-runtime")


@pytest.mark.parametrize("first,again", [(True, True), (True, False), (False, False)])
async def test_a_connector_fence_taken_again_in_a_mode_held_is_re_entry(session, auth, first, again):
    """A transaction never waits on a lock it holds in that mode or a stronger one, so
    taking it again under the company lock cannot deadlock."""
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await lock_connector_key(session, "shopify", exclusive=first)
        await lock_company(session, cid)
        await lock_connector_key(session, "shopify", exclusive=again)
        await _event(session, cid)
        await session.commit()
    assert _kinds() == []


async def test_a_connector_fence_made_exclusive_under_the_company_lock_is_reported(session, auth):
    """Shared to exclusive waits for every other holder while the company lock is held."""
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await lock_connector_key(session, "shopify")
        await lock_company(session, cid)
        await lock_connector_key(session, "shopify", exclusive=True)
        await _event(session, cid)
        await session.commit()
    finding, = mutation_observer.CENSUS.findings
    assert (finding.kind, finding.detail) == ("lock-order", "connector-owner:shopify after the company lock")


async def _run_row(session, company: str) -> None:
    from celerp.models.sync_run import SyncRun
    await session.execute(insert(SyncRun).values(company_id=company, connector="shopify", entity="reset",
                                                 direction="inbound", started_at=func.now(),
                                                 status="disconnected"))


@pytest.mark.parametrize("exclusive", [True, False])
async def test_the_unassigned_connectors_rows_need_its_exclusive_ownership_fence(session, auth, exclusive):
    """A connector set up before companies had their own is kept under the install id,
    which is no company: its exclusive ownership fence is the lock that protects it."""
    from celerp.config import ensure_instance_id
    with serving("DELETE", "/observed"):
        _authorize(session, auth)
        await lock_connector_key(session, "shopify", exclusive=exclusive)
        await _run_row(session, ensure_instance_id())
        await session.commit()
    assert _kinds() == ([] if exclusive else ["cross-company"])


async def test_the_exclusive_ownership_fence_protects_no_companys_rows(session, auth):
    with serving("DELETE", "/observed"):
        cid = _authorize(session, auth)
        await lock_connector_key(session, "shopify", exclusive=True)
        await _run_row(session, str(cid))
        await session.commit()
    assert _kinds() == ["unprotected"]


@pytest.mark.parametrize("key", ["connector-runtime:shopify", "connector-sync:shopify", "connector-ownership"])
async def test_only_the_two_connector_fences_rank_before_the_company_lock(session, auth, key):
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})
        await lock_company(session, cid)
        await _event(session, cid)
        await session.commit()
    assert set(_kinds()) == {"lock-order"}


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


async def _migration_run(session, auth, status: str = "ready") -> uuid.UUID:
    run_id = uuid.uuid4()
    session.add(MigrationRun(id=run_id, company_id=auth["company_id"], created_by_user_id=auth["user_id"],
                             scan_claim_sha256=run_id.hex * 2, source_system="observed",
                             source_artifact_sha256="0" * 64, adapter_version="1", cif_version="1", mode="new",
                             status=status))
    await session.commit()
    return run_id


@pytest.mark.parametrize("judged", [True, False])
async def test_a_cancel_is_protected_by_the_request_judged_again_without_the_lock(session, auth, judged):
    run_id = await _migration_run(session, auth, "running")
    with serving("POST", "/observed"):
        _authorize(session, auth)
        if judged:
            await request_cancel(session, await session.get(MigrationRun, run_id))
        else:
            await session.execute(update(MigrationRun).where(MigrationRun.id == run_id).values(status="cancelled"))
            await session.commit()
    assert _kinds() == ([] if judged else ["unprotected"])


async def test_a_judgment_without_the_lock_protects_only_the_cancel_that_judged(session, auth):
    run_id = await _migration_run(session, auth, "running")
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await judge_without_company_lock(session, cid)
        await session.execute(update(MigrationRun).where(MigrationRun.id == run_id).values(status="cancelled"))
        await session.commit()
    assert _kinds() == ["unprotected"]


@pytest.mark.parametrize("write", ["another table", "another statement"])
async def test_a_judgment_without_the_lock_protects_only_the_named_write(session, auth, write):
    run_id = await _migration_run(session, auth)
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        await judge_without_company_lock(session, cid)
        if write == "another table":
            await session.execute(update(Company).where(Company.id == cid).values(name="Judged"))
        else:
            await session.execute(delete(MigrationRun).where(MigrationRun.id == run_id))
        await session.commit()
    assert _kinds() == ["unprotected"]


@pytest.mark.parametrize("columns, by, path", [("every", "named", "/observed"), ("one", "named", "/observed"),
                                               ("every", "another", "/observed"), ("every", "named", "/elsewhere")])
async def test_a_read_request_may_make_only_the_note_it_is_allowed(session, auth, monkeypatch, columns, by, path):
    """A note READ_ROUTE_WRITES allows a read request is its columns only, written by its
    functions only, for its route only: a further column, the same write from elsewhere,
    or from another read route, is reported."""
    allowed = tuple(c.name for c in MigrationRun.__table__.columns) if columns == "every" else ("cancel_requested_at",)
    monkeypatch.setitem(mutation_read_notes.READ_ROUTE_WRITES, ("migration_runs", "UPDATE"), mutation_read_notes.ReadNote(
        allowed, ("celerp/services/migrations.py request_cancel",), "a note made while reading", route="GET /observed"))
    run_id = await _migration_run(session, auth, "running")
    with serving("GET", path):
        _authorize(session, auth)
        if by == "named":
            await request_cancel(session, await session.get(MigrationRun, run_id))
        else:
            await judge_without_company_lock(session, auth["company_id"])
            await session.execute(update(MigrationRun).where(MigrationRun.id == run_id).values(status="cancelled"))
            await session.commit()
    expected = ["read-route-write"] if (columns, by, path) != ("every", "named", "/observed") else []
    assert [k for k in _kinds() if k != "unprotected"] == expected


async def test_a_judgment_without_the_lock_that_refuses_writes_nothing(session, auth):
    run_id = await _migration_run(session, auth)
    with serving("POST", "/observed"):
        cid = auth["company_id"]
        authorize_request(session, cid, auth["user_id"], "viewer")
        with pytest.raises(HTTPException):
            await judge_without_company_lock(session, cid)
        await session.execute(update(MigrationRun).where(MigrationRun.id == run_id).values(status="cancelled"))
        with pytest.raises(HTTPException):
            await session.commit()
        await session.rollback()
    assert _kinds() == []


@pytest.mark.parametrize("deleter", ["the cleanup sweep", "row locked elsewhere", "unlocked"])
async def test_a_cleanup_task_is_deleted_under_its_own_row_lock_by_the_sweep_only(session, auth, deleter):
    task = MigrationCleanupTask(company_id=auth["company_id"], run_ids=[])
    session.add(task)
    await session.commit()
    with serving("POST", "/observed"):
        _authorize(session, auth)
        if deleter == "the cleanup sweep":
            assert await run_cleanup_task(session, task.id)
        else:
            query = select(MigrationCleanupTask.id).where(MigrationCleanupTask.id == task.id)
            await session.execute(query.with_for_update() if deleter == "row locked elsewhere" else query)
            await session.execute(delete(MigrationCleanupTask).where(MigrationCleanupTask.id == task.id))
            await session.commit()
    assert _kinds() == ([] if deleter == "the cleanup sweep" else ["unprotected"])


async def test_a_session_token_is_issued_and_ended_under_the_logins_session_state(session, auth):
    user = await session.get(User, auth["user_id"])
    with serving("POST", "/observed"):
        await issue_token_pair(session, user=user, company_id=auth["company_id"], expected_snonce=None)
        await invalidate_sessions(session, str(auth["user_id"]))
    assert _kinds() == []
    assert (await session.scalar(select(func.count()).select_from(SessionRegistry))) == 0


@pytest.mark.parametrize("slot", ["registered", "gone"])
async def test_a_read_requests_token_refresh_slides_or_registers_its_session(session, auth, slot):
    """The middleware refreshes the token of any request, a read too: issuance slides the
    session's expiry forward, or registers the session again when its slot is gone."""
    user = await session.get(User, auth["user_id"])
    jti = uuid.uuid4().hex
    if slot == "registered":
        session.add(SessionRegistry(jti=jti, user_id=auth["user_id"], company_id=auth["company_id"],
                                    expiry=datetime.now(timezone.utc) + timedelta(minutes=5)))
        await session.commit()
    with serving("GET", "/observed"):
        await issue_token_pair(session, user=user, company_id=auth["company_id"], jti=jti, expected_snonce=None)
    assert _kinds() == []
    assert (await session.get(SessionRegistry, jti)).expiry > datetime.now(timezone.utc) + timedelta(minutes=5)
    # The add counts for site coverage only when it ran, though both send their row at the commit.
    lines, first = inspect.getsourcelines(register_token)
    added = first + next(n for n, line in enumerate(lines) if "session.add(" in line)
    site = f"celerp/services/session_tracker.py:{added} register_token@{first}"
    assert (site in mutation_observer.CENSUS.sites) == (slot == "gone")


async def test_a_token_refresh_does_not_show_the_route_it_rides_on_writes(session, auth, monkeypatch):
    """The middleware refreshes a token past half its life on whichever request comes then,
    so its write does not show that route, or the page it is made for, writes; a route that
    issues a token itself does."""
    from contextlib import asynccontextmanager
    import celerp.middleware as middleware

    @asynccontextmanager
    async def this_session():
        yield session

    user = await session.get(User, auth["user_id"])
    with serving("POST", "/sign-in"):
        pair = await issue_token_pair(session, user=user, company_id=auth["company_id"], expected_snonce=None)
    monkeypatch.setattr(middleware, "get_session_ctx", this_session)
    monkeypatch.setattr(middleware, "_past_half_life", lambda exp: True)
    with serving("POST", "/page"), serving("POST", "/api"):
        assert await middleware._refresh_bearer_validated(pair["access_token"])
    routes = mutation_observer.CENSUS.routes
    assert routes["POST /sign-in"].get("protected")
    assert routes["POST /api"].get("made for any request")
    assert not {"protected", "writes through"} & (set(routes["POST /api"]) | set(routes.get("POST /page", {})))
    assert _kinds() == []


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.parametrize("state_locked", [True, False])
async def test_a_session_token_written_outside_issuance_is_reported(session, auth, state_locked, method):
    with serving(method, "/observed"):
        if state_locked:
            await lock_auth_state(session, str(auth["user_id"]))
        session.add(SessionRegistry(jti=uuid.uuid4().hex, user_id=auth["user_id"], company_id=auth["company_id"],
                                    expiry=datetime.now(timezone.utc) + timedelta(hours=1)))
        await session.commit()
    assert sorted(_kinds()) == (["read-route-write", "unprotected"] if method == "GET" else ["unprotected"])


@pytest.mark.parametrize("created", ["in this transaction", "earlier in this request", "before this request"])
async def test_a_company_is_written_unlocked_only_inside_the_transaction_that_creates_it(session, auth, created):
    cid = uuid.uuid4()
    if created == "before this request":
        session.add(Company(id=cid, name="Made before", slug=f"made-{cid.hex[:8]}"))
        await session.commit()
    with serving("POST", "/observed"):
        if created != "before this request":
            session.add(Company(id=cid, name="Made here", slug=f"made-{cid.hex[:8]}"))
            await (session.flush() if created == "in this transaction" else session.commit())
        session.add(Location(company_id=cid, name="Seeded", type="warehouse"))
        await session.commit()
    assert _kinds() == ([] if created == "in this transaction" else ["unprotected"])


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


async def test_a_sync_drivers_statements_are_classified_with_their_parameters(session, auth):
    """psycopg2 sends %(name)s and %s placeholders and %% for a literal percent sign, which
    are no PostgreSQL syntax: they are read as parameters, so its writes and reads are
    judged as the asyncpg driver's are, never as unparsable SQL."""
    from sqlalchemy import create_engine
    from test_helpers import DATABASE_URL
    engine = create_engine(DATABASE_URL.replace("+asyncpg", "+psycopg2"))
    try:
        with serving("POST", "/observed"), engine.connect() as conn:
            conn.exec_driver_sql("SELECT id FROM companies WHERE name LIKE 'a%%' AND slug = %(s)s", {"s": "x"})
            conn.exec_driver_sql("SELECT id FROM companies WHERE slug = %s", ("x",))
            conn.execute(text("SELECT 1 FROM companies WHERE slug = :s"), {"s": "x"})
            conn.exec_driver_sql(
                "INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                "VALUES (%(i)s, '100%%', %(i)s, '{}', true, now())", {"i": str(uuid.uuid4())})
            conn.rollback()
    finally:
        engine.dispose()
    assert [(f.kind, f.table, f.detail) for f in mutation_observer.CENSUS.findings] == \
        [("no-session", "companies", "INSERT")]


@pytest.mark.parametrize("migrating", ["held", "released", "other key", "with a session"])
async def test_the_migrators_writes_are_its_own_only_under_the_migration_lock(session, auth, migrating):
    """A System Recovery runs the schema migrator inside its request: the migrator's writes on
    its own connections, made while the request holds the migration lock, are the
    migrator's; a write once the lock is released, under another lock, or on a Session is
    judged as usual."""
    from sqlalchemy import create_engine
    from celerp.db import _MIGRATION_LOCK_KEY
    from test_helpers import DATABASE_URL
    key = _MIGRATION_LOCK_KEY if migrating != "other key" else _MIGRATION_LOCK_KEY + 1
    # The migration lock is a session lock, held whatever the transaction does; the insert
    # runs in a transaction this test rolls back, so it leaves no company for later tests.
    engine = create_engine(DATABASE_URL.replace("+asyncpg", "+psycopg2"))
    try:
        with serving("POST", "/observed"), engine.connect() as lock_conn, engine.connect() as conn:
            lock_conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            if migrating == "released":
                lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            if migrating == "with a session":
                cid = _authorize(session, auth)
                await session.execute(update(Company).where(Company.id == cid).values(name="Migrating"))
                await session.commit()
            else:
                with conn.begin() as tx:
                    conn.execute(text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                                      "VALUES (:i, 'Migrated', :i, '{}', true, now())"), {"i": str(uuid.uuid4())})
                    tx.rollback()
            if migrating != "released":
                lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    finally:
        engine.dispose()
    expected = {"held": [], "with a session": [("unprotected", "companies")]}.get(migrating, [("no-session", "companies")])
    assert [(f.kind, f.table) for f in mutation_observer.CENSUS.findings] == expected


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


async def test_another_clients_edit_staged_in_a_request_is_not_the_requests(session, auth):
    """An edit a test stages as another client's is left out; the request's own edit after
    it is still judged."""
    with serving("POST", "/observed"):
        cid = _authorize(session, auth)
        with mutation_observer.another_client():
            await session.execute(insert(Location).values(company_id=cid, name="Elsewhere", type="warehouse"))
        session.add(Location(company_id=cid, name="Unlocked", type="warehouse"))
        await session.commit()
    assert [(f.kind, f.table) for f in mutation_observer.CENSUS.findings] == [("unprotected", "locations")]


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


async def test_a_request_starting_meanwhile_leaves_background_work_its_own_edit(session, auth):
    """Work that outlives the request that started it (a migration runner) runs outside
    request scope on its own sessions; a request starting while it holds an unflushed
    edit leaves the edit to it, to flush when it carries on."""
    import asyncio
    import contextvars

    import httpx
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    app, added, served = FastAPI(), asyncio.Event(), asyncio.Event()
    cid = auth["company_id"]

    @app.post("/observed")
    async def endpoint() -> dict:
        return {}

    async def background() -> list:
        mine = AsyncSession(bind=session.bind, join_transaction_mode="create_savepoint")
        await mine.execute(select(1))
        mine.add(Location(company_id=cid, name="Background", type="warehouse"))
        added.set()
        await served.wait()
        pending = list(mine.new)
        await mine.close()
        return pending

    work = contextvars.Context().run(asyncio.get_running_loop().create_task, background())
    await added.wait()
    transport = httpx.ASGITransport(app=mutation_observer.observed(app, "api"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/observed")).status_code == 200
    served.set()
    assert [p.name for p in await work] == ["Background"]
    assert mutation_observer.CENSUS.findings == []
