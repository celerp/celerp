# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/conversations.py"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company, User
from celerp.models.ai import AIBatchJob, AIConversation, AIMessage
from types import SimpleNamespace

from celerp.ai.conversations import (
    EXECUTING_STALE_S,
    MAX_CONVERSATIONS_PER_USER,
    UNFINISHED_ACTION_TEXT,
    MAX_MESSAGES_PER_CONVERSATION,
    HISTORY_TOKEN_BUDGET,
    _CHARS_PER_TOKEN,
    add_message,
    build_history_context,
    claim_tool_call,
    create_conversation,
    delete_conversation,
    dismiss_tool_call,
    finalize_tool_call,
    get_conversation,
    get_messages,
    list_conversation_history,
    list_conversations,
    pending_actions,
    rename_conversation,
    tool_names,
)



# `session` (Postgres, rollback-isolated) comes from the root conftest.


@pytest_asyncio.fixture
async def company(session) -> Company:
    c = Company(name="ConvCo", slug="convco", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


@pytest_asyncio.fixture
async def user(session, company) -> User:
    u = User(email="conv@test.com", name="Test")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest_asyncio.fixture
async def user_b(session, company) -> User:
    u = User(email="other@test.com", name="Other")
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
    u = User(email="bo@other.com", name="Bo")
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
async def test_conversation_limit_per_user(session, company, user):
    for i in range(MAX_CONVERSATIONS_PER_USER + 5):
        await create_conversation(session, company.id, user.id, title=f"Conv {i}")
    await session.commit()

    convs = await list_conversations(session, company.id, user.id, limit=200)
    assert len(convs) <= MAX_CONVERSATIONS_PER_USER


@pytest.mark.asyncio
async def test_conversation_pruning_preserves_unresolved_action(session, company, user):
    protected = await create_conversation(session, company.id, user.id, title="Needs decision")
    await add_message(
        session, protected.id, "assistant", "I can do that.",
        tools_called=[_pending_record("keep-pending")],
    )
    await session.commit()
    protected_id = protected.id

    for i in range(MAX_CONVERSATIONS_PER_USER):
        await create_conversation(session, company.id, user.id, title=f"New {i}")
    await session.commit()

    assert await get_conversation(session, protected_id, company.id, user.id) is not None
    convs = await list_conversations(session, company.id, user.id, limit=MAX_CONVERSATIONS_PER_USER + 10)
    assert len(convs) == MAX_CONVERSATIONS_PER_USER + 1


@pytest.mark.asyncio
async def test_pruning_is_scoped_per_user(session, company, user, user_b):
    """One busy user hitting the cap must not delete another user's threads."""
    keep = await create_conversation(session, company.id, user_b.id, title="B keeps this")
    await session.commit()
    keep_id = keep.id

    for i in range(MAX_CONVERSATIONS_PER_USER + 5):
        await create_conversation(session, company.id, user.id, title=f"A {i}")
    await session.commit()

    # User A pruned to the cap; user B's single conversation is untouched.
    a_convs = await list_conversations(session, company.id, user.id, limit=500)
    assert len(a_convs) <= MAX_CONVERSATIONS_PER_USER
    assert await get_conversation(session, keep_id, company.id, user_b.id) is not None


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


@pytest.mark.asyncio
async def test_message_pruning_preserves_unresolved_action(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    protected = await add_message(
        session, conv.id, "assistant", "I can do that.",
        tools_called=[_pending_record("keep-pending")],
    )
    await session.commit()
    protected_id = protected.id

    for i in range(MAX_MESSAGES_PER_CONVERSATION):
        await add_message(session, conv.id, "user", f"m{i}")
    await session.commit()

    assert await session.get(AIMessage, protected_id) is not None
    msgs = await get_messages(session, conv.id, limit=MAX_MESSAGES_PER_CONVERSATION + 10)
    assert len(msgs) == MAX_MESSAGES_PER_CONVERSATION + 1


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


# ── newest-N messages ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_messages_returns_newest_n_chronological(session, company, user):
    """get_messages(limit=N) selects the newest N and returns them oldest-first."""
    conv = await create_conversation(session, company.id, user.id)
    for i in range(5):
        await add_message(session, conv.id, "user", f"m{i}")
    await session.commit()

    msgs = await get_messages(session, conv.id, limit=3)
    assert [m.content for m in msgs] == ["m2", "m3", "m4"]


# ── tool-call record helpers ───────────────────────────────────────────────

def _pending_record(tool_call_id="call_1", name="create_contact", *, minutes=15):
    now = datetime.now(timezone.utc)
    return {
        "id": tool_call_id,
        "name": name,
        "arguments": {"body": {"name": "Acme"}},
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=minutes)).isoformat(),
    }


