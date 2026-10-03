# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Starting, holding, resuming and rescheduling a production run, one run at a time or many.

A completed or cancelled run stays closed: none of these bring it back (a completed run
leaves only through Reopen). Resume needs a run on hold. Sending the same request again
changes nothing it already changed. The same rules hold from the runs list's bulk actions.
Concurrent requests are covered in test_mfg_wip_race_pg and test_fresh_authority_races_pg.
"""
from __future__ import annotations

import pytest

from mfg_runs import cancel, complete, issue, product, receive, refusal, run, snapshot
from test_cost_restatement import _item, _state, auth, ids  # noqa: F401  (fixtures)
from test_mfg_wip_upgrade import _events

pytestmark = pytest.mark.asyncio


async def _run(client, auth) -> tuple[str, str]:
    raw = await _item(client, auth, 100.0, qty=10)
    return raw, await run(client, auth, await product(client, auth, [(raw, 5)]), 2)


async def _act(client, auth, order: str, action: str, body: dict | None = None):
    return await client.post(f"/manufacturing/{order}/{action}", headers=auth["headers"], json=body or {})


async def _bulk(client, auth, orders: list[str], action: str, key: str | None = None) -> dict:
    body = {"run_ids": orders, "action": action} | ({"idempotency_key": key} if key else {})
    r = await client.post("/manufacturing/bulk-action", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _completed(client, auth) -> str:
    raw, order = await _run(client, auth)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    assert (await receive(client, auth, order, key="r")).status_code == 200
    if (await client.get(f"/manufacturing/{order}", headers=auth["headers"])).json()["status"] != "completed":
        assert (await complete(client, auth, order, key="c")).status_code == 200
    return order


async def test_a_cancelled_run_cannot_be_started_held_resumed_or_rescheduled(client, session, auth):
    _, order = await _run(client, auth)
    assert (await cancel(client, auth, order, key="x")).status_code == 200
    before = await snapshot(session, auth, order)

    for action, body in (("start", None), ("hold", None), ("resume", None), ("schedule", {"due_date": "2026-04-01"})):
        refusal(await _act(client, auth, order, action, body), 409, "run_closed")
    out = await _bulk(client, auth, [order], "start")

    assert out["done"] == [] and out["skipped"][0]["message_key"] == "mfg.run_closed"
    assert await snapshot(session, auth, order) == before
    assert (await _state(session, auth, order))["status"] == "cancelled"


async def test_a_completed_run_leaves_only_through_reopen(client, session, auth):
    order = await _completed(client, auth)
    before = await snapshot(session, auth, order)

    for action, body in (("start", None), ("hold", None), ("resume", None), ("schedule", {"priority": "high"})):
        refusal(await _act(client, auth, order, action, body), 409, "run_closed")
    for action in ("start", "hold", "resume"):
        assert (await _bulk(client, auth, [order], action))["done"] == []

    assert await snapshot(session, auth, order) == before


async def test_only_a_run_on_hold_resumes(client, session, auth):
    _, order = await _run(client, auth)
    refusal(await _act(client, auth, order, "resume"), 409, "not_on_hold")
    assert (await _act(client, auth, order, "hold", {"reason": "waiting"})).status_code == 200
    assert (await _state(session, auth, order))["status"] == "on_hold"

    assert (await _act(client, auth, order, "resume")).status_code == 200

    assert (await _state(session, auth, order))["status"] == "in_progress"


async def test_sending_the_same_request_again_changes_nothing(client, session, auth):
    _, order = await _run(client, auth)
    first = await _act(client, auth, order, "hold", {"reason": "waiting", "idempotency_key": "h1"})
    assert first.status_code == 200, first.text
    events = await _events(session, auth)

    again = await _act(client, auth, order, "hold", {"reason": "waiting", "idempotency_key": "h1"})

    assert again.json() == first.json()
    assert await _events(session, auth) == events
    refusal(await _act(client, auth, order, "hold", {"reason": "other", "idempotency_key": "h1"}), 409, "key_reused")
    s1 = await _act(client, auth, order, "schedule", {"due_date": "2026-04-01", "idempotency_key": "s1"})
    assert (await _act(client, auth, order, "schedule", {"due_date": "2026-04-01", "idempotency_key": "s1"})).json() \
        == s1.json()
    assert await _events(session, auth) == events + 1


async def test_a_bulk_action_sent_again_changes_nothing(client, session, auth):
    runs = [(await _run(client, auth))[1] for _ in range(2)]
    first = await _bulk(client, auth, runs, "hold", key="bulk-1")
    assert first["done"] == runs
    events = await _events(session, auth)

    again = await _bulk(client, auth, runs, "hold", key="bulk-1")

    assert again["done"] == runs
    assert await _events(session, auth) == events
    assert [(await _state(session, auth, o))["status"] for o in runs] == ["on_hold", "on_hold"]


async def test_only_a_planned_run_starts_and_a_run_on_hold_is_not_held_again(client, session, auth):
    """A run on hold carries on through Resume, which clears why it was held; Start does not."""
    _, order = await _run(client, auth)
    assert (await _act(client, auth, order, "start")).status_code == 200
    refusal(await _act(client, auth, order, "start"), 409, "not_planned")
    assert (await _act(client, auth, order, "hold", {"reason": "waiting"})).status_code == 200
    before = await snapshot(session, auth, order)

    refusal(await _act(client, auth, order, "start"), 409, "not_planned")
    refusal(await _act(client, auth, order, "hold", {"reason": "other"}), 409, "already_on_hold")
    out = await _bulk(client, auth, [order], "start")

    assert out["done"] == [] and out["skipped"][0]["message_key"] == "mfg.not_planned"
    assert await snapshot(session, auth, order) == before
    assert (await _act(client, auth, order, "resume")).status_code == 200
    state = await _state(session, auth, order)
    assert state["status"] == "in_progress" and "hold_reason" not in state
