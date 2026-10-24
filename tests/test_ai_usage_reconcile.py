# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The usage record holds one row per question the AI service was asked, keyed by the
question's operation id, with what the service charged for that operation. A row whose
charge did not come back (the reply was lost, the app restarted) is pending until the
usage report reads the operation's status from the service; retries never add twice.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from celerp.ai import llm
from celerp.ai.llm import ModelResult, RelayError
from celerp.models.ai import AIUsage
from test_ai_usage import _answer, _ask, _claims, _conversation, _counts, _relay, _usage, owner  # noqa: F401
from test_helpers import invite_user


def _replayed(credits: int = 1) -> ModelResult:
    """The service's reply to a retry of a charged question: nothing added, the
    operation's whole charge named."""
    return ModelResult(message={"role": "assistant", "content": "Open Contacts and choose New."},
                       model_used="test-model", usage={"credits": 0}, reservation_id=None, remaining=10,
                       operation_credits=credits)


def _status(monkeypatch, states: dict) -> list:
    """Answer operation status reads from ``states`` (operation id -> status, None for
    an older service, or an exception to raise); returns the ids read."""
    read: list = []

    async def _operation_status(operation_id):
        read.append(operation_id)
        state = states[operation_id]
        if isinstance(state, Exception):
            raise state
        return state

    monkeypatch.setattr(llm, "operation_status", _operation_status)
    return read


def _charged(op: uuid.UUID, credits: int = 1) -> dict:
    return {"operation_id": str(op), "state": "charged", "outcome": "answered", "credits": credits,
            "charged_at": "2026-10-25T08:00:00+00:00"}


def _state(op: uuid.UUID, state: str) -> dict:
    return {"operation_id": str(op), "state": state, "outcome": None, "credits": 0, "charged_at": None}


async def _age(session, minutes: int = 5) -> None:
    """Let every row pass the reconcile wait, as time would."""
    await session.execute(update(AIUsage).values(
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes)))
    await session.commit()


async def _rows(session) -> dict:
    session.expire_all()
    rows = (await session.execute(select(AIUsage))).scalars().all()
    return {r.operation_id: (r.credits, r.pending) for r in rows}


async def test_lost_reply_is_reconciled_from_the_service_after_a_restart(client, session, owner, monkeypatch):
    """The service answered and charged, the whole reply was lost, and the app was
    restarted before anyone asked again: the usage report reads the charge back."""
    token, _, _ = owner
    _relay(monkeypatch, [httpx.ReadTimeout("slow")])
    conv = await _conversation(client, token)
    question = uuid.uuid4()
    assert (await _ask(client, token, conv, question)).json()["error_code"] == "timeout"
    assert await _rows(session) == {question: (0, True)}

    read = _status(monkeypatch, {question: _charged(question)})
    assert _counts((await _usage(client, token)).json()["users"]) == []  # too recent to ask
    await _age(session)
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 1)]
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 1)]
    assert read == [question]  # a known charge is never read again
    assert await _rows(session) == {question: (1, False)}


async def test_retry_after_a_lost_reply_records_the_operation_charge(client, session, owner, monkeypatch):
    token, _, _ = owner
    _relay(monkeypatch, [httpx.ReadTimeout("slow"), _replayed()])
    _status(monkeypatch, {})  # the retry's reply names the charge: no status read
    conv = await _conversation(client, token)
    question = uuid.uuid4()
    await _ask(client, token, conv, question)
    await _ask(client, token, conv, question)
    await _age(session)
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 1)]
    assert await _rows(session) == {question: (1, False)}


