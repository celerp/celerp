# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A seeded property check of the two claims that decide an effect exactly once, on real
PostgreSQL connections: the bank key claim (``bank_statements._claimed``) and the AI action
claim (``conversations.claim_tool_call`` through ``change_tools_called``).

Each seed runs several same-key requests at once in a random order, some with another
request body or another operation, some stopped between the claim and its commit (rolled
back) or between the commit and the answer (the answer lost), then a retry. Reference model:
the first committed claim fixes (operation, request, outcome) for the key; there is exactly
one effect row; every answer equals the stored outcome, or is a key_reused refusal for a
request the key is not bound to, or claims nothing."""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.ai import conversations
from celerp.events.engine import emit_event
from celerp.models.ai import AIConversation, AIMessage
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry

pytestmark = pytest.mark.asyncio

SEEDS = range(6)
# How each run ends: answered, stopped before its commit, or stopped after it (answer lost).
ENDINGS = ("answer", "crash_before_commit", "crash_after_commit")


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _owner(factory) -> tuple[uuid.UUID, uuid.UUID]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id, email=f"u-{user_id.hex[:8]}@example.test", name="Owner", auth_hash="x",
                   is_active=True))
        await s.flush()
        s.add(Company(id=company_id, name="OracleCo", slug=f"oracle-{company_id.hex[:8]}", settings={}))
        await s.commit()
    return company_id, user_id


def _plan(rng: random.Random, variants: int) -> list[tuple[int, str, float]]:
    """Each run: which request variant it sends (0 is the one retried), how it ends, and
    how long it waits before starting. Every plan holds at least one other variant."""
    runs = [(rng.randrange(variants) if rng.random() < 0.4 else 0, rng.choice(ENDINGS), rng.random() / 50)
            for _ in range(rng.randint(1, 4))]
    return [*runs, (rng.randrange(1, variants), rng.choice(ENDINGS), rng.random() / 50)]


async def _bank_run(factory, company_id, user_id, key, variant, ending, delay, effect_id):
    from celerp_accounting.bank_statements import _claimed

    operation, request = ("ask", {"note": "a"}) if variant != 2 else ("resolve", {"note": "a"})
    if variant == 1:
        request = {"note": "b"}
    await asyncio.sleep(delay)
    async with factory() as s:
        try:
            answer, first = await _claimed(s, company_id, user_id, key, request, operation, None,
                                           {"run": str(uuid.uuid4())})
        except HTTPException as exc:
            await s.rollback()
            return variant, "refused", exc.detail["message_key"]
        if first:
            await emit_event(s, company_id=company_id, entity_id=effect_id, entity_type="bank_operation",
                             event_type="acc.bank_statement.done", data={"operation": "effect"},
                             actor_id=user_id, location_id=None, source="reconciliation",
                             idempotency_key=str(uuid.uuid4()))
        if ending == "crash_before_commit":
            await s.rollback()
            return variant, "crashed", None
        await s.commit()
        return variant, ("lost" if ending == "crash_after_commit" else "answered"), answer


async def _rows(factory, company_id, entity_id) -> list[LedgerEntry]:
    async with factory() as s:
        return list((await s.execute(select(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id))).scalars())


@pytest.mark.parametrize("seed", SEEDS)
async def test_a_bank_key_decides_one_effect_whatever_the_interleaving(committed_engine, seed):
    from celerp_accounting.bank_statements import _bound
    from celerp.events.engine import request_fingerprint

    rng = random.Random(seed)
    factory = _factory(committed_engine)
    company_id, user_id = await _owner(factory)
    key, effect_id = f"bank:{uuid.uuid4()}", f"oracle:{uuid.uuid4()}"
    claim_id = f"bankop:{uuid.uuid5(uuid.NAMESPACE_URL, f'{company_id}:{key}')}"
    plan = _plan(rng, 3)
    runs = await asyncio.gather(*(
        _bank_run(factory, company_id, user_id, key, v, e, d, effect_id) for v, e, d in plan))
    # The retry of variant 0, answered: the key now holds whichever request committed first.
    retry = await _bank_run(factory, company_id, user_id, key, 0, "answer", 0, effect_id)

    claims = await _rows(factory, company_id, claim_id)
    assert len(claims) == 1, (seed, plan, runs)
    stored = claims[0].metadata_
    variants = {0: ("ask", {"note": "a"}), 1: ("ask", {"note": "b"}), 2: ("resolve", {"note": "a"})}
    owner = next(v for v, (op, req) in variants.items()
                 if request_fingerprint(_bound(req, op)) == stored["request"])
    assert len(await _rows(factory, company_id, effect_id)) == 1, (seed, plan, runs)
    for variant, state, answer in [*runs, retry]:
        if state == "crashed":
            continue
        if variant == owner:
            assert state != "refused" and answer == stored["result"], (seed, plan, runs, retry)
        else:
            assert (state, answer) == ("refused", "request.key_reused"), (seed, plan, runs, retry)


def _action(tool_call_id: str) -> dict:
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return {"id": tool_call_id, "name": "items.update", "status": "pending", "keyed": True, "expires_at": expires}


async def _ai_run(factory, ids, tool_call_id, variant, ending, delay):
    company_id, user_id, conversation_id, message_id = ids
    await asyncio.sleep(delay)
    async with factory() as s:
        claim = conversations.claim_tool_call if variant != 2 else conversations.claim_continuation
        kw = {"tool_call_id": tool_call_id, "run_again": variant == 1} if variant != 2 else {"job_id": uuid.uuid4()}
        answer = await claim(s, conversation_id=conversation_id, message_id=message_id,
                             company_id=company_id, user_id=user_id, **kw)
        if ending == "crash_before_commit":
            await s.rollback()
            return variant, "crashed", None
        await s.commit()
        return variant, ("lost" if ending == "crash_after_commit" else "answered"), answer


@pytest.mark.parametrize("seed", SEEDS)
async def test_an_ai_action_is_claimed_once_whatever_the_interleaving(committed_engine, seed):
    rng = random.Random(seed)
    factory = _factory(committed_engine)
    company_id, user_id = await _owner(factory)
    conversation_id, message_id, tool_call_id = uuid.uuid4(), uuid.uuid4(), f"call-{seed}"
    async with factory() as s:
        s.add(AIConversation(id=conversation_id, company_id=company_id, user_id=user_id))
        await s.flush()
        s.add(AIMessage(id=message_id, conversation_id=conversation_id, role="assistant", content="",
                        tools_called=[_action(tool_call_id)]))
        await s.commit()
    ids = (company_id, user_id, conversation_id, message_id)
    plan = _plan(rng, 3)
    runs = await asyncio.gather(*(_ai_run(factory, ids, tool_call_id, v, e, d) for v, e, d in plan))
    retry = await _ai_run(factory, ids, tool_call_id, 0, "answer", 0)

    async with factory() as s:
        records = (await s.get(AIMessage, message_id)).tools_called
    record = next(r for r in records if r.get("id") == tool_call_id)
    claimed = [r for r in [*runs, retry] if r[1] != "crashed" and r[0] == 0 and r[2] is not None]
    # Exactly one committed claim moved the action to executing; nothing else claims it.
    assert record["status"] == "executing" and len(claimed) == 1, (seed, plan, runs, retry)
    assert claimed[0][2] == record, (seed, plan, runs, retry)
    assert all(r[2] is None for r in [*runs, retry] if r[0] != 0), (seed, plan, runs, retry)
    assert not any(isinstance(r, dict) and r.get("continued_job_id") for r in records)
