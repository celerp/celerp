# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The usage report reads the usage record only: it never waits on the AI service.
Rows whose charge has not come back show as awaiting confirmation. A background pass
settles them with one batch status read, only after each row's own reconcile time,
and only when no send under the same question moved that time meanwhile.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from celerp.ai import deadlines, jobs, llm, usage
from celerp.models.ai import AIUsage
from test_ai_usage import _usage, owner  # noqa: F401


def _status(op, state, credits=0) -> dict:
    return {"operation_id": str(op), "state": state, "credits": credits,
            "outcome": "answered" if state == "charged" else None, "charged_at": None}


def _batch(monkeypatch, answer) -> list[list[uuid.UUID]]:
    """Answer llm.operations_status with ``answer(ids)`` (a list, None for an older
    service, or an exception); returns the id lists asked."""
    asked: list = []

    async def _operations_status(ids):
        asked.append(list(ids))
        result = answer(ids)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(llm, "operations_status", _operations_status)
    return asked


async def _pending(session, company_id, user_id, n: int, *, due: bool = True) -> list[uuid.UUID]:
    now = datetime.now(timezone.utc)
    ops = [uuid.uuid4() for _ in range(n)]
    for op in ops:
        session.add(AIUsage(company_id=company_id, user_id=user_id, operation_id=op, credits=0, pending=True,
                            created_at=now - timedelta(minutes=10),
                            reconcile_at=now - timedelta(seconds=1) if due else now + timedelta(minutes=5)))
    await session.commit()
    return ops


async def _rows(session) -> dict:
    session.expire_all()
    return {r.operation_id: (r.credits, r.pending)
            for r in (await session.execute(select(AIUsage))).scalars().all()}


# ── Red 13: the report never calls the service ────────────────────────────────

async def test_usage_report_under_50_slow_pending_ops_answers_from_the_record(client, session, owner, monkeypatch):
    token, company_id, user_id = owner
    await _pending(session, company_id, user_id, 50)

    async def _slow(*_a, **_k):
        await asyncio.sleep(60)
    monkeypatch.setattr(llm, "operations_status", _slow)
    monkeypatch.setattr(llm, "operation_status", _slow, raising=False)

    started = time.monotonic()
    r = await _usage(client, token)
    assert r.status_code == 200
    assert time.monotonic() - started < 5
    (row,) = r.json()["users"]
    assert row["pending_count"] == 50
    assert (row["query_count"], row["credits_used"]) == (0, 0)


async def test_usage_report_shows_confirmed_and_awaiting_side_by_side(client, session, owner):
    token, company_id, user_id = owner
    session.add(AIUsage(company_id=company_id, user_id=user_id, operation_id=uuid.uuid4(), credits=2,
                        pending=False, created_at=datetime.now(timezone.utc),
                        reconcile_at=datetime.now(timezone.utc)))
    await _pending(session, company_id, user_id, 3)
    (row,) = (await _usage(client, token)).json()["users"]
    assert (row["query_count"], row["credits_used"], row["pending_count"]) == (1, 2, 3)


def test_usage_stats_holds_no_service_call():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "default_modules" / "celerp-ai" / "celerp_ai" / "routes.py").read_text()
    body = src.split("async def usage_stats(", 1)[1].split("\n@router", 1)[0]
    assert "reconcile_usage" not in body and "operations_status" not in body


# ── The reconcile pass ────────────────────────────────────────────────────────

async def test_one_pass_is_one_batch_call_of_at_most_50_due_rows(session, owner, monkeypatch):
    _, company_id, user_id = owner
    due = await _pending(session, company_id, user_id, 60)
    later = await _pending(session, company_id, user_id, 2, due=False)
    asked = _batch(monkeypatch, lambda ids: [_status(i, "charged", 1) for i in ids])
    await usage.reconcile_usage(session)
    assert len(asked) == 1 and len(asked[0]) == 50
    assert set(asked[0]) <= set(due) and not set(asked[0]) & set(later)
    rows = await _rows(session)
    assert sum(1 for op in due if rows[op] == (1, False)) == 50
    assert all(rows[op] == (0, True) for op in later)


async def test_nothing_due_makes_no_call(session, owner, monkeypatch):
    _, company_id, user_id = owner
    await _pending(session, company_id, user_id, 3, due=False)
    asked = _batch(monkeypatch, lambda ids: [])
    await usage.reconcile_usage(session)
    assert asked == []


