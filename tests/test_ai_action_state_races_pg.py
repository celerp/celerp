# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Every change to a message's action records goes through one locked, refreshed
transition, so two confirmations of one message never overwrite each other's outcome.

Each test makes both sessions load the message before either writes (the copy a
session keeps with expire_on_commit=False), then lets them write in either order; every
outcome must persist on PostgreSQL.
"""

from __future__ import annotations

import asyncio
import re
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.ai import conversations
from celerp.models.ai import AIConversation, AIMessage
from celerp.models.company import Company, User
from shipped_sources import REPO, shipped_sources

pytestmark = pytest.mark.asyncio

_DONE = {"status": 200, "data": {"id": "x"}}


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


def _record(tool_call_id: str, status: str) -> dict:
    now = datetime.now(timezone.utc)
    return {"id": tool_call_id, "name": "items.update", "status": status, "keyed": True,
            "executing_since": now.isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat()}


async def _message(factory, *records: dict) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    company_id, user_id, conversation_id, message_id = (uuid.uuid4() for _ in range(4))
    async with factory() as s:
        s.add(User(id=user_id, email=f"u-{user_id.hex[:8]}@example.test", name="Owner", auth_hash="x",
                   is_active=True))
        await s.flush()
        s.add(Company(id=company_id, name="RaceCo", slug=f"race-{company_id.hex[:8]}", settings={}))
        await s.flush()
        s.add(AIConversation(id=conversation_id, company_id=company_id, user_id=user_id))
        await s.flush()
        s.add(AIMessage(id=message_id, conversation_id=conversation_id, role="assistant", content="",
                        tools_called=list(records)))
        await s.commit()
    return company_id, user_id, conversation_id, message_id


async def _stored(factory, message_id) -> dict:
    async with factory() as s:
        msg = await s.get(AIMessage, message_id)
        return {r["id"]: r for r in msg.tools_called if isinstance(r, dict)}


async def _both_loaded(factory, message_id):
    """Two sessions that each hold a copy of the message from before either writes."""
    first, second = factory(), factory()
    for s in (first, second):
        # The identity map holds objects weakly: keep the copy, as a request handler would.
        s.info["held"] = await s.get(AIMessage, message_id)
        assert s.info["held"].tools_called
    return first, second


def _finish(tool_call_id: str):
    return lambda s, message_id: conversations.finalize_tool_call(
        s, message_id=message_id, tool_call_id=tool_call_id, status="completed", result=_DONE)


@pytest.mark.parametrize("first_wins", [True, False])
async def test_two_confirmations_of_one_message_both_persist(committed_engine, first_wins):
    factory = _factory(committed_engine)
    *_, message_id = await _message(factory, _record("a", "executing"), _record("b", "executing"))
    first, second = await _both_loaded(factory, message_id)
    order = [(first, _finish("a")), (second, _finish("b"))]
    for s, write in (order if first_wins else order[::-1]):
        await write(s, message_id)
        await s.commit()
        await s.close()
    stored = await _stored(factory, message_id)
    assert stored["a"]["status"] == stored["b"]["status"] == "completed"


async def test_a_waiting_confirmation_writes_over_the_committed_state(committed_engine):
    """The second writer blocks on the conversation lock while the first holds it, then
    writes over what the first committed, not over the copy it loaded."""
    factory = _factory(committed_engine)
    *_, message_id = await _message(factory, _record("a", "executing"), _record("b", "executing"))
    first, second = await _both_loaded(factory, message_id)
    await _finish("a")(first, message_id)
    waiting = asyncio.create_task(_finish("b")(second, message_id))
    await asyncio.sleep(0.5)
    assert not waiting.done()  # held by the first writer's lock
    await first.commit()
    await waiting
    await second.commit()
    for s in (first, second):
        await s.close()
    stored = await _stored(factory, message_id)
    assert stored["a"]["status"] == stored["b"]["status"] == "completed"


@pytest.mark.parametrize("note_first", [True, False])
@pytest.mark.parametrize("note", ["pending", "outcomes"])
async def test_a_note_and_a_confirmation_both_persist(committed_engine, note, note_first):
    factory = _factory(committed_engine)
    *_, message_id = await _message(factory, _record("a", "executing"),
                                    _record("b", "pending" if note == "pending" else "executing"))
    first, second = await _both_loaded(factory, message_id)
    if note == "pending":
        error = {"code": "earlier_change_failed"}
        noting = lambda s, m: conversations.note_pending(s, message_id=m, tool_call_id="b", error=error)  # noqa: E731
    else:
        noting = lambda s, m: conversations.note_outcomes(  # noqa: E731
            s, message_id=m, tool_call_id="b", outcomes=[{"record": "r1", "status": "completed"}])
    order = [(first, noting), (second, _finish("a"))]
    for s, write in (order if note_first else order[::-1]):
        await write(s, message_id)
        await s.commit()
        await s.close()
    stored = await _stored(factory, message_id)
    assert stored["a"]["status"] == "completed"
    if note == "pending":
        assert stored["b"]["status"] == "pending" and stored["b"]["error"] == {"code": "earlier_change_failed"}
    else:
        assert stored["b"]["outcomes"] == [{"record": "r1", "status": "completed"}]


@pytest.mark.filterwarnings("ignore")
def test_only_the_transition_assigns_tools_called():
    """Any other assignment of a message's action records could write a stale copy back."""
    assignment = re.compile(r"\.tools_called\s*=(?!=)")
    found = sorted(
        f"{rel}:{n}" for rel, path in shipped_sources()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if assignment.search(line))
    assert len(found) == 1 and found[0].startswith("celerp/ai/conversations.py:"), found
    source = (REPO / "celerp/ai/conversations.py").read_text(encoding="utf-8")
    owner = source[source.index("async def change_tools_called("):source.index("def _replace(")]
    assert assignment.search(owner)