def test_tool_names_mixes_strings_and_records():
    assert tool_names(None) == []
    assert tool_names(["list_items", {"id": "c1", "name": "create_contact"}]) == [
        "list_items", "create_contact",
    ]


def test_pending_actions_filters_status_and_expiry():
    fresh = _pending_record("keep")
    now = datetime.now(timezone.utc)
    executing = {**_pending_record("busy"), "status": "executing", "executing_since": now.isoformat()}
    completed = {"id": "done", "name": "x", "status": "completed"}
    expired = _pending_record("gone", minutes=-1)
    result = pending_actions(["list_items", fresh, executing, completed, expired])
    assert [r["id"] for r in result] == ["keep"]


def test_pending_actions_reports_stale_executing_as_retryable():
    """A claim that never finalized can safely retry the same immutable action id."""
    stale_since = (datetime.now(timezone.utc) - timedelta(seconds=EXECUTING_STALE_S + 1)).isoformat()
    stale = {**_pending_record("lost"), "status": "executing", "executing_since": stale_since}
    result = pending_actions([stale])
    assert result[0]["id"] == "lost"
    assert result[0]["status"] == "retryable"
    assert result[0]["error"] == UNFINISHED_ACTION_TEXT
    history = build_history_context([SimpleNamespace(role="assistant", content="hi", tools_called=[stale])])
    assert "[action awaiting retry: create_contact]" in history[0]["content"]


