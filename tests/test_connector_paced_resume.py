# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Deferred connector work is a closed state machine over execution.PACED and REFUSALS.

For every table of PACED and every pacing REFUSALS gives, work stopped before it ran ends
in a known state: "retry" waits a bounded backoff and execution.resume_reruns makes it due
again; "park" waits, shown to the person, until the person's own work (or their try now)
resumes it, and it is then sent as its row stands, with any change folded into it while it
waited; "drop" removes the work and tells the person where they see it. A table added to
PACED without a loop here fails the coverage test."""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from celerp.connectors.execution import _RERUN_BACKOFF_MAX, PACED, REFUSALS
from celerp.db import get_session_ctx
from connector_test_support import connect
from migration_support import real_engine  # noqa: F401 - fixture
from test_connector_deferred_pacing import _due_first_sync, _first_sync_pacing, _queued, _stopping
from test_connector_rerun_pacing import _beat, _outside, _remote_refusing, stub  # noqa: F401 - fixture
from test_first_sync_recovery import DIRECTIONS, _started, store  # noqa: F401 - fixture
from test_services.test_outbound_queue import _purge_committed

pytestmark = pytest.mark.asyncio

_PACINGS = sorted(set(REFUSALS.values()))
_CAP = _RERUN_BACKOFF_MAX.total_seconds()


def _reason(pacing: str) -> str:
    """A refusal REFUSALS paces with ``pacing``."""
    return next(reason for reason, paced in sorted(REFUSALS.items()) if paced == pacing)


async def _refused_runs(company_id, entity: str) -> int:
    from celerp.models.sync_run import SyncRun

    async with get_session_ctx() as s:
        return await s.scalar(sa.select(sa.func.count()).select_from(SyncRun).where(
            SyncRun.company_id == str(company_id), SyncRun.entity == entity, SyncRun.status == "failed"))


@pytest.mark.parametrize("pacing", _PACINGS)
async def test_a_rerun_ends_as_its_refusal_paces_it(stub, monkeypatch, pacing):
    from celerp.connectors import execution

    connector, cid = stub
    real_prove = execution._prove_store
    calls = _remote_refusing(monkeypatch, _reason(pacing))
    await execution.record_rerun(cid, connector.name, "orders")
    for _ in range(3):
        await _outside(execution.sweep_reruns())
    pending, attempts, waiting, parked = await _beat(cid, connector.name)
    assert len(calls) == 1, f"{len(calls)} attempts over 3 sweeper ticks"
    if pacing == "drop":
        assert pending == 0 and await _refused_runs(cid, "orders") == 1
        return
    if pacing == "retry":
        assert attempts == 1 and waiting and not parked
        async with get_session_ctx() as s:
            wait = await s.scalar(sa.text(
                "SELECT EXTRACT(EPOCH FROM rerun_retry_at - now()) FROM connector_execution_heartbeats "
                "WHERE company_id = :c AND resource = 'orders'"), {"c": cid})
        assert 0 < float(wait) <= _CAP
        await execution.resume_reruns(cid, connector.name)
        await _outside(execution.sweep_reruns())
        assert len(calls) == 2, "resumed work is tried at the next tick"
        return
    assert parked and await _refused_runs(cid, "orders") == 1, "a park is shown on the sync history"
    monkeypatch.setattr(execution, "_prove_store", real_prove)
    async with execution.connector_execution(cid, connector, "manual_sync", ["orders"]):
        pass
    assert connector.calls == [{"reconcile": True}] and (await _beat(cid, connector.name))[:2] == (0, 0)


@pytest.mark.usefixtures("connector_modules_running")
@pytest.mark.parametrize("pacing", _PACINGS)
async def test_a_first_sync_ends_as_its_refusal_paces_it(real_engine, store, monkeypatch, pacing):
    from celerp.connectors import sync_runner
    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.execution import resume_reruns, sweep_reruns

    cid = await _started(f"Resumed {pacing}", DIRECTIONS)
    await _due_first_sync(cid, abandoned=True)
    calls = _stopping(monkeypatch, _reason(pacing), lambda c: ConnectorContext(company_id=str(c), access_token="ck:cs"))
    for _ in range(3):
        await sweep_reruns()
    row = await _first_sync_pacing(cid)
    assert len(calls) == 1, f"{len(calls)} attempts over 3 sweeper ticks"
    if pacing == "retry":
        assert row["state"] == "running" and row["retry_count"] == 1 and 0 < row["wait"] <= _CAP, row
        await resume_reruns(cid, "woocommerce")
        assert (await _first_sync_pacing(cid))["retry_count"] == 0
        await sweep_reruns()
        assert len(calls) == 2, "resumed work is tried at the next tick"
        return
    # Parked or dropped, it needs attention with its reason, and the person's Retry starts it.
    assert row["state"] == "failed" and (row["error"] or {}).get("detail"), row
    async with get_session_ctx() as s:
        await sync_runner.retry_first_sync(s, str(cid), "woocommerce")
        await s.commit()
    row = await _first_sync_pacing(cid)
    assert row["state"] == "starting" and row["retry_count"] == 0 and row["wait"] is None, row


async def _queue_pass_count(company_id) -> int:
    from celerp.models.connector_config import OutboundQueue

    async with get_session_ctx() as s:
        return await s.scalar(sa.select(sa.func.count()).select_from(OutboundQueue).where(
            OutboundQueue.company_id == str(company_id)))


async def _parked_count(company_id) -> int:
    from celerp.models.connector_config import OutboundQueue

    async with get_session_ctx() as s:
        return await s.scalar(sa.select(sa.func.count()).select_from(OutboundQueue).where(
            OutboundQueue.company_id == str(company_id),
            OutboundQueue.next_retry_at == sa.literal_column("CAST('infinity' AS TIMESTAMPTZ)")))


@pytest.mark.parametrize("pacing", _PACINGS)
async def test_a_queued_outbound_identity_ends_as_its_refusal_paces_it(_db_engine, monkeypatch, pacing):
    from celerp.connectors import execution
    from celerp.connectors.base import ConnectorContext, SyncDirection, SyncEntity, SyncResult
    from celerp.connectors.outbound_queue import process_outbound_queue_once
    from celerp.connectors.woocommerce import WooCommerceConnector
    from celerp.models.company import Company
    from celerp.models.connector_config import OutboundQueue

    calls = _stopping(monkeypatch, _reason(pacing), lambda c: ConnectorContext(company_id=str(c), access_token="k:s"))
    sent: list[str] = []

    async def push(self, ctx, identity):
        sent.append(identity)
        return SyncResult(entity=SyncEntity.INVENTORY, direction=SyncDirection.OUTBOUND)

    monkeypatch.setattr(WooCommerceConnector, "sync_inventory_identity_out", push)

    def change():
        return OutboundQueue(company_id=str(company_id), connector="woocommerce",
                             entity_type="inventory", entity_id="99", status="pending")

    company_id = uuid.uuid4()
    try:
        async with get_session_ctx() as seed:
            seed.add(Company(id=company_id, name="Resumed Co", slug=f"resumed-{company_id.hex[:8]}", settings={}))
            await seed.flush()
            await connect(seed, company_id, "woocommerce", first_sync="completed")
            seed.add(change())
            await seed.commit()
        for _ in range(3):
            await process_outbound_queue_once()
        row = await _queued(company_id)
        assert len(calls) == 1 and not sent, f"{len(calls)} attempts over 3 queue passes"
        if pacing == "drop":
            assert row is None and await _refused_runs(company_id, "inventory_out") == 1
            return
        assert row["retry_count"] == 1 and row["error_message"], row
        if pacing == "retry":
            assert 0 < row["wait"] <= _CAP, row
            await execution.resume_reruns(company_id, "woocommerce")
            await process_outbound_queue_once()
            assert len(calls) == 2, "resumed work is tried at the next pass"
            return
        assert row["parked"], row
        # A later change for the same item waits with it.
        async with get_session_ctx() as s:
            s.add(change())
            await s.commit()
        await process_outbound_queue_once()
        assert await _parked_count(company_id) == 2 and not sent
        # The person's own work, once the store is right, resumes both: they are folded and
        # sent once, as the item now stands.
        monkeypatch.setattr(execution, "_prove_store", lambda ex, connector: _false())
        async with execution.connector_execution(company_id, "woocommerce", "manual_sync",
                                                 [execution.execution_resource("outbound", "inventory")]):
            pass
        await process_outbound_queue_once()
        assert sent == ["99"] and await _queue_pass_count(company_id) == 0
    finally:
        await _purge_committed(company_id)


async def _false() -> bool:
    return False


_LOOPS = {
    "connector_execution_heartbeats": test_a_rerun_ends_as_its_refusal_paces_it,
    "connector_first_syncs": test_a_first_sync_ends_as_its_refusal_paces_it,
    "outbound_queue": test_a_queued_outbound_identity_ends_as_its_refusal_paces_it,
}


async def test_every_paced_table_is_driven_through_every_pacing():
    """A table added to PACED, or a pacing added to REFUSALS, needs its loop above."""
    assert set(_LOOPS) == set(PACED)
    for loop in _LOOPS.values():
        marks = [m for m in loop.pytestmark if m.name == "parametrize"]
        assert [sorted(m.args[1]) for m in marks] == [_PACINGS], loop.__name__
    assert set(REFUSALS.values()) == {"drop", "park", "retry"}
