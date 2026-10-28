"""A request run by the test client ends its own transaction, as production does.

Production gives each request a session of its own and closes it at the end, so a
request that never commits holds nothing afterwards. The client shares the test's
session with every request; without the request boundary, a preview's company lock
stayed held into the next request and the lock rank recorder judged that request
by locks it never took."""

from __future__ import annotations

from sqlalchemy import text

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
