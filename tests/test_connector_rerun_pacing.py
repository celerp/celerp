# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A pending rerun that did not complete is paced, whatever stopped it.

Every outcome of an attempt that is neither success nor a drop goes through one backoff:
a refusal for any reason the connectors raise, or a failure before the work ran. Each is
tried at most once until its retry time; a refusal only the person can clear waits for
them, recorded where they see it, and their own sync or a new store binding resumes it."""
from __future__ import annotations

import ast
import asyncio
import contextlib
import contextvars
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from celerp.connectors.base import ConnectorContext, EntityFlow, SyncEntity, SyncResult
from celerp.db import get_session_ctx
from connector_test_support import connected, retry_due
from migration_support import real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

_ROOT = Path(__file__).resolve().parents[1]


def _raised_reasons() -> set[str]:
    """Every reason the code refuses an execution with: the literal of each
    ExecutionRefused(...) and each string ``eligible`` returns, read from the source."""
    reasons: set[str] = set()
    for path in [*(_ROOT / "celerp").rglob("*.py"), *(_ROOT / "default_modules").rglob("*.py")]:
        if "/tests/" in str(path):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ExecutionRefused":
                first = node.args[0] if node.args else None
                assert isinstance(first, (ast.Constant, ast.Name)), f"{path}:{node.lineno}"
                if isinstance(first, ast.Constant):
                    reasons.add(first.value)
            if isinstance(node, ast.FunctionDef) and node.name == "eligible" and path.name == "execution.py":
                for ret in ast.walk(node):
                    if isinstance(ret, ast.Return) and ret.value is not None:
                        values = [ret.value.body, ret.value.orelse] if isinstance(ret.value, ast.IfExp) \
                            else [ret.value]
                        reasons |= {v.value for v in values
                                    if isinstance(v, ast.Constant) and isinstance(v.value, str)}
    return reasons


async def test_every_refusal_reason_the_code_raises_has_one_pacing():
    """No reason can reach the sweeper without saying whether it drops, retries or parks."""
    from celerp.connectors.execution import REFUSALS

    assert _raised_reasons() == set(REFUSALS)
    assert set(REFUSALS.values()) <= {"drop", "retry", "park"}


async def test_a_refusal_names_a_known_reason():
    from celerp.connectors.execution import ExecutionRefused

    with pytest.raises(ValueError):
        ExecutionRefused("never_heard_of")


class _Stub:
    name = "stub_pacing"
    display_name = "Stub"
    store_scoped_ids = False
    reviewed_first_sync = True
    entity_flows = {"orders": EntityFlow(inbound=("orders",))}
    outbound_dependencies: dict = {}
    default_directions = {"orders": "inbound"}

    def __init__(self):
        self.calls: list[dict] = []

    async def sync_orders(self, ctx, since=None, reconcile=False):
        self.calls.append({"reconcile": reconcile})
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
    cid = str(uuid.uuid4())
    await connected(cid, connector, connector.default_directions)
    return connector, cid


async def _outside(coro):
    return await asyncio.create_task(coro, context=contextvars.Context())


async def _beat(cid, connector) -> tuple[int, int, bool, bool]:
    """(pending requests, attempts, waiting for a retry time, parked) of the orders row."""
    async with get_session_ctx() as s:
        row = (await s.execute(sa.text(
            "SELECT rerun_requested - rerun_done AS pending, rerun_attempts AS attempts, "
            "COALESCE(rerun_retry_at > now(), false) AS waiting, "
            "COALESCE(rerun_retry_at = 'infinity', false) AS parked FROM connector_execution_heartbeats "
            "WHERE company_id = :c AND connector = :k AND resource = 'orders'"),
            {"c": cid, "k": connector})).one()
    return int(row.pending), int(row.attempts), bool(row.waiting), bool(row.parked)


def _outcomes() -> list[str]:
    return [*sorted(_raised_reasons()), "error"]


def _remote_refusing(monkeypatch, outcome: str) -> list[str]:
    """The store check, the remote step every execution takes before its work, answers
    ``outcome`` (a refusal reason, or a failure); returns the list of its calls."""
    from celerp.connectors import execution

    calls: list[str] = []

    async def refusing(ex, connector):
        calls.append(ex.purpose)
        if outcome == "error":
            raise RuntimeError("the store answered 500")
        raise execution.ExecutionRefused(outcome)

    monkeypatch.setattr(execution, "_prove_store", refusing)
    return calls


async def _webhook(connector, cid) -> None:
    from celerp.connectors.webhooks import WebhookEvent, handle_webhook

    await _outside(handle_webhook(WebhookEvent(platform=connector, topic="orders/updated"), cid))


@pytest.mark.parametrize("outcome", _outcomes())
async def test_a_delivery_not_run_is_tried_again_only_after_its_retry_time(stub, monkeypatch, outcome):
    """A delivery its execution refused or failed before the work: the delivery's own
    attempt and two sweeper ticks make one remote call, not three, and the request is
    waiting for a retry time (parked when only the person can clear it), or dropped."""
    from celerp.connectors.execution import REFUSALS, sweep_reruns

    connector, cid = stub
    calls = _remote_refusing(monkeypatch, outcome)
    # A failure the webhook does not know reaches its caller, after it is paced.
    with contextlib.suppress(RuntimeError):
        await _webhook(connector.name, cid)
    for _ in range(2):
        await _outside(sweep_reruns())
    pacing = REFUSALS.get(outcome, "retry")
    pending, attempts, waiting, parked = await _beat(cid, connector.name)
    assert len(calls) == 1 and connector.calls == []
    if pacing == "drop":
        assert pending == 0
    else:
        assert (pending, attempts, waiting, parked) == (1, 1, True, pacing == "park")


@pytest.mark.parametrize("outcome", _outcomes())
async def test_the_sweeper_tries_a_refused_rerun_once_per_retry_time(stub, monkeypatch, outcome):
    """The same through the sweeper alone; a retry time that passes allows exactly one more
    try, with a longer wait after it, and a park does not pass with time."""
    from celerp.connectors.execution import REFUSALS, record_rerun, sweep_reruns

    connector, cid = stub
    calls = _remote_refusing(monkeypatch, outcome)
    await record_rerun(cid, connector.name, "orders")
    for _ in range(2):
        await _outside(sweep_reruns())
    pacing = REFUSALS.get(outcome, "retry")
    assert len(calls) == 1
    if pacing == "drop":
        assert (await _beat(cid, connector.name))[0] == 0
        return
    await retry_due(cid)
    for _ in range(2):
        await _outside(sweep_reruns())
    pending, attempts, waiting, parked = await _beat(cid, connector.name)
    if pacing == "park":
        assert (len(calls), pending, attempts, waiting, parked) == (1, 1, 1, True, True)
    else:
        assert (len(calls), pending, attempts, waiting, parked) == (2, 1, 2, True, False)


async def test_the_backoff_is_bounded(stub, monkeypatch):
    from celerp.connectors.execution import _RERUN_BACKOFF_MAX, record_rerun, sweep_reruns

    connector, cid = stub
    _remote_refusing(monkeypatch, "relay_unavailable")
    await record_rerun(cid, connector.name, "orders")
    async with get_session_ctx() as s:
        await s.execute(sa.text("UPDATE connector_execution_heartbeats SET rerun_attempts = 40 "
                                "WHERE company_id = :c"), {"c": cid})
        await s.commit()
    await _outside(sweep_reruns())
    async with get_session_ctx() as s:
        wait = await s.scalar(sa.text(
            "SELECT EXTRACT(EPOCH FROM rerun_retry_at - now()) FROM connector_execution_heartbeats "
            "WHERE company_id = :c AND resource = 'orders'"), {"c": cid})
    assert 0 < float(wait) <= _RERUN_BACKOFF_MAX.total_seconds()


async def test_a_park_is_shown_and_survives_a_new_delivery_until_the_persons_sync(stub, monkeypatch):
    """A store that is not the company's parks the rerun: the refusal is on the entity's
    sync history in its own words, a new delivery's own attempt does not unpark it for the
    sweeper, and the person's own sync, once the store is right, runs it."""
    from celerp.connectors import execution
    from celerp.models.sync_run import SyncRun

    connector, cid = stub
    real_prove = execution._prove_store
    calls = _remote_refusing(monkeypatch, "store_changed")
    await execution.record_rerun(cid, connector.name, "orders")
    await _outside(execution.sweep_reruns())
    await _webhook(connector.name, cid)
    await _outside(execution.sweep_reruns())
    assert len(calls) == 2 and (await _beat(cid, connector.name))[3] is True
    async with get_session_ctx() as s:
        shown = (await s.execute(sa.select(SyncRun.entity, SyncRun.status).where(
            SyncRun.company_id == cid))).all()
    assert shown and {tuple(r) for r in shown} == {("orders", "failed")}

    monkeypatch.setattr(execution, "_prove_store", real_prove)
    async with execution.connector_execution(cid, connector, "manual_sync", ["orders"]):
        pass
    assert connector.calls == [{"reconcile": True}]
    assert (await _beat(cid, connector.name))[:2] == (0, 0)


async def test_a_backoff_ends_at_the_persons_own_sync(stub, monkeypatch):
    """A backoff left by an unreachable relay does not hold work back once the person syncs."""
    from celerp.connectors import execution

    connector, cid = stub
    real_prove = execution._prove_store
    _remote_refusing(monkeypatch, "relay_unavailable")
    await execution.record_rerun(cid, connector.name, "orders")
    await _outside(execution.sweep_reruns())
    assert (await _beat(cid, connector.name))[2] is True
    monkeypatch.setattr(execution, "_prove_store", real_prove)
    async with execution.connector_execution(cid, connector, "manual_sync", ["orders"]):
        pass
    assert connector.calls == [{"reconcile": True}]
    assert (await _beat(cid, connector.name))[0] == 0
