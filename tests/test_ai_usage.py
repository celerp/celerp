# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Per-user AI question usage (GET /ai/usage-stats): a question counts once, when the
relay charged it; the newest asker comes first; the period is this month or all time.

Questions are stored as the app stores them: a user ``AIMessage`` in the asker's
``AIConversation`` with the charged credits in ``credits_used``, and a receipts batch as an
``AIBatchJob`` carrying its credits.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

from celerp.ai import service
from celerp.ai.conversations import record_credits
from celerp.ai.llm import ModelResult, RelayError
from celerp.ai.service import run_agent
from celerp.models.ai import AIBatchJob, AIConversation, AIMessage
from celerp.services.auth import decode_access_token
from celerp.services.business_time import business_today
from test_helpers import invite_user, register_admin


def _claims(token: str) -> tuple[uuid.UUID, uuid.UUID]:
    claims = decode_access_token(token)
    return uuid.UUID(claims["company_id"]), uuid.UUID(claims["sub"])


async def _question(session, company_id, user_id, *, credits: int, when: datetime | None = None) -> AIMessage:
    conversation = AIConversation(company_id=company_id, user_id=user_id)
    session.add(conversation)
    await session.flush()
    message = AIMessage(conversation_id=conversation.id, role="user", content="q",
                        credits_used=credits, created_at=when or datetime.now(timezone.utc))
    session.add(message)
    await session.flush()
    return message


async def _usage(client, token: str, **params):
    return await client.get("/ai/usage-stats", headers={"Authorization": f"Bearer {token}"}, params=params)


@pytest.fixture
async def owner(client):
    token = await register_admin(client)
    company_id, user_id = _claims(token)
    return token, company_id, user_id


async def test_usage_counts_only_charged_questions(client, session, owner):
    token, company_id, user_id = owner
    await _question(session, company_id, user_id, credits=0)  # refused by the relay
    await _question(session, company_id, user_id, credits=1)
    await session.commit()
    rows = (await _usage(client, token)).json()["users"]
    assert [(r["query_count"], r["credits_used"]) for r in rows] == [(1, 1)]

    # A receipts batch stores its credits on the job; its own message carries none.
    job_message = await _question(session, company_id, user_id, credits=0)
    session.add(AIBatchJob(company_id=company_id, user_id=user_id, conversation_id=job_message.conversation_id,
                           total_files=1, query="read these receipts", file_ids=["f"], credits_consumed=3))
    await session.commit()
    rows = (await _usage(client, token)).json()["users"]
    assert [(r["query_count"], r["credits_used"]) for r in rows] == [(2, 4)]


async def test_credits_kept_when_a_later_turn_fails(client, session, owner, monkeypatch):
    token, company_id, user_id = owner
    turns = [ModelResult(
        message={"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}}]},
        model_used="test-model", usage={"credits": 1}, reservation_id="r", remaining=10,
    )]

    async def _complete(messages, *, tools=None, reservation_id=None, **_kw):
        if turns:
            return turns.pop(0)
        raise RelayError("service_unavailable", "relay down")

    monkeypatch.setattr(service, "complete", _complete)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: {})
    result = await run_agent(
        app=object(), authorization="Bearer tok", query="how do I add a supplier?",
        company_id=company_id, company_settings={}, user_id=user_id, role="owner",
        memory={"notes": [], "kv": {}}, file_ids=None, history=[],
    )
    assert result.error and result.credits == 1

    message = await _question(session, company_id, user_id, credits=0)
    await record_credits(session, message.id, result.credits)
    await session.commit()
    rows = (await _usage(client, token)).json()["users"]
    assert [(r["query_count"], r["credits_used"]) for r in rows] == [(1, 1)]


async def test_credits_kept_when_the_relay_refuses_a_later_turn(client, session, owner, monkeypatch):
    """The relay charges a free question's first turn, then refuses the continuation
    (413 free_text_too_large once tool results have grown the history). The refusal still
    reaches the user, and the charged question stays in the usage table."""
    from fastapi import HTTPException
    from celerp.gateway.state import set_session_token

    token, company_id, user_id = owner
    set_session_token("s" * 64)
    turns = [ModelResult(
        message={"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}}]},
        model_used="test-model", usage={"credits": 1}, reservation_id="r", remaining=10,
    )]

    async def _complete(messages, *, tools=None, reservation_id=None, **_kw):
        if turns:
            return turns.pop(0)
        raise HTTPException(status_code=413, detail={"code": "free_text_too_large", "message": "too long"})

    monkeypatch.setattr(service, "complete", _complete)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: {})
    headers = {"Authorization": f"Bearer {token}"}
    conv = (await client.post("/ai/conversations", headers=headers, json={"title": None})).json()["id"]
    r = await client.post(f"/ai/conversations/{conv}/query", headers=headers, json={"query": "supplier?"})
    assert r.status_code == 413, r.text
    assert r.json()["detail"]["code"] == "free_text_too_large"
    rows = (await _usage(client, token)).json()["users"]
    assert [(r["query_count"], r["credits_used"]) for r in rows] == [(1, 1)]


async def test_usage_newest_first(client, session, owner):
    token, company_id, user_id = owner
    other_token = await invite_user(client, session, {"Authorization": f"Bearer {token}"}, "staff@perm.example", "manager")
    _, other_id = _claims(other_token)
    now = datetime.now(timezone.utc)
    for minutes in (30, 20):
        await _question(session, company_id, user_id, credits=1, when=now - timedelta(minutes=minutes))
    await _question(session, company_id, other_id, credits=1, when=now - timedelta(minutes=5))
    await session.commit()
    rows = (await _usage(client, token)).json()["users"]
    assert [(r["user_id"], r["query_count"]) for r in rows] == [(str(other_id), 1), (str(user_id), 2)]


async def test_usage_period_all_counts_earlier_months(client, session, owner):
    token, company_id, user_id = owner
    await _question(session, company_id, user_id, credits=1, when=datetime.now(timezone.utc) - timedelta(days=70))
    await _question(session, company_id, user_id, credits=1)
    await session.commit()

    month = (await _usage(client, token)).json()
    assert [r["query_count"] for r in month["users"]] == [1]
    assert month["period"]["kind"] == "month"
    tz = (await client.get("/companies/me", headers={"Authorization": f"Bearer {token}"})).json()["settings"].get("timezone")
    assert month["period"]["start"] == business_today(tz).replace(day=1).isoformat()

    every = (await _usage(client, token, period="all")).json()
    assert [r["query_count"] for r in every["users"]] == [2]
    assert every["period"] == {"kind": "all", "start": None}


async def test_usage_period_rejects_unknown_value(client, owner):
    token, _, _ = owner
    r = await _usage(client, token, period="year")
    assert r.status_code == 422, r.text


async def test_usage_refused_without_manage_users(client, session, owner):
    token, _, _ = owner
    viewer = await invite_user(client, session, {"Authorization": f"Bearer {token}"}, "viewer@perm.example", "viewer")
    r = await _usage(client, viewer)
    assert r.status_code == 403, r.text