# The continuation: claimed once per proposal message, only when every action is done.

async def _continue(factory, conversation, message_id):
    from celerp_ai import routes as ai_routes

    company_id, user_id, conversation_id = conversation
    async with factory() as s:
        return await ai_routes._continue_request(
            s, conversation_id=conversation_id, message_id=message_id, company_id=company_id, user_id=user_id,
            auth=types.SimpleNamespace(snonce=None))


async def _questions(factory, conversation_id) -> int:
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(AIMessage).where(
            AIMessage.conversation_id == conversation_id, AIMessage.role == "user"))


@pytest.fixture
def started(monkeypatch):
    from celerp.ai import jobs

    ids: list[uuid.UUID] = []
    monkeypatch.setattr(jobs, "start_job", ids.append)
    return ids


async def test_no_continuation_while_another_action_still_runs(committed_engine, started):
    factory = _factory(committed_engine)
    *conversation, message_id = await _message(factory, _record("a", "executing"), _record("b", "executing"))
    async with factory() as s:
        await _finish("a")(s, message_id)
        await s.commit()
    assert await _continue(factory, conversation, message_id) is None
    assert await _questions(factory, conversation[2]) == 0 and not started
    async with factory() as s:
        await _finish("b")(s, message_id)
        await s.commit()
    job_id = await _continue(factory, conversation, message_id)
    assert job_id and started == [uuid.UUID(job_id)]


async def test_concurrent_completions_start_one_continuation_and_a_retry_gets_it(committed_engine, started):
    factory = _factory(committed_engine)
    *conversation, message_id = await _message(factory, _record("a", "completed"), _record("b", "completed"))
    first, second = await asyncio.gather(*(_continue(factory, conversation, message_id) for _ in range(2)))
    assert first and first == second
    assert await _continue(factory, conversation, message_id) == first
    assert await _questions(factory, conversation[2]) == 1 and started == [uuid.UUID(first)]
