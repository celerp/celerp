# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/conversations.py"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.models.base import Base
from celerp.models.company import Company, User
from celerp.models.ai import AIConversation, AIMessage
from celerp.ai.conversations import (
    MAX_CONVERSATIONS_PER_COMPANY,
    MAX_MESSAGES_PER_CONVERSATION,
    HISTORY_TOKEN_BUDGET,
    _CHARS_PER_TOKEN,
    add_message,
    build_history_context,
    create_conversation,
    delete_conversation,
    get_conversation,
    get_messages,
    list_conversations,
    rename_conversation,
)

_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest_asyncio.fixture
async def company(session) -> Company:
    c = Company(name="ConvCo", slug="convco", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


@pytest_asyncio.fixture
async def user(session, company) -> User:
    u = User(company_id=company.id, email="conv@test.com", name="Test", role="owner")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest_asyncio.fixture
async def user_b(session, company) -> User:
    u = User(company_id=company.id, email="other@test.com", name="Other", role="user")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest_asyncio.fixture
async def company_b(session) -> Company:
    c = Company(name="OtherCo", slug="otherco2", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


@pytest_asyncio.fixture
async def user_b_co(session, company_b) -> User:
    u = User(company_id=company_b.id, email="bo@other.com", name="Bo", role="owner")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


# ── create ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_conversation(session, company, user):
    conv = await create_conversation(session, company.id, user.id, title="My chat")
    await session.commit()
    assert conv.id is not None
    assert conv.title == "My chat"
    assert conv.company_id == company.id
    assert conv.user_id == user.id


@pytest.mark.asyncio
async def test_create_conversation_no_title(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    await session.commit()
    assert conv.title is None


@pytest.mark.asyncio
async def test_create_conversation_sets_title_from_query(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    await add_message(session, conv.id, "user", "How much inventory do I have?")
    await session.commit()
    await session.refresh(conv)
    assert conv.title == "How much inventory do I have?"


@pytest.mark.asyncio
async def test_conversation_limit_per_company(session, company, user):
    for i in range(MAX_CONVERSATIONS_PER_COMPANY + 5):
        await create_conversation(session, company.id, user.id, title=f"Conv {i}")
    await session.commit()

    convs = await list_conversations(session, company.id, user.id, limit=200)
    assert len(convs) <= MAX_CONVERSATIONS_PER_COMPANY


# ── list ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_conversations_newest_first(session, company, user):
    c1 = await create_conversation(session, company.id, user.id, title="First")
    c2 = await create_conversation(session, company.id, user.id, title="Second")
    await session.commit()

    convs = await list_conversations(session, company.id, user.id)
    assert len(convs) == 2
    assert convs[0].title == "Second"
    assert convs[1].title == "First"


@pytest.mark.asyncio
async def test_list_conversations_pagination(session, company, user):
    for i in range(5):
        await create_conversation(session, company.id, user.id, title=f"C{i}")
    await session.commit()

    page1 = await list_conversations(session, company.id, user.id, limit=2, offset=0)
    page2 = await list_conversations(session, company.id, user.id, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert page1[0].id != page2[0].id


# ── get ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_conversation_with_messages(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    await add_message(session, conv.id, "user", "Hello")
    await add_message(session, conv.id, "assistant", "Hi there!")
    await session.commit()

    result = await get_conversation(session, conv.id, company.id, user.id)
    assert result is not None
    msgs = await get_messages(session, conv.id)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"


# ── delete ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_conversation_cascades_messages(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    await add_message(session, conv.id, "user", "Hello")
    await add_message(session, conv.id, "assistant", "Hi")
    await session.commit()

    deleted = await delete_conversation(session, conv.id, company.id, user.id)
    await session.commit()
    assert deleted is True

    # Conversation gone
    result = await get_conversation(session, conv.id, company.id, user.id)
    assert result is None


@pytest.mark.asyncio
async def test_delete_conversation_not_found(session, company, user):
    deleted = await delete_conversation(session, uuid.uuid4(), company.id, user.id)
    assert deleted is False


# ── rename ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rename_conversation(session, company, user):
    conv = await create_conversation(session, company.id, user.id, title="Old")
    await session.commit()

    result = await rename_conversation(session, conv.id, company.id, user.id, "New Title")
    await session.commit()
    assert result is not None
    assert result.title == "New Title"


# ── messages ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_query_in_conversation_adds_messages(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    await add_message(session, conv.id, "user", "Question 1")
    await add_message(session, conv.id, "assistant", "Answer 1", model_used="test-model", tools_called=["kpis"])
    await session.commit()

    msgs = await get_messages(session, conv.id)
    assert len(msgs) == 2
    assert msgs[1].model_used == "test-model"
    assert msgs[1].tools_called == ["kpis"]


@pytest.mark.asyncio
async def test_message_limit_per_conversation(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    for i in range(MAX_MESSAGES_PER_CONVERSATION + 10):
        await add_message(session, conv.id, "user" if i % 2 == 0 else "assistant", f"Msg {i}")
    await session.commit()

    msgs = await get_messages(session, conv.id, limit=300)
    assert len(msgs) <= MAX_MESSAGES_PER_CONVERSATION


# ── history context ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_token_budget_truncates(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    # Each message ~100 chars = 25 tokens. Budget 4000 / 25 = 160 messages max.
    # But let's create a long message that exceeds budget.
    long_msg = "x" * (HISTORY_TOKEN_BUDGET * _CHARS_PER_TOKEN + 100)
    await add_message(session, conv.id, "user", long_msg)
    await add_message(session, conv.id, "assistant", "Short answer")
    await session.commit()

    msgs = await get_messages(session, conv.id)
    history = build_history_context(msgs)
    # The long message should be excluded (exceeds budget)
    # Only the short answer fits
    assert len(history) == 1
    assert history[0]["content"] == "Short answer"


def test_build_history_context_empty():
    result = build_history_context([])
    assert result == []


@pytest.mark.asyncio
async def test_history_excludes_file_content(session, company, user):
    """File IDs are stored but file content is not re-sent in history."""
    conv = await create_conversation(session, company.id, user.id)
    await add_message(session, conv.id, "user", "Process this", file_ids=["ai_up_abc"])
    await add_message(session, conv.id, "assistant", "Processed!")
    await session.commit()

    msgs = await get_messages(session, conv.id)
    history = build_history_context(msgs)
    # History should contain the text but no file data
    assert len(history) == 2
    assert history[0]["content"] == "Process this"
    assert "file" not in str(history[0])  # No file key in history dict


# ── isolation ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_isolation_between_companies(session, company, company_b, user, user_b_co):
    c1 = await create_conversation(session, company.id, user.id, title="CoA")
    c2 = await create_conversation(session, company_b.id, user_b_co.id, title="CoB")
    await session.commit()

    # User A can't see User B's company's conversation
    result = await get_conversation(session, c2.id, company.id, user.id)
    assert result is None


@pytest.mark.asyncio
async def test_isolation_between_users(session, company, user, user_b):
    c1 = await create_conversation(session, company.id, user.id, title="UserA's")
    await session.commit()

    # User B in same company can't see User A's conversation
    result = await get_conversation(session, c1.id, company.id, user_b.id)
    assert result is None

    # User B sees empty list
    convs = await list_conversations(session, company.id, user_b.id)
    assert len(convs) == 0
