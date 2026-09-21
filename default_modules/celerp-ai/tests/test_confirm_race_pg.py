# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Concurrency invariants for confirming a pending agent action (Postgres).

The rollback-isolated ``session`` fixture runs one test inside a single
connection's transaction, so it cannot demonstrate a row lock taken by one
request and awaited by another. These tests open independent, committing
sessions on separate connections against the real test Postgres and prove:

  E1  claim-before-execute: two concurrent confirmations of the same action
      contend on the FOR UPDATE row lock; exactly one claims it, the other sees
      it already executing and returns nothing. The action can never run twice.
  E2  fail-closed after crash: once an action is claimed and committed as
      ``executing``, a process that dies before finalizing leaves it unclaimable.
      It never silently reverts to pending and re-runs.

Each test seeds its own committed rows and deletes them at teardown, so nothing
leaks into other tests sharing the database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from test_helpers import DATABASE_URL

from celerp.ai.conversations import claim_tool_call
from celerp.models.ai import AIConversation, AIMessage
from celerp.models.company import Company, User


@pytest_asyncio.fixture
async def committed_maker(_db_engine):
    """A sessionmaker whose commits persist (separate from the rollback fixture).

    Depends on the session engine so the schema exists even when this module is
    the first to run on its worker. NullPool so each session gets its own
    connection and the event loop stays consistent across the per-test asyncio
    loop.
    """
    engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool,
        connect_args={"server_settings": {"lock_timeout": "10000"}},
    )
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


def _pending_record(tool_call_id="call_1", name="create_contact"):
    now = datetime.now(timezone.utc)
    return {
        "id": tool_call_id,
        "name": name,
        "arguments": {"body": {"name": "Acme"}},
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=15)).isoformat(),
    }


@pytest_asyncio.fixture
async def seeded(committed_maker):
    """Seed a committed conversation + assistant message holding one pending action.

    Yields (company_id, user_id, conversation_id, message_id); deletes everything
    it created at teardown so the shared database is left clean.
    """
    tag = uuid.uuid4().hex[:12]
    async with committed_maker() as s:
        company = Company(name="RaceCo", slug=f"raceco-{tag}", settings={})
        user = User(email=f"race-{tag}@test.invalid", name="Race")
        s.add_all([company, user])
        await s.flush()
        conv = AIConversation(company_id=company.id, user_id=user.id, title="race")
        s.add(conv)
        await s.flush()
        msg = AIMessage(
            conversation_id=conv.id, role="assistant",
            content="I can create that contact.", tools_called=[_pending_record()],
        )
        s.add(msg)
        await s.flush()
        ids = (company.id, user.id, conv.id, msg.id)
        await s.commit()

    yield ids

    company_id, user_id, conv_id, _ = ids
    async with committed_maker() as s:
        await s.execute(delete(AIConversation).where(AIConversation.id == conv_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


@pytest.mark.asyncio
async def test_concurrent_confirms_claim_once(committed_maker, seeded):
    """E1: two concurrent confirmations, only one wins the claim."""
    company_id, user_id, conv_id, msg_id = seeded

    async def claim() -> dict | None:
        async with committed_maker() as s:
            record = await claim_tool_call(
                s, conversation_id=conv_id, message_id=msg_id,
                tool_call_id="call_1", company_id=company_id, user_id=user_id,
            )
            await s.commit()
            return record

    first, second = await asyncio.gather(claim(), claim())
    winners = [r for r in (first, second) if r is not None]
    assert len(winners) == 1
    assert winners[0]["name"] == "create_contact"

    # The persisted record is executing, not pending.
    async with committed_maker() as s:
        msg = await s.get(AIMessage, msg_id)
        assert msg.tools_called[0]["status"] == "executing"


@pytest.mark.asyncio
async def test_crashed_execution_stays_unclaimable(committed_maker, seeded):
    """E2: an action claimed then abandoned (crash before finalize) never re-runs."""
    company_id, user_id, conv_id, msg_id = seeded

    # First confirmation claims and commits the executing state, then "crashes"
    # before finalizing (we simply never finalize).
    async with committed_maker() as s:
        claimed = await claim_tool_call(
            s, conversation_id=conv_id, message_id=msg_id,
            tool_call_id="call_1", company_id=company_id, user_id=user_id,
        )
        await s.commit()
    assert claimed is not None

    # A retry after the crash finds the action locked in executing: no re-claim.
    async with committed_maker() as s:
        again = await claim_tool_call(
            s, conversation_id=conv_id, message_id=msg_id,
            tool_call_id="call_1", company_id=company_id, user_id=user_id,
        )
        await s.commit()
    assert again is None
