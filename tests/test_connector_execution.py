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