async def test_each_state_settles_as_the_contract_says(session, owner, monkeypatch):
    _, company_id, user_id = owner
    charged, not_charged, running, unknown = await _pending(session, company_id, user_id, 4)
    _batch(monkeypatch, lambda ids: [_status(charged, "charged", 2), _status(not_charged, "not_charged"),
                                     _status(running, "in_progress"), _status(unknown, "unknown_id")])
    await usage.reconcile_usage(session)
    assert await _rows(session) == {charged: (2, False), not_charged: (0, False),
                                    running: (0, True), unknown: (0, True)}


async def test_an_older_service_settles_on_what_the_replies_reported(session, owner, monkeypatch):
    _, company_id, user_id = owner
    (op,) = await _pending(session, company_id, user_id, 1)
    await session.execute(update(AIUsage).values(credits=1))
    await session.commit()
    _batch(monkeypatch, lambda ids: None)
    await usage.reconcile_usage(session)
    assert await _rows(session) == {op: (1, False)}


@pytest.mark.parametrize("failure", [llm.RelayError("gateway_error", "x", status=500), TimeoutError()])
async def test_any_error_changes_nothing(session, owner, monkeypatch, failure):
    _, company_id, user_id = owner
    ops = await _pending(session, company_id, user_id, 3)
    _batch(monkeypatch, lambda ids: failure)
    await usage.reconcile_usage(session)
    assert await _rows(session) == {op: (0, True) for op in ops}


async def test_a_send_after_the_read_keeps_the_row_pending(session, owner, monkeypatch):
    """Finding 6: the row is resent under its id between the status read and the
    update. The update is conditional on the reconcile time it read, so the row stays
    pending and is read again after its new time."""
    _, company_id, user_id = owner
    (op,) = await _pending(session, company_id, user_id, 1)

    async def _operations_status(ids):
        await usage.record_usage(session, company_id=company_id, user_id=user_id, operation_id=op, send_bytes=0)
        return [_status(op, "not_charged")]
    monkeypatch.setattr(llm, "operations_status", _operations_status)
    await usage.reconcile_usage(session)
    session.expire_all()
    row = (await session.execute(select(AIUsage))).scalar_one()
    assert row.pending and row.reconcile_at > datetime.now(timezone.utc)


async def test_a_row_held_by_a_running_job_is_not_due(session, owner, monkeypatch):
    """G2: a resumed step queued at the gate is never fenced before it is sent."""
    _, company_id, user_id = owner
    held, free = await _pending(session, company_id, user_id, 2)
    monkeypatch.setattr(jobs, "held_operation_ids", lambda: {held})
    asked = _batch(monkeypatch, lambda ids: [_status(i, "not_charged") for i in ids])
    await usage.reconcile_usage(session)
    assert asked == [[free]]


async def test_outage_reply_without_charge_stays_awaiting(session, owner):
    """A reply with operation_credits null (the service could not settle) leaves the
    row pending and shown as awaiting, never as charged."""
    _, company_id, user_id = owner
    op = uuid.uuid4()
    await usage.record_usage(session, company_id=company_id, user_id=user_id, operation_id=op,
                             credits=1, known=False, send_bytes=0)
    await session.commit()
    assert (await _rows(session))[op] == (1, True)


# ── reconcile_at is written before every send ─────────────────────────────────

async def test_every_send_writes_its_reconcile_time(session, owner, monkeypatch):
    _, company_id, user_id = owner
    limits = deadlines.CURRENT_RELAY_DEFAULTS

    async def _limits():
        return limits
    monkeypatch.setattr(deadlines, "limits", _limits)
    op = uuid.uuid4()
    for nbytes in (0, 5 * 1024 * 1024):
        before = datetime.now(timezone.utc)
        await usage.record_usage(session, company_id=company_id, user_id=user_id, operation_id=op,
                                 send_bytes=nbytes)
        await session.commit()
        session.expire_all()
        row = (await session.execute(select(AIUsage))).scalar_one()
        expected = timedelta(seconds=deadlines.reconcile_delay_s(limits, nbytes))
        assert before + expected - timedelta(seconds=2) <= row.reconcile_at <= datetime.now(timezone.utc) + expected
        assert row.pending


def test_reconcile_at_is_required():
    assert AIUsage.__table__.c.reconcile_at.nullable is False


def test_the_reconcile_loop_starts_after_recovery():
    """G2 boot order, one lifespan sequence: recover_on_start completes before the
    reconcile loop is started."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "celerp" / "main.py").read_text()
    assert "recover_on_start" in src and "reconcile_usage_loop" in src
    assert src.index("await jobs.recover_on_start(") < src.index("reconcile_usage_loop(")