@pytest_asyncio.fixture
async def assistant_msg(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    msg = await add_message(
        session, conv.id, "assistant", "I can create that contact.",
        tools_called=[_pending_record()],
    )
    await session.commit()
    return conv, msg


@pytest.mark.asyncio
async def test_claim_tool_call_moves_pending_to_executing(session, company, user, assistant_msg):
    conv, msg = assistant_msg
    claimed = await claim_tool_call(
        session, conversation_id=conv.id, message_id=msg.id,
        tool_call_id="call_1", company_id=company.id, user_id=user.id,
    )
    assert claimed is not None
    assert claimed["arguments"] == {"body": {"name": "Acme"}}
    await session.commit()

    await session.refresh(msg)
    assert msg.tools_called[0]["status"] == "executing"
    assert msg.tools_called[0]["executing_since"]

    # A second claim of the same action finds nothing pending.
    again = await claim_tool_call(
        session, conversation_id=conv.id, message_id=msg.id,
        tool_call_id="call_1", company_id=company.id, user_id=user.id,
    )
    assert again is None


@pytest.mark.asyncio
async def test_claim_tool_call_rejects_wrong_owner(session, company, user, user_b, assistant_msg):
    conv, msg = assistant_msg
    claimed = await claim_tool_call(
        session, conversation_id=conv.id, message_id=msg.id,
        tool_call_id="call_1", company_id=company.id, user_id=user_b.id,
    )
    assert claimed is None


@pytest.mark.asyncio
async def test_claim_tool_call_rejects_expired(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    msg = await add_message(
        session, conv.id, "assistant", "offer",
        tools_called=[_pending_record("call_exp", minutes=-1)],
    )
    await session.commit()
    claimed = await claim_tool_call(
        session, conversation_id=conv.id, message_id=msg.id,
        tool_call_id="call_exp", company_id=company.id, user_id=user.id,
    )
    assert claimed is None


@pytest.mark.asyncio
async def test_finalize_tool_call_records_terminal_state(session, company, user, assistant_msg):
    conv, msg = assistant_msg
    await claim_tool_call(
        session, conversation_id=conv.id, message_id=msg.id,
        tool_call_id="call_1", company_id=company.id, user_id=user.id,
    )
    await finalize_tool_call(
        session, message_id=msg.id, tool_call_id="call_1",
        status="completed", result={"ok": True, "status": 201, "data": {"id": "x"}},
    )
    await session.commit()

    await session.refresh(msg)
    record = msg.tools_called[0]
    assert record["status"] == "completed"
    assert record["result_summary"] == {"id": "x", "http_status": 201}
    assert record["name"] == "create_contact"
    # Keep the immutable approved arguments for audit/retry history, but never the
    # arbitrary response body.
    assert record["arguments"] == {"body": {"name": "Acme"}}
    assert "data" not in record
    assert tool_names(msg.tools_called) == ["create_contact"]
    history = build_history_context([msg])
    assert "[completed action: create_contact -> x]" in history[0]["content"]


@pytest.mark.asyncio
async def test_retryable_action_reclaims_same_tool_call(session, company, user, assistant_msg):
    conv, msg = assistant_msg
    await claim_tool_call(
        session, conversation_id=conv.id, message_id=msg.id, tool_call_id="call_1",
        company_id=company.id, user_id=user.id,
    )
    await finalize_tool_call(
        session, message_id=msg.id, tool_call_id="call_1", status="retryable",
        result={"ok": False, "status": 503}, error="temporarily unavailable",
    )
    await session.commit()

    retried = await claim_tool_call(
        session, conversation_id=conv.id, message_id=msg.id, tool_call_id="call_1",
        company_id=company.id, user_id=user.id,
    )
    assert retried is not None
    assert retried["id"] == "call_1"
    assert retried["arguments"] == {"body": {"name": "Acme"}}


@pytest.mark.asyncio
async def test_dismiss_tool_call_persists(session, company, user, assistant_msg):
    conv, msg = assistant_msg
    assert await dismiss_tool_call(
        session, conversation_id=conv.id, message_id=msg.id, tool_call_id="call_1",
        company_id=company.id, user_id=user.id,
    ) is True
    await session.commit()
    await session.refresh(msg)
    assert msg.tools_called[0]["status"] == "dismissed"
    assert pending_actions(msg.tools_called) == []


@pytest.mark.asyncio
async def test_history_appends_proposed_action_lines(session, company, user):
    conv = await create_conversation(session, company.id, user.id)
    await add_message(
        session, conv.id, "assistant", "I can create that contact.",
        tools_called=[_pending_record()],
    )
    await session.commit()

    msgs = await get_messages(session, conv.id)
    history = build_history_context(msgs)
    assert "[proposed action: create_contact]" in history[0]["content"]


@pytest.mark.asyncio
async def test_list_conversations_stable_order_when_updated_at_ties(session, company, user):
    """Regression: conversations sharing an updated_at must order deterministically by id, so
    OFFSET pagination can't skip or duplicate a tied row across pages."""
    from datetime import datetime, timezone
    from sqlalchemy import update

    ids = []
    for i in range(6):
        c = await create_conversation(session, company.id, user.id, title=f"C{i}")
        ids.append(c.id)
    await session.commit()

    await session.execute(
        update(AIConversation).where(AIConversation.id.in_(ids)).values(updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    )
    await session.commit()

    listed = await list_conversations(session, company.id, user.id, limit=100)
    assert [c.id for c in listed] == sorted(ids, reverse=True), \
        "updated_at-tied conversations are not deterministically ordered by id (no tiebreaker)"

    paged = []
    for off in range(0, len(ids), 2):
        page = await list_conversations(session, company.id, user.id, limit=2, offset=off)
        paged += [c.id for c in page]
    assert sorted(paged) == sorted(ids)
    assert len(paged) == len(set(paged))


# ── sidebar history retention ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_normal_conversation_list_preserves_limit_semantics(session, company, user):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        AIConversation(
            id=uuid.UUID(int=i + 1),
            company_id=company.id,
            user_id=user.id,
            title=f"C{i}",
            created_at=stamp + timedelta(seconds=i),
            updated_at=stamp + timedelta(seconds=i),
        )
        for i in range(MAX_CONVERSATIONS_PER_USER + 1)
    ]
    session.add_all(rows)
    await session.commit()

    listed = await list_conversations(
        session, company.id, user.id, limit=MAX_CONVERSATIONS_PER_USER,
    )
    assert len(listed) == MAX_CONVERSATIONS_PER_USER


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "retryable", "executing"])
async def test_history_includes_old_unresolved_action_beyond_limit(
    session, company, user, status,
):
    old = AIConversation(
        company_id=company.id, user_id=user.id, title="old",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    recent = AIConversation(
        company_id=company.id, user_id=user.id, title="recent",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    session.add_all([old, recent])
    await session.flush()
    record = _pending_record("keep")
    if status == "retryable":
        record["status"] = "retryable"
    elif status == "executing":
        record["status"] = "executing"
        record["executing_since"] = datetime.now(timezone.utc).isoformat()
    session.add(AIMessage(
        conversation_id=old.id, role="assistant", content="work",
        tools_called=[record],
    ))
    await session.commit()

    history = await list_conversation_history(session, company.id, user.id, limit=1)
    assert [c.id for c in history] == [recent.id, old.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "running"])
async def test_history_includes_old_active_batch_job_beyond_limit(
    session, company, user, status,
):
    old = AIConversation(
        company_id=company.id, user_id=user.id, title="old",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    recent = AIConversation(
        company_id=company.id, user_id=user.id, title="recent",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    session.add_all([old, recent])
    await session.flush()
    session.add(AIBatchJob(
        conversation_id=old.id,
        company_id=company.id,
        user_id=user.id,
        status=status,
        total_files=1,
        query="read",
        file_ids=["f1"],
    ))
    await session.commit()

    history = await list_conversation_history(session, company.id, user.id, limit=1)
    assert [c.id for c in history] == [recent.id, old.id]


@pytest.mark.asyncio
async def test_history_does_not_restore_old_resolved_conversation(session, company, user):
    old = AIConversation(
        company_id=company.id, user_id=user.id, title="old",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    recent = AIConversation(
        company_id=company.id, user_id=user.id, title="recent",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    session.add_all([old, recent])
    await session.flush()
    session.add(AIMessage(
        conversation_id=old.id, role="assistant", content="done",
        tools_called=[{**_pending_record("done"), "status": "completed"}],
    ))
    await session.commit()

    history = await list_conversation_history(session, company.id, user.id, limit=1)
    assert [c.id for c in history] == [recent.id]


@pytest.mark.asyncio
async def test_history_protected_tail_is_deduplicated_and_sorted(session, company, user):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = [uuid.UUID(int=i) for i in (1, 2, 3)]
    rows = [
        AIConversation(
            id=cid, company_id=company.id, user_id=user.id, title=str(cid.int),
            created_at=stamp, updated_at=stamp,
        )
        for cid in ids
    ]
    session.add_all(rows)
    await session.flush()
    session.add(AIMessage(
        conversation_id=ids[0], role="assistant", content="pending",
        tools_called=[_pending_record("old")],
    ))
    await session.commit()

    history = await list_conversation_history(session, company.id, user.id, limit=2)
    assert [c.id for c in history] == [ids[2], ids[1], ids[0]]
    assert len({c.id for c in history}) == len(history)


@pytest.mark.asyncio
async def test_protected_history_is_user_and_company_scoped(
    session, company, user, user_b, company_b, user_b_co,
):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recent = AIConversation(
        company_id=company.id, user_id=user.id, title="recent",
        created_at=stamp + timedelta(days=2), updated_at=stamp + timedelta(days=2),
    )
    own_old = AIConversation(
        company_id=company.id, user_id=user.id, title="own",
        created_at=stamp, updated_at=stamp,
    )
    other_user = AIConversation(
        company_id=company.id, user_id=user_b.id, title="other-user",
        created_at=stamp, updated_at=stamp,
    )
    other_company = AIConversation(
        company_id=company_b.id, user_id=user_b_co.id, title="other-company",
        created_at=stamp, updated_at=stamp,
    )
    session.add_all([recent, own_old, other_user, other_company])
    await session.flush()
    for target in (own_old, other_user, other_company):
        session.add(AIMessage(
            conversation_id=target.id, role="assistant", content="pending",
            tools_called=[_pending_record(str(target.id))],
        ))
    await session.commit()

    history = await list_conversation_history(session, company.id, user.id, limit=1)
    assert {c.id for c in history} == {recent.id, own_old.id}


@pytest.mark.asyncio
async def test_pruning_and_history_share_same_protection_rule(session, company, user):
    protected = await create_conversation(session, company.id, user.id, title="executing")
    record = {
        **_pending_record("running"),
        "status": "executing",
        "executing_since": datetime.now(timezone.utc).isoformat(),
    }
    await add_message(
        session, protected.id, "assistant", "Applying.",
        tools_called=[record],
    )
    await session.commit()
    protected_id = protected.id

    for i in range(MAX_CONVERSATIONS_PER_USER):
        await create_conversation(session, company.id, user.id, title=f"new {i}")
    await session.commit()

    assert await get_conversation(session, protected_id, company.id, user.id) is not None
    history = await list_conversation_history(
        session, company.id, user.id, limit=MAX_CONVERSATIONS_PER_USER,
    )
    assert protected_id in {c.id for c in history}
