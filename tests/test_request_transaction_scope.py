"""A request run by the test client ends its own transaction, as production does.

Production gives each request a session of its own and closes it at the end, so a
request that never commits holds nothing afterwards. The client shares the test's
session with every request; without the request boundary, a preview's company lock
stayed held into the next request and the lock rank recorder judged that request
by locks it never took."""

from __future__ import annotations

from sqlalchemy import delete, func, select, text, update

from test_cost_restatement import _item


async def test_a_request_that_never_commits_holds_nothing_after_it(client, session, auth):
    sources = [await _item(client, auth, 100.0), await _item(client, auth, 50.0)]

    r = await client.post("/items/merge/preview", headers=auth["headers"],
                          json={"source_entity_ids": sources, "target_sku_from": sources[0]})

    assert r.status_code == 200, r.text
    assert not session.in_transaction()


async def test_a_request_never_joins_the_transaction_the_test_left_open(client, session, auth):
    sources = [await _item(client, auth, 100.0), await _item(client, auth, 50.0)]
    await session.execute(text("SELECT 1"))

    r = await client.post("/items/merge/preview", headers=auth["headers"],
                          json={"source_entity_ids": sources, "target_sku_from": sources[0]})

    assert r.status_code == 200, r.text
    assert not session.in_transaction()


async def test_a_request_that_answers_an_error_keeps_nothing_it_did_not_commit(client, session, auth):
    """Production closes a failed request's session without committing it: a write the
    handler flushed before refusing is gone."""
    from fastapi import Depends, HTTPException

    from celerp.db import get_session
    from celerp.main import app
    from celerp.models.company import Company

    company_id = auth["company_id"]
    before = await session.scalar(select(Company.name).where(Company.id == company_id))

    async def _flush_then_refuse(db=Depends(get_session)):
        await db.execute(update(Company).where(Company.id == company_id).values(name="Never kept"))
        raise HTTPException(status_code=409, detail="refused")

    app.router.add_api_route("/__test__/flush-then-refuse", _flush_then_refuse, methods=["POST"])
    try:
        r = await client.post("/__test__/flush-then-refuse", headers=auth["headers"])
    finally:
        app.router.routes.pop()

    assert r.status_code == 409, r.text
    assert await session.scalar(select(Company.name).where(Company.id == company_id)) == before


async def test_reading_the_runtime_state_writes_nothing(session):
    """The drain check reads the runtime state on every write request through a session it
    never commits. On an install with no state row yet the read answers the defaults and
    inserts nothing: an insert would be discarded at close, and until then it holds the
    row's key, so every other write request's drain check waits on it."""
    from celerp.models.auth import SystemRuntimeState
    from celerp.services import runtime_state

    await session.execute(delete(SystemRuntimeState))
    runtime_state._drain_cache_bust()

    assert await runtime_state.is_draining(session) is False
    assert not session.new
    assert await session.scalar(select(func.count()).select_from(SystemRuntimeState)) == 0
