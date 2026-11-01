# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Deferred connector work stopped before it ran is paced by REFUSALS, whatever loop
tries it again.

A first sync no process holds (resumed by the sweeper) and a queued outbound identity
(sent by the queue) are each tried at most once until their retry time, whatever stopped
the attempt: every refusal reason, the relay unreachable, the connection not connected,
Connect ended, or any other failure. Work only the person can clear, or no longer called
for, ends visibly: a first sync needs attention with its reason and Retry; queued rows
are released, or wait for the person's own sync with their reason."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from celerp.connectors.execution import REFUSALS
from celerp.db import get_session_ctx
from connector_test_support import connect
from migration_support import real_engine  # noqa: F401 - fixture
from pacing_guard import seeding_pacing
from test_first_sync_recovery import DIRECTIONS, _started, store  # noqa: F401 - fixture
from test_services.test_outbound_queue import _purge_committed

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("connector_modules_running")]

# Each way an attempt is stopped before its work, and the pacing REFUSALS gives it.
_STOPS = {**REFUSALS, "relay_down": "retry", "no_context": REFUSALS["not_connected"],
          "connect_ended": "drop", "error": "retry"}


def _stopping(monkeypatch, stop: str, context) -> list:
    """Every attempt reaches the relay and is stopped by ``stop``; returns its calls."""
    from celerp.connectors import execution, relay_token

    calls: list = []

    async def fetch(cid, *_a, **_k):
        calls.append(cid)
        if stop == "relay_down":
            raise relay_token.RelayUnavailable("relay 503")
        if stop == "connect_ended":
            raise relay_token.ConnectRequired("Connect ended")
        if stop == "error":
            raise RuntimeError("the relay answered 500")
        return None if stop == "no_context" else context(cid)

    async def refusing(ex, connector):
        raise execution.ExecutionRefused(stop, f"refused: {stop}")

    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", AsyncMock(side_effect=fetch))
    if stop in REFUSALS:
        monkeypatch.setattr(execution, "_prove_store", refusing)
    return calls


async def _first_sync_pacing(cid) -> dict:
    async with get_session_ctx() as s:
        row = (await s.execute(sa.text(
            "SELECT state, error, retry_count, EXTRACT(EPOCH FROM next_attempt_at - now()) AS wait "
            "FROM connector_first_syncs WHERE company_id = :c"), {"c": str(cid)})).one()
    return dict(row._mapping)


async def _due_first_sync(cid, *, abandoned: bool = False) -> None:
    from celerp.connectors.execution import TTL

    async with get_session_ctx() as s, seeding_pacing(s):
        await s.execute(sa.text(
            "UPDATE connector_first_syncs SET state = 'running', "
            + ("updated_at = :at" if abandoned else "next_attempt_at = now() - interval '1 second'")
            + " WHERE company_id = :c"), {"c": str(cid), "at": datetime.now(timezone.utc) - TTL * 2})
        await s.commit()


@pytest.mark.parametrize("stop", sorted(_STOPS))
async def test_an_abandoned_first_sync_stopped_before_its_work_is_paced(real_engine, store, monkeypatch, stop):
    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.execution import sweep_reruns

    cid = await _started(f"Paced {stop}", DIRECTIONS)
    await _due_first_sync(cid, abandoned=True)
    calls = _stopping(monkeypatch, stop, lambda c: ConnectorContext(company_id=str(c), access_token="ck:cs"))
    for _ in range(3):
        await sweep_reruns()
    row = await _first_sync_pacing(cid)
    assert len(calls) == 1, f"{stop}: {len(calls)} attempts over 3 sweeper ticks"
    if _STOPS[stop] != "retry":
        # Only the person can clear it, or it is no longer called for: it needs attention,
        # with the reason and Retry.
        assert row["state"] == "failed" and (row["error"] or {}).get("detail"), row
        return
    assert row["state"] == "running" and row["retry_count"] == 1, row
    assert 50 < row["wait"] <= 61, row
    assert (row["error"] or {}).get("detail"), "the person is told why it waits"
    await _due_first_sync(cid)
    await sweep_reruns()
    row = await _first_sync_pacing(cid)
    assert len(calls) == 2 and row["retry_count"] == 2 and 110 < row["wait"] <= 121, row


async def test_a_waiting_first_sync_is_shown_with_its_reason_and_can_be_tried_now(real_engine, store, monkeypatch):
    from celerp.connectors import sync_runner
    from celerp.connectors.execution import sweep_reruns

    cid = await _started("Waiting", DIRECTIONS)
    await _due_first_sync(cid, abandoned=True)
    _stopping(monkeypatch, "relay_down", None)
    await sweep_reruns()
    async with get_session_ctx() as s:
        await sync_runner.retry_first_sync(s, str(cid), "woocommerce")
        await s.commit()
    row = await _first_sync_pacing(cid)
    assert row["state"] == "starting" and row["retry_count"] == 0 and row["wait"] is None, row
    assert row["error"] is None


async def _queued(company_id) -> dict | None:
    from celerp.models.connector_config import OutboundQueue

    async with get_session_ctx() as s:
        parked = OutboundQueue.next_retry_at == sa.literal_column("CAST('infinity' AS TIMESTAMPTZ)")
        row = (await s.execute(sa.select(
            OutboundQueue.status, OutboundQueue.retry_count, OutboundQueue.error_message,
            sa.case((parked, None), else_=sa.extract("epoch", OutboundQueue.next_retry_at - sa.func.now())).label("wait"),
            parked.label("parked"),
        ).where(OutboundQueue.company_id == str(company_id)))).one_or_none()
    return dict(row._mapping) if row is not None else None


@pytest.mark.parametrize("stop", sorted(_STOPS))
async def test_a_queued_outbound_identity_stopped_before_its_work_is_paced(_db_engine, monkeypatch, stop):
    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.outbound_queue import process_outbound_queue_once
    from celerp.models.company import Company
    from celerp.models.connector_config import OutboundQueue

    calls = _stopping(monkeypatch, stop, lambda c: ConnectorContext(company_id=str(c), access_token="k:s"))
    company_id = uuid.uuid4()
    try:
        async with get_session_ctx() as seed:
            seed.add(Company(id=company_id, name="Paced Co", slug=f"paced-{company_id.hex[:8]}", settings={}))
            await seed.flush()
            await connect(seed, company_id, "woocommerce", first_sync="completed")
            seed.add(OutboundQueue(company_id=str(company_id), connector="woocommerce",
                                   entity_type="inventory", entity_id="99", status="pending"))
            await seed.commit()
        for _ in range(3):
            await process_outbound_queue_once()
        row = await _queued(company_id)
        assert len(calls) == 1, f"{stop}: {len(calls)} attempts over 3 queue passes"
        if _STOPS[stop] == "drop":
            assert row is None, row
            return
        assert row["status"] == "pending" and row["retry_count"] == 1 and row["error_message"], row
        if _STOPS[stop] == "park":
            assert row["parked"], row
            return
        assert 50 < row["wait"] <= 61, row
        async with get_session_ctx() as s, seeding_pacing(s):
            await s.execute(sa.update(OutboundQueue).where(OutboundQueue.company_id == str(company_id))
                            .values(next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await s.commit()
        await process_outbound_queue_once()
        row = await _queued(company_id)
        assert len(calls) == 2 and row["retry_count"] == 2 and 110 < row["wait"] <= 121, row
    finally:
        await _purge_committed(company_id)
