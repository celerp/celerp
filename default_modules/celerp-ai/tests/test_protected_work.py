# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Protected work in a conversation: one rule at every lifecycle boundary.

An action awaiting the user (pending, retryable, outcome unknown) or being run
(executing) is protected work, as is the question an open job answers. Pruning past
the message cap keeps it, the conversation read and its older-actions pages reach it
however old it is, and
deleting the conversation is refused while a confirmed action is running.
"""
from __future__ import annotations

import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from sqlalchemy import select

from celerp.ai import conversations as ai_conversations
from celerp.ai import tools as ai_tools
from celerp.ai.conversations import (
    EXECUTING_STALE_S,
    MAX_MESSAGES_PER_CONVERSATION,
    ConversationBusy,
    add_message,
    claim_tool_call,
    create_conversation,
    delete_conversation,
    finalize_tool_call,
    get_messages,
)
from celerp.models.ai import AIMessage
from celerp.models.company import Company, User
from test_action_outcomes import (
    _confirm,
    _fund_count,
    _location_count,
    _lost_after_sending,
    _propose,
    _propose_location,
    _record,
    _call,
)
from test_helpers import perm_setup
from test_workflows import ScriptedModel, _op, agent_caps, ai_session  # noqa: F401


async def _flood(session, conv_id, n: int = MAX_MESSAGES_PER_CONVERSATION) -> None:
    """``n`` newer plain messages, pushing everything before them past the cap."""
    for i in range(n):
        await add_message(session, uuid.UUID(str(conv_id)), "user", f"later {i}")
    await session.commit()


def _card(conv: dict, message_id: str) -> list[dict]:
    """The open action cards the conversation read returns on ``message_id``."""
    return [a for m in conv["messages"] if m["id"] == message_id for a in (m.get("pending_actions") or [])]


# ── the actual conversation API ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.usefixtures("tools_open_when_called", "model_answers_unscripted_runs")
async def test_old_pending_action_is_read_and_confirmed_through_the_api(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session, currency="USD")
    admin = ctx["admin_h"]
    before = await _location_count(client, admin)
    conv_id, message_id, call_id = await _propose_location(client, admin, agent_caps, "Old room")
    await _flood(session, conv_id)

    conv = (await client.get(f"/ai/conversations/{conv_id}", headers=admin)).json()
    assert [a["id"] for a in _card(conv, message_id)] == [call_id]
    times = [m["created_at"] for m in conv["messages"]]
    assert times == sorted(times)

    r = await _confirm(client, admin, conv_id, message_id, call_id)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True, r.text
    assert await _location_count(client, admin) == before + 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("tools_open_when_called", "model_answers_unscripted_runs")
async def test_old_pending_action_is_dismissed_through_the_api(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session, currency="USD")
    admin = ctx["admin_h"]
    conv_id, message_id, call_id = await _propose_location(client, admin, agent_caps, "Spare room")
    await _flood(session, conv_id)

    conv = (await client.get(f"/ai/conversations/{conv_id}", headers=admin)).json()
    assert [a["id"] for a in _card(conv, message_id)] == [call_id]
    r = await client.post(f"/ai/conversations/{conv_id}/dismiss", headers=admin,
                          json={"message_id": message_id, "tool_call_id": call_id})
    assert r.status_code == 200, r.text
    assert (await _record(session, message_id, call_id))["status"] == "dismissed"


@pytest.mark.asyncio
@pytest.mark.usefixtures("tools_open_when_called", "model_answers_unscripted_runs")
async def test_old_retryable_action_is_read_and_retried_through_the_api(client, session, agent_caps, ai_session):
    ctx = await perm_setup(client, session, currency="USD")
    admin = ctx["admin_h"]
    on = await client.put("/accounting/funds/setting", headers=admin, json={"enabled": True})
    assert on.status_code == 200, on.text
    before = await _fund_count(client, admin)
    op = _op(agent_caps, "POST", "/accounting/funds")
    conv_id, message_id, call_id = await _propose(client, admin, ScriptedModel(
        _call(op, {"body": {"name": "Roof fund"}}, "fund1")))
    with patch.object(ai_tools, "execute_agent_capability", _lost_after_sending([])):
        first = await _confirm(client, admin, conv_id, message_id, call_id)
    assert first.json()["action_status"] == "retryable", first.text
    await _flood(session, conv_id)

    conv = (await client.get(f"/ai/conversations/{conv_id}", headers=admin)).json()
    assert [a["status"] for a in _card(conv, message_id)] == ["retryable"]
    again = await _confirm(client, admin, conv_id, message_id, call_id)
    assert again.status_code == 200, again.text
    assert again.json()["ok"] is True, again.text
    assert await _fund_count(client, admin) == before + 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("tools_open_when_called", "model_answers_unscripted_runs")
async def test_running_action_blocks_deletion_and_stays_auditable(client, session, agent_caps, ai_session):
    """A confirmed action paused between its claim and its result: the conversation
    cannot be deleted under it, and the action finalizes on its own record."""
    ctx = await perm_setup(client, session, currency="USD")
    admin = ctx["admin_h"]
    conv_id, message_id, call_id = await _propose_location(client, admin, agent_caps, "Busy room")
    msg = (await session.execute(select(AIMessage).where(AIMessage.id == uuid.UUID(message_id)))).scalar_one()
    conv = await session.get(ai_conversations.AIConversation, msg.conversation_id)
    msg_id = msg.id
    claimed = await claim_tool_call(session, conversation_id=conv.id, message_id=msg_id, tool_call_id=call_id,
                                    company_id=conv.company_id, user_id=conv.user_id)
    assert claimed is not None and claimed["status"] == "executing"
    await session.commit()

    r = await client.delete(f"/ai/conversations/{conv_id}", headers=admin)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "conversation_busy", r.text

    await finalize_tool_call(session, message_id=msg_id, tool_call_id=call_id, status="completed",
                             result={"ok": True, "data": {"id": "loc"}})
    await session.commit()
    assert (await _record(session, message_id, call_id))["status"] == "completed"
    r = await client.delete(f"/ai/conversations/{conv_id}", headers=admin)
    assert r.status_code == 204, r.text


# ── pruning keeps a running action ──────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.usefixtures("tools_open_when_called", "model_answers_unscripted_runs")
async def test_more_older_actions_than_a_page_are_each_reached_by_paging_and_acted_on(
        client, session, agent_caps, ai_session, monkeypatch):
    """Far more older open actions than a page: the conversation read and the older-actions
    pages, followed by their cursors, return each exactly once in time order, every response
    bounded, and each is then confirmed, retried or dismissed as its status allows."""
    cap = 4
    monkeypatch.setattr(ai_conversations, "MAX_MESSAGES_PER_CONVERSATION", cap)
    ctx = await perm_setup(client, session, currency="USD")
    admin = ctx["admin_h"]
    before = await _location_count(client, admin)
    conv_id, message_id, call_id = await _propose_location(client, admin, agent_caps, "Room 0")
    seed = await _record(session, message_id, call_id)
    cards = {message_id: (call_id, "pending")}
    for i in range(1, 3 * cap + 6):
        status = ("pending", "retryable", "unknown")[i % 3]
        record = {**seed, "id": f"loc{i}", "arguments": {"body": {"name": f"Room {i}", "type": "warehouse"}},
                  **({} if status == "pending" else {"status": status})}
        msg = await add_message(session, uuid.UUID(conv_id), "assistant", f"proposal {i}", tools_called=[record])
        await add_message(session, uuid.UUID(conv_id), "user", f"chat {i}")
        cards[str(msg.id)] = (f"loc{i}", status)
    await session.commit()
    await _flood(session, conv_id, cap)

    conv = (await client.get(f"/ai/conversations/{conv_id}", headers=admin)).json()
    assert len(conv["messages"]) <= 2 * cap
    seen, pages = [m["id"] for m in conv["messages"]], 0
    older = conv["older_actions"]
    while older is not None:
        r = await client.get(f"/ai/conversations/{conv_id}/older-actions", headers=admin, params=older)
        assert r.status_code == 200, r.text
        page = r.json()
        assert 0 < len(page["messages"]) <= cap and pages < len(cards)
        times = [datetime.fromisoformat(m["created_at"]) for m in page["messages"]]
        assert times == sorted(times) and times[-1] <= datetime.fromisoformat(older["before_at"])
        seen = [m["id"] for m in page["messages"]] + seen
        older, pages = page["older_actions"], pages + 1
    assert pages >= 3
    assert len(seen) == len(set(seen)), "a message came back twice"
    assert set(cards) <= set(seen), "an open action was never returned"

    for mid, (cid, status) in cards.items():
        if status == "unknown" or (status == "pending" and int(cid.removeprefix("loc") or 0) % 2):
            r = await client.post(f"/ai/conversations/{conv_id}/dismiss", headers=admin,
                                  json={"message_id": mid, "tool_call_id": cid})
            assert r.status_code == 200, (status, r.text)
            assert (await _record(session, mid, cid))["status"] == "dismissed"
        else:
            r = await _confirm(client, admin, conv_id, mid, cid)
            assert r.status_code == 200 and r.json()["ok"] is True, (status, r.text)
    confirmed = sum(1 for cid, status in cards.values()
                    if status == "retryable" or (status == "pending" and not int(cid.removeprefix("loc") or 0) % 2))
    assert await _location_count(client, admin) == before + confirmed


async def _owner(session) -> tuple[Company, User]:
    company = Company(name="Protected Co", slug=f"protected-{uuid.uuid4().hex[:8]}", settings={})
    user = User(email=f"p-{uuid.uuid4().hex[:8]}@test.com", name="P")
    session.add(company)
    await session.commit()
    session.add(user)
    await session.commit()
    return company, user


def _record_of(kind: str, call_id: str) -> dict | str:
    now = datetime.now(timezone.utc)
    base = {"id": call_id, "name": "create_contact", "arguments": {"body": {"name": "Acme"}},
            "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=15)).isoformat()}
    fresh = now.isoformat()
    stale = (now - timedelta(seconds=EXECUTING_STALE_S + 60)).isoformat()
    return {
        "read": "list_items",
        "pending": base,
        "expired": {**base, "expires_at": (now - timedelta(minutes=1)).isoformat()},
        "retryable": {**base, "status": "retryable"},
        "unknown": {**base, "status": "unknown"},
        "executing": {**base, "status": "executing", "executing_since": fresh},
        "stale_keyed": {**base, "status": "executing", "executing_since": stale, "keyed": True},
        "stale_unkeyed": {**base, "status": "executing", "executing_since": stale},
        "completed": {**base, "status": "completed"},
        "failed": {**base, "status": "failed"},
        "dismissed": {**base, "status": "dismissed"},
    }[kind]


KINDS = ["read", "pending", "expired", "retryable", "unknown", "executing", "stale_keyed",
         "stale_unkeyed", "completed", "failed", "dismissed"]
# The reference, written apart from the code under test: work the user still decides on
# or that is being run.
PROTECTED = {"pending", "retryable", "unknown", "executing", "stale_keyed", "stale_unkeyed"}
RUNNING = {"executing"}


@pytest.mark.asyncio
async def test_pruning_keeps_a_running_action_that_then_finalizes(session):
    company, user = await _owner(session)
    conv = await create_conversation(session, company.id, user.id)
    proposal = await add_message(session, conv.id, "assistant", "Applying.",
                                 tools_called=[_record_of("pending", "run1")])
    await session.commit()
    proposal_id = proposal.id
    assert await claim_tool_call(session, conversation_id=conv.id, message_id=proposal_id, tool_call_id="run1",
                                 company_id=company.id, user_id=user.id) is not None
    await session.commit()
    await _flood(session, conv.id)

    await finalize_tool_call(session, message_id=proposal_id, tool_call_id="run1", status="completed",
                             result={"ok": True, "data": {"id": "c1"}})
    await session.commit()
    session.expire_all()
    kept = await session.get(AIMessage, proposal_id)
    assert kept is not None
    assert kept.tools_called[0]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(12))
async def test_retention_retrieval_and_deletion_follow_one_rule(session, seed, monkeypatch):
    """Seeded mixes of action states: what pruning keeps, what the read returns beyond the
    newest window and whether deletion is refused all agree with the reference rule."""
    rng = random.Random(seed)
    cap = 6
    monkeypatch.setattr(ai_conversations, "MAX_MESSAGES_PER_CONVERSATION", cap)
    company, user = await _owner(session)
    company_id, user_id = company.id, user.id
    conv = await create_conversation(session, company_id, user_id)
    conv_id = conv.id
    old: list[tuple[uuid.UUID, set[str]]] = []
    for i in range(rng.randint(3, 10)):
        kinds = rng.sample(KINDS, rng.randint(0, 3))
        msg = await add_message(session, conv_id, "assistant", f"old {i}",
                                tools_called=[_record_of(k, f"c{i}-{j}") for j, k in enumerate(kinds)] or None)
        old.append((msg.id, set(kinds)))
    await session.commit()
    await _flood(session, conv_id, cap)
    session.expire_all()

    expected_kept = {mid for mid, kinds in old if kinds & PROTECTED}
    surviving = set((await session.execute(
        select(AIMessage.id).where(AIMessage.id.in_([mid for mid, _ in old])))).scalars().all())
    assert surviving == expected_kept

    read, _ = await get_messages(session, conv_id, limit=cap)
    newest = list((await session.execute(
        select(AIMessage.id).where(AIMessage.conversation_id == conv_id)
        .order_by(AIMessage.created_at.desc(), AIMessage.id.desc()).limit(cap))).scalars().all())
    assert {m.id for m in read} == set(newest) | expected_kept
    assert [m.id for m in read] == [m.id for m in sorted(read, key=lambda m: (m.created_at, m.id))]

    # Protected work inside the newest window is read once there, never again as older work.
    inside = await add_message(session, conv_id, "assistant", "inside", tools_called=[_record_of("pending", "inside")])
    await add_message(session, conv_id, "user", "after")
    await session.commit()
    read, _ = await get_messages(session, conv_id, limit=cap)
    ids = [m.id for m in read]
    assert inside.id in ids and len(ids) == len(set(ids)), ids

    running = any(kinds & RUNNING for _, kinds in old)
    if running:
        with pytest.raises(ConversationBusy):
            await delete_conversation(session, conv_id, company_id, user_id)
        await session.rollback()
        assert len((await get_messages(session, conv_id, limit=cap))[0]) == len(read)
    else:
        assert await delete_conversation(session, conv_id, company_id, user_id) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(4))
async def test_an_older_actions_page_reads_a_bounded_slice_of_a_long_history(session, monkeypatch, seed):
    """DB work per page is bounded, never the whole history: paging a long history of
    protected and plain messages (ties on one timestamp included) from the newest, each
    request reads at most the rows down to the message after its page plus one batch, and
    every protected message is reached exactly once, each page in time order."""
    rng = random.Random(seed)
    company, user = await _owner(session)
    conv = await create_conversation(session, company.id, user.id)
    start, limit = datetime(2026, 1, 1, tzinfo=timezone.utc), rng.randint(2, 9)
    protected_kinds, plain_kinds = sorted(PROTECTED), sorted(set(KINDS) - PROTECTED)
    for i in range(400):
        kind = rng.choice(protected_kinds if rng.random() < 0.5 else plain_kinds)
        session.add(AIMessage(conversation_id=conv.id, role="assistant", content=kind,
                              created_at=start + timedelta(seconds=i // 3), tools_called=[_record_of(kind, f"c{i}")]))
    await session.commit()
    rows = (await session.execute(select(AIMessage.id, AIMessage.created_at, AIMessage.content)
                                  .where(AIMessage.conversation_id == conv.id))).all()
    newest_first = sorted(rows, key=lambda r: (r.created_at, r.id), reverse=True)

    scanned: list[int] = []
    real = ai_conversations._protected_message_ids

    async def _counting(session_, conversation_id, read):
        scanned.append(len(read))
        return await real(session_, conversation_id, read)
    monkeypatch.setattr(ai_conversations, "_protected_message_ids", _counting)

    cursor, seen = (start + timedelta(days=1), uuid.UUID(int=2 ** 128 - 1)), []
    while cursor is not None:
        older = [r for r in newest_first if (r.created_at, r.id) < cursor]
        wanted = [r.id for r in older if r.content in PROTECTED]
        after = wanted[limit] if len(wanted) > limit else None
        need = next(i for i, r in enumerate(older) if r.id == after) + 1 if after else len(older)
        scanned.clear()
        page, cursor = await ai_conversations.protected_before(session, conv.id, cursor, limit=limit)
        assert sum(scanned) <= need + limit + 1, (sum(scanned), need, limit)
        assert [m.id for m in page] == list(reversed(wanted[:limit]))
        assert (cursor is None) == (after is None)
        seen += [m.id for m in page]
    assert sorted(seen) == sorted(r.id for r in rows if r.content in PROTECTED)


@pytest.mark.asyncio
async def test_an_older_actions_request_examines_at_most_the_scan_budget_however_long_the_finished_history(
        session, monkeypatch):
    """A request's work is bounded by PROTECTED_SCAN_BUDGET rows, not by how many finished
    (formerly protected) actions sit between two cards still protected: when the budget runs
    out before the page fills, the request returns what it found and a cursor at the last row
    it examined, and following the cursor reaches every protected message once, in time order."""
    budget, limit = 30, 4
    monkeypatch.setattr(ai_conversations, "PROTECTED_SCAN_BUDGET", budget)
    company, user = await _owner(session)
    conv = await create_conversation(session, company.id, user.id)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    finished_kinds = sorted(set(KINDS) - PROTECTED)
    kinds = [sorted(PROTECTED)[i % len(PROTECTED)] for i in range(3)] + [
        finished_kinds[i % len(finished_kinds)] for i in range(300)] + [sorted(PROTECTED)[0]]
    for i, kind in enumerate(kinds):
        session.add(AIMessage(conversation_id=conv.id, role="assistant", content=kind,
                              created_at=start + timedelta(seconds=i), tools_called=[_record_of(kind, f"c{i}")]))
    await session.commit()
    rows = (await session.execute(select(AIMessage.id, AIMessage.created_at, AIMessage.content)
                                  .where(AIMessage.conversation_id == conv.id))).all()
    wanted = sorted((r for r in rows if r.content in PROTECTED), key=lambda r: (r.created_at, r.id))

    scanned: list[int] = []
    real = ai_conversations._protected_message_ids

    async def _counting(session_, conversation_id, read):
        scanned.append(len(read))
        return await real(session_, conversation_id, read)
    monkeypatch.setattr(ai_conversations, "_protected_message_ids", _counting)

    cursor, seen, requests = (start + timedelta(days=1), uuid.UUID(int=2 ** 128 - 1)), [], 0
    while cursor is not None:
        scanned.clear()
        page, cursor = await ai_conversations.protected_before(session, conv.id, cursor, limit=limit)
        requests += 1
        assert sum(scanned) <= budget, sum(scanned)
        assert [m.id for m in page] == [m.id for m in sorted(page, key=lambda m: (m.created_at, m.id))]
        seen = [m.id for m in page] + seen
        assert requests <= len(rows) // budget + 2
    assert seen == [r.id for r in wanted]
