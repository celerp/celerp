# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bulk run action that meets an unexpected failure keeps nothing and can be sent again.

Only a refusal or a refused journal entry skips a run. Anything else, here a plain
ValueError while the second run completes, fails the whole action: the first run, already
completed inside its own savepoint, is rolled back with it, no stock moves, and the request
key is not used up, so the same request sent again with the same key goes through.

Runs on real PostgreSQL, each request in its own session, as in production.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.services import auto_je
from company_backup_support import owner, token
from migration_support import auth, maker
from test_mfg_wip_race_pg import _job
from test_posting_roles_race_pg_origin import own_client  # noqa: F401  (own_client is a fixture)

pytestmark = pytest.mark.asyncio


async def _events(engine, company_id) -> int:
    async with maker(engine)() as s:
        return await s.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == company_id))


async def _status(client, tok: str, run_id: str) -> str:
    return (await client.get(f"/manufacturing/{run_id}", headers=auth(tok))).json()["status"]


async def _qty(client, tok: str, item_id: str) -> float:
    return float((await client.get(f"/items/{item_id}", headers=auth(tok))).json()["quantity"])


async def test_an_unexpected_failure_fails_the_whole_bulk_action_and_the_retry_goes_through(
        committed_engine, own_client, monkeypatch):
    user = await owner(committed_engine)
    job = await _job(committed_engine, own_client, user, uuid.uuid4().hex[:8], runs=2, stock=100.0)
    tok = await token(committed_engine, user, job["cid"])
    first, second = job["orders"]
    before = await _events(committed_engine, job["cid"])
    real = auto_je.create_for_mfg_movement

    async def fail_second(session, *, order_id, movement, **kw):
        if order_id == second and movement.startswith("complete:"):
            raise ValueError("unexpected state")
        return await real(session, order_id=order_id, movement=movement, **kw)

    monkeypatch.setattr(auto_je, "create_for_mfg_movement", fail_second)
    body = {"run_ids": [first, second], "action": "complete", "idempotency_key": f"bulk-{uuid.uuid4()}"}

    with pytest.raises(ValueError, match="unexpected state"):
        await own_client.post("/manufacturing/bulk-action", headers=auth(tok), json=body)

    assert await _events(committed_engine, job["cid"]) == before
    assert [await _status(own_client, tok, r) for r in (first, second)] == ["planned", "planned"]
    assert await _qty(own_client, tok, job["raw"]) == 100.0

    monkeypatch.setattr(auto_je, "create_for_mfg_movement", real)
    r = await own_client.post("/manufacturing/bulk-action", headers=auth(tok), json=body)

    assert r.status_code == 200, r.text
    assert r.json() == {"done": [first, second], "skipped": []}
    assert [await _status(own_client, tok, r) for r in (first, second)] == ["completed", "completed"]
    assert await _qty(own_client, tok, job["raw"]) == 80.0
