# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A busy webhook or schedule never loses its reconciliation.

A delivery that finds the resource held records a rerun on the heartbeat row. Only
an execution that runs the work clears it: the holder runs it before it releases the
resource, and a rerun with no live holder (left by a crashed holder, or recorded while
admission was full) runs within one sweeper tick. An elapsed time is never proof a run
ended: a holder that keeps renewing keeps its lease however long it runs."""
from __future__ import annotations

import asyncio
import contextvars
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from celerp.connectors.base import ConnectorContext, EntityFlow, SyncEntity, SyncResult
from celerp.db import get_session_ctx
from company_backup_support import company, owner, token
from connector_test_support import connected
from migration_support import real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio


class _Stub:
    name = "stub_rerun"
    display_name = "Stub"
    store_scoped_ids = False
    reviewed_first_sync = True
    entity_flows = {"orders": EntityFlow(inbound=("orders",))}
    default_directions = {"orders": "inbound"}

    def __init__(self):
        self.calls: list[dict] = []
        self.during = None

    async def sync_orders(self, ctx, since=None, reconcile=False):
        self.calls.append({"reconcile": reconcile})
        if self.during is not None:
            during, self.during = self.during, None
            await during()
        return SyncResult(entity=SyncEntity.ORDERS, created=0, updated=0)


@pytest.fixture
async def stub(real_engine, monkeypatch):
    from unittest.mock import AsyncMock

    from celerp.connectors import registry

    connector = _Stub()
    monkeypatch.setitem(registry._registry, connector.name, connector)
    monkeypatch.setattr("celerp.connectors.ownership._lock_active_company", AsyncMock(return_value=None))
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(
        side_effect=lambda company_id, *_a, **_k: ConnectorContext(company_id=str(company_id), access_token="t")))
    return connector


async def _connected(stub) -> str:
    cid = str(uuid.uuid4())
    await connected(cid, stub, stub.default_directions)
    return cid


async def _reruns(cid, connector) -> list:
    async with get_session_ctx() as s:
        rows = (await s.execute(sa.text(
            "SELECT rerun_requested FROM connector_execution_heartbeats "
            "WHERE company_id = :c AND connector = :k"), {"c": cid, "k": connector})).all()
    return [r for row in rows for r in (row[0] or [])]


async def _outside(coro):
    """``coro`` in a task of its own, as a request or the sweeper runs: outside the
    execution of the task that awaits it."""
    return await asyncio.create_task(coro, context=contextvars.Context())


async def _webhook(stub, cid) -> None:
    from celerp.connectors.webhooks import WebhookEvent, handle_webhook

    await _outside(handle_webhook(WebhookEvent(platform=stub.name, topic="orders/updated"), cid))


async def test_a_busy_webhook_runs_after_a_long_running_sync_ends(stub):
    """Sync A runs past 15 minutes and keeps its lease; webhook B is refused without
    running, and the reconciliation B needed runs once after A, before A releases."""
    from celerp.connectors import execution
    from celerp.connectors.sync_runner import run_sync

    cid = await _connected(stub)

    async def _long_running():
        async def _backdate():
            async with get_session_ctx() as s:
                await s.execute(sa.text(
                    "UPDATE sync_runs SET started_at = :t WHERE company_id = :c"),
                    {"t": datetime.now(timezone.utc) - timedelta(minutes=16), "c": cid})
                await s.commit()
        await _outside(_backdate())
        assert await execution._renew_once(execution.current())  # a live holder keeps its lease
        await _webhook(stub, cid)
        assert len(stub.calls) == 1  # B's body did not run
        assert await _outside(_reruns(cid, stub.name)) == ["orders"]

    stub.during = _long_running
    await run_sync(stub, cid, "orders", purpose="manual_sync")

    assert stub.calls == [{"reconcile": False}, {"reconcile": True}]
    assert await _reruns(cid, stub.name) == []
    async with get_session_ctx() as s:
        live = await execution.live_executions(s, cid, stub.name)
    assert live == []


async def test_a_sync_request_runs_the_rerun_a_busy_delivery_left_before_it_releases(
        stub, real_engine, real_client, connector_modules_running, signed_in_to_cloud):
    """A delivery found the resource held and recorded a rerun; the next sync the user asks
    for runs its own work, then the rerun once with the reconciliation pass, and clears it
    before it releases."""
    from celerp.connectors.execution import record_rerun

    user = await owner(real_engine)
    cid = str(await company(real_engine, user, "Rerun Trading", "rerun-marker"))
    tok = await token(real_engine, user, cid)
    await connected(cid, stub, stub.default_directions)
    await record_rerun(cid, stub.name, "orders")

    r = await real_client.post(f"/connectors/{stub.name}/sync", json={"entity": "orders"},
                               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert stub.calls == [{"reconcile": False}, {"reconcile": True}]
    assert await _reruns(cid, stub.name) == []


async def test_a_rerun_refused_while_modules_change_in_a_sync_request_is_recorded_again(
        stub, real_engine, real_client, connector_modules_running, signed_in_to_cloud):
    """The rerun a sync request runs before it releases is refused because a module is
    changing: the request still answers for its own work, and the rerun is recorded again
    for the next holder or the sweeper, never dropped."""
    from celerp.connectors.execution import record_rerun
    from celerp.modules.boundary import ModuleBusy

    user = await owner(real_engine)
    cid = str(await company(real_engine, user, "Rerun Again Trading", "rerun-again-marker"))
    tok = await token(real_engine, user, cid)
    await connected(cid, stub, stub.default_directions)
    await record_rerun(cid, stub.name, "orders")

    async def _module_changing():
        stub.sync_orders = _refused

    async def _refused(ctx, since=None, reconcile=False):
        stub.calls.append({"reconcile": reconcile})
        raise ModuleBusy("celerp-inventory")

    stub.during = _module_changing
    r = await real_client.post(f"/connectors/{stub.name}/sync", json={"entity": "orders"},
                               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert stub.calls == [{"reconcile": False}, {"reconcile": True}]
    assert await _reruns(cid, stub.name) == ["orders"]


async def test_another_execution_acquiring_does_not_clear_a_pending_rerun(stub):
    """An execution that takes the resource for other work runs the recorded rerun
    before it releases; it never drops it at acquire."""
    from celerp.connectors.execution import connector_execution, record_rerun

    cid = await _connected(stub)
    await record_rerun(cid, stub.name, "orders")
    async with connector_execution(cid, stub, "undo", ["orders"]):
        assert await _outside(_reruns(cid, stub.name)) == ["orders"]
    assert stub.calls == [{"reconcile": True}]
    assert await _reruns(cid, stub.name) == []


async def test_a_crashed_holders_rerun_runs_within_one_sweeper_tick(stub):
    from celerp.connectors.execution import record_rerun, sweep_reruns

    cid = await _connected(stub)
    await record_rerun(cid, stub.name, "orders")
    async with get_session_ctx() as s:  # the holder died: its heartbeat expired, nothing released it
        await s.execute(sa.text(
            "UPDATE connector_execution_heartbeats SET holder = :h, expires_at = now() - interval '1 minute' "
            "WHERE company_id = :c AND connector = :k"), {"h": str(uuid.uuid4()), "c": cid, "k": stub.name})
        await s.commit()

    await sweep_reruns()
    assert stub.calls == [{"reconcile": True}]
    assert await _reruns(cid, stub.name) == []

    await sweep_reruns()  # ran once; nothing left to run
    assert stub.calls == [{"reconcile": True}]


async def test_the_sweeper_leaves_a_live_holders_rerun_to_the_holder(stub):
    from celerp.connectors.execution import connector_execution, sweep_reruns

    cid = await _connected(stub)

    async def _during():
        await _webhook(stub, cid)
        await _outside(sweep_reruns())
        assert len(stub.calls) == 1
        assert await _outside(_reruns(cid, stub.name)) == ["orders"]

    stub.during = _during
    async with connector_execution(cid, stub, "manual_sync", ["orders"]):
        from celerp.connectors.sync_runner import run_sync
        await run_sync(stub, cid, "orders", purpose="manual_sync")
    assert stub.calls == [{"reconcile": False}, {"reconcile": True}]
