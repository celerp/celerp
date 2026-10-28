# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The connector execution boundary on the real database: its connection budget,
lease takeover and loss, renewal, and the guards on writes it did not fence."""
from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa

from celerp.connectors.base import ConnectorContext, EntityFlow
from celerp.db import get_session_ctx
from connector_test_support import connected

pytestmark = pytest.mark.asyncio


class _Stub:
    store_scoped_ids = False
    reviewed_first_sync = False
    entity_flows = {"orders": EntityFlow(inbound=("orders",))}
    default_directions = {"orders": "inbound"}

    def __init__(self):
        self.name = f"stub_execution_{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def _relay(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("celerp.connectors.ownership._lock_active_company", AsyncMock(return_value=None))
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(
        side_effect=lambda company_id, *_a, **_k: ConnectorContext(company_id=str(company_id), access_token="t")))


@pytest.fixture
async def stub(_db_engine):
    """A connector connected to a fresh company, its connection removed afterwards."""
    from celerp.connectors.execution import forget_executions
    from celerp.models.connector_config import ConnectorConfig

    connector = _Stub()
    connector.company_id = str(uuid.uuid4())
    await connected(connector.company_id, connector, connector.default_directions)
    yield connector
    async with get_session_ctx() as s:
        await s.execute(sa.delete(ConnectorConfig).where(ConnectorConfig.connector == connector.name))
        await forget_executions(s, [connector.company_id], connector.name)
        await s.commit()


def _run(stub, purpose="manual_sync"):
    from celerp.connectors.execution import connector_execution

    return connector_execution(stub.company_id, stub, purpose, ["orders"])


# -- connection budget ---------------------------------------------------------------

async def test_an_execution_holding_a_connection_cannot_check_out_a_second(stub):
    from celerp.connectors.execution import ExecutionPoolBudget

    async with _run(stub):
        async with get_session_ctx() as first:
            await first.execute(sa.text("SELECT 1"))
            with pytest.raises(ExecutionPoolBudget):
                async with get_session_ctx() as second:
                    await second.execute(sa.text("SELECT 1"))


async def test_an_execution_uses_one_connection_after_another(stub):
    async with _run(stub):
        for _ in range(3):
            async with get_session_ctx() as s:
                assert await s.scalar(sa.text("SELECT 1")) == 1


# -- lease: takeover, loss, renewal ------------------------------------------------------

_NOOP_WRITE = sa.text("UPDATE connector_execution_heartbeats SET rerun_requested = rerun_requested WHERE false")


async def _expire(stub):
    """The lease's heartbeat runs out, as when its holder stops renewing."""
    from celerp.connectors.execution import _internal

    with _internal():
        async with get_session_ctx() as s:
            await s.execute(sa.text(
                "UPDATE connector_execution_heartbeats SET expires_at = now() - interval '1 second' "
                "WHERE company_id = :c AND connector = :k"), {"c": stub.company_id, "k": stub.name})
            await s.commit()


async def test_a_held_lease_refuses_a_second_execution(stub):
    from celerp.connectors.execution import ExecutionBusy

    async def _second():
        async with _run(stub):
            pass

    async with _run(stub):
        with pytest.raises(ExecutionBusy):
            await asyncio.create_task(_second())


async def test_an_expired_lease_is_taken_over_and_its_old_holder_cannot_write(stub):
    from celerp.connectors.execution import LeaseLost

    taken = asyncio.Event()
    done = asyncio.Event()

    async def _takeover():
        async with _run(stub) as ex:
            async with get_session_ctx() as s:
                await s.execute(_NOOP_WRITE)
                await s.commit()
            taken.set()
            await done.wait()
        return ex.generations

    async with _run(stub) as old:
        await _expire(stub)
        new = asyncio.create_task(_takeover())
        await taken.wait()
        with pytest.raises(LeaseLost):
            async with get_session_ctx() as s:
                await s.execute(_NOOP_WRITE)
        assert old.lost
        done.set()
        generations = await new
    assert generations["orders"] == old.generations["orders"] + 1


async def test_renewal_extends_the_lease_from_the_database_clock(stub):
    from celerp.connectors.execution import _internal, _renew_once

    async with _run(stub) as ex:
        before = ex.confirmed_expiry
        await asyncio.sleep(0.05)
        with _internal():
            assert await _renew_once(ex)
        assert ex.confirmed_expiry > before
        ex.check_alive()


async def test_an_expired_heartbeat_is_never_renewed_and_the_lease_is_lost(stub):
    from celerp.connectors.execution import LeaseLost, _internal, _renew_once

    async with _run(stub) as ex:
        await _expire(stub)
        with _internal():
            assert not await _renew_once(ex)
        assert ex.lost
        with pytest.raises(LeaseLost):
            ex.check_alive()
        with pytest.raises(LeaseLost):
            async with get_session_ctx() as s:
                await s.execute(sa.text("SELECT 1"))


async def test_a_lease_past_its_confirmed_expiry_refuses_store_calls(stub):
    from datetime import datetime, timezone

    from celerp.connectors.execution import LeaseLost, before_remote_call

    async with _run(stub) as ex:
        before_remote_call("GET")
        ex.confirmed_expiry = datetime.now(timezone.utc)
        with pytest.raises(LeaseLost):
            before_remote_call("GET")


async def test_a_store_call_with_a_fenced_transaction_open_is_refused(stub):
    from celerp.connectors.execution import RemoteCallInTransaction, before_remote_call

    async with _run(stub):
        async with get_session_ctx() as s:
            await s.execute(sa.text("SELECT 1"))
            with pytest.raises(RemoteCallInTransaction):
                before_remote_call("POST")
            await s.commit()
        before_remote_call("POST")


# -- writes the execution did not fence ----------------------------------------------------

async def test_a_fenced_session_write_commits(stub):
    async with _run(stub):
        async with get_session_ctx() as s:
            await s.execute(_NOOP_WRITE)
            await s.commit()


async def test_a_task_spawned_inside_an_execution_cannot_write_on_its_lease(stub):
    from celerp.connectors.execution import SpawnedTaskWrite

    async def _spawned():
        async with get_session_ctx() as s:
            await s.execute(_NOOP_WRITE)

    async with _run(stub):
        with pytest.raises(SpawnedTaskWrite):
            await asyncio.create_task(_spawned())


async def test_a_core_write_beside_the_session_fence_is_refused(stub):
    import celerp.db as db
    from celerp.connectors.execution import UnfencedWrite

    async with _run(stub):
        async with db.engine.connect() as conn:
            with pytest.raises(UnfencedWrite):
                await conn.execute(_NOOP_WRITE)


# -- re-entrancy -----------------------------------------------------------------------------

async def test_a_nested_execution_joins_for_held_resources_and_refuses_others(stub):
    from celerp.connectors.execution import NestedExecutionError, connector_execution

    async with _run(stub) as outer:
        async with connector_execution(stub.company_id, stub, "manual_sync", ["orders"]) as inner:
            assert inner is outer
        with pytest.raises(NestedExecutionError):
            async with connector_execution(stub.company_id, stub, "manual_sync", ["products"]):
                pass