async def test_a_charge_is_never_counted_twice(client, session, owner, monkeypatch):
    """A one-off query sent twice under one id, a reconcile that read the charge before
    the reply arrived, and a late reply: one row, one charge."""
    token, _, _ = owner
    headers = {"Authorization": f"Bearer {token}"}
    operation = uuid.uuid4()
    _relay(monkeypatch, [_answer(credits=1), _replayed(), _replayed()])
    for _ in range(2):
        r = await client.post("/ai/query", headers=headers, json={"query": "supplier?", "operation_id": str(operation)})
        assert r.status_code == 200, r.text
    assert await _rows(session) == {operation: (1, False)}

    late = uuid.uuid4()
    await session.execute(update(AIUsage).values(pending=True))  # as if the reply had not come back
    await session.commit()
    _status(monkeypatch, {operation: _charged(operation), late: _charged(late)})
    await _age(session)
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 1)]
    r = await client.post("/ai/query", headers=headers, json={"query": "supplier?", "operation_id": str(operation)})
    assert r.status_code == 200, r.text
    assert await _rows(session) == {operation: (1, False)}


async def test_reconcile_follows_each_state_of_the_operation(client, session, owner, monkeypatch):
    """Charged and not charged are final; a question still running, a service that
    cannot be reached, stay pending and are read again; an older service that has no
    operation status leaves the charge the replies reported, final."""
    token, _, _ = owner
    errors = [httpx.ReadTimeout("slow")] * 5
    _relay(monkeypatch, errors)
    conv = await _conversation(client, token)
    ops = [uuid.uuid4() for _ in range(5)]
    for op in ops:
        await _ask(client, token, conv, op)
    charged, refused, running, unreachable, older = ops
    read = _status(monkeypatch, {
        charged: _charged(charged, 2), refused: _state(refused, "not_charged"),
        running: _state(running, "in_progress"), unreachable: RelayError("no_session", "signed out"),
        older: None,
    })
    await _age(session)
    # Rows are read oldest first and a service that cannot be reached ends the pass, so
    # the unreachable question is the newest here.
    await session.execute(update(AIUsage).where(AIUsage.operation_id == unreachable).values(
        created_at=datetime.now(timezone.utc) - timedelta(minutes=2)))
    await session.commit()
    assert _counts((await _usage(client, token)).json()["users"]) == [(1, 2)]
    assert await _rows(session) == {charged: (2, False), refused: (0, False), running: (0, True),
                                    unreachable: (0, True), older: (0, False)}
    read.clear()
    await _usage(client, token)
    assert sorted(map(str, read)) == sorted(map(str, [running, unreachable]))


async def test_each_user_keeps_their_own_charges(client, session, owner, monkeypatch):
    token, company_id, owner_id = owner
    member = await invite_user(client, session, {"Authorization": f"Bearer {token}"}, "m@example.test", "admin")
    _, member_id = _claims(member)
    answered = _answer(credits=1)
    answered.operation_credits = 1
    _relay(monkeypatch, [httpx.ReadTimeout("slow"), answered])
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _ask(client, token, await _conversation(client, token), mine)
    await _ask(client, member, await _conversation(client, member), theirs)
    _status(monkeypatch, {mine: _charged(mine, 3)})
    await _age(session)
    users = (await _usage(client, token)).json()["users"]
    assert {u["user_id"]: (u["query_count"], u["credits_used"]) for u in users} == {
        str(owner_id): (1, 3), str(member_id): (1, 1)}


async def test_a_module_query_cut_short_is_held_for_reconcile(session, monkeypatch):
    """The app stopped while a module's question was with the service: its row was
    written before the question left, so the charge is not lost."""
    from celerp.modules.api import ai_query
    from test_helpers import seed_member, signed_request

    monkeypatch.setattr("celerp.session_gate.get_session_token", lambda: "session-1")
    _relay(monkeypatch, [asyncio.CancelledError()])  # the app shutting down
    company_id, user_id = await seed_member(session)
    async with signed_request(session, company_id, user_id):
        with pytest.raises(asyncio.CancelledError):
            await ai_query(query="hello", company_id=str(company_id), session_token="session-1", db_session=session)
    assert list((await _rows(session)).values()) == [(0, True)]
