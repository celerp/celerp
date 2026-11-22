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


async def _owner(factory, settings: dict | None = None) -> tuple[uuid.UUID, uuid.UUID]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id, email=f"u-{user_id.hex[:8]}@example.test", name="Owner", auth_hash="x",
                   is_active=True))
        await s.flush()
        s.add(Company(id=company_id, name="OracleCo", slug=f"oracle-{company_id.hex[:8]}",
                          settings=settings or {}))
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


# How the transport takes one Ask email: delivered and answered, refused, delivered with the
# answer lost (unconfirmed), or delivered and the process killed before the outcome is kept.
TRANSPORT = ("accept", "reject", "lost", "killed")


class _Killed(BaseException):
    """The process stopping after the transport took the email, before anything is settled."""


async def _ask_members(factory, company_id, count: int) -> dict[str, dict]:
    from celerp.models.accounting import UserCompany

    members = {}
    async with factory() as s:
        for i in range(count):
            member = uuid.uuid4()
            members[str(member)] = {"name": f"Member {i}", "email": f"m{i}-{member.hex[:8]}@example.test"}
            s.add(User(id=member, auth_hash="x", is_active=True, **members[str(member)]))
        await s.flush()
        for member in members:
            s.add(UserCompany(user_id=uuid.UUID(member), company_id=company_id, role="viewer", is_active=True))
        await s.commit()
    return members


async def _change_member(factory, company_id, member: str, how: str) -> str | None:
    """Revoke a member (membership ended, a company write) or move their address (the user's
    own row); returns the new address."""
    from celerp.models.accounting import UserCompany
    from celerp.services.company_lock import lock_company

    async with factory() as s:
        if how == "revoke":
            await lock_company(s, company_id)
            row = (await s.execute(select(UserCompany).where(
                UserCompany.company_id == company_id, UserCompany.user_id == uuid.UUID(member)))).scalar_one()
            row.is_active = False
            address = None
        else:
            user = await s.get(User, uuid.UUID(member))
            user.email = address = f"moved-{uuid.uuid4().hex[:8]}@example.test"
        await s.commit()
    return address


@pytest.mark.parametrize("seed", range(12))
async def test_an_ask_emails_only_members_still_answering_whatever_the_interleaving(committed_engine, seed,
                                                                                      monkeypatch):
    """Ask emails under one claimed key, in rounds of 1-3 runs at once, each email taken by the
    transport at random (accept, reject, answer lost, process killed after acceptance); between
    rounds a member is revoked or moves address at random, and at random one member or everyone
    is revoked after the first attempts are recorded, just before they go. Reference model: every
    answer reads as sent only when it names a recipient and each one it names was sent to; a send in a round
    goes only to a member still answering at that round, at the address they have then; over
    SMTP no member is ever delivered twice; a final run answers each delivered member as sent
    (or unconfirmed when its answer never came back), answers unconfirmed and never resends an
    attempt its killed run left pending, emails every other member still answering, and names
    each revoked member who was never sent anything."""
    from celerp.services import email as email_service
    from celerp_accounting.bank_statements import _claimed
    from celerp_accounting.statement_lines import email_ask
    from ui.i18n import t

    rng = random.Random(seed)
    factory = _factory(committed_engine)
    company_id, user_id = await _owner(factory, {"role_grants": {"answer_bank_questions": ["viewer"]}})
    members = await _ask_members(factory, company_id, 3)
    allowed = {m: v["email"] for m, v in members.items()}  # who answers now, at which address
    by_address = {v["email"]: m for m, v in members.items()}
    delivered: dict[str, list[str]] = {m: [] for m in members}  # how each delivery was answered
    modes: list[str] = []
    refused: set[str] = set()  # members an email was refused for: their next send is a new one
    misdirected: list[tuple] = []  # sends to a member no longer answering, or to an address they left
    monkeypatch.setattr(email_service, "SEND_TIMEOUT", timedelta(0))  # a cut-off send is unconfirmed at once

    async def transport(to, subject, body_html, body_text="", **kw):
        from celerp.services.company_lock import refuse_held_transaction
        refuse_held_transaction("an email is sent")
        member = by_address[to]
        if allowed.get(member) != to:  # kept, not raised: transmit takes a raise as an unknown outcome
            misdirected.append((member, to, dict(allowed)))
        mode = modes.pop(0) if modes else "accept"
        if mode == "reject":
            refused.add(member)
        else:
            delivered[member].append(mode)
        if mode == "killed":
            raise _Killed
        if mode == "lost":
            raise ConnectionError("answer lost")
        return email_service.EmailResult(mode == "accept", "sent" if mode == "accept" else "refused")
    monkeypatch.setattr(email_service, "send_email", transport)
    cut_off: set[str] = set()  # members whose attempt was pending when its run was killed
    transmit = email_service.transmit

    first = [True]  # the first emails to go: every attempt is this run's, so no other run is sending
    answers: list[dict] = []

    async def tracked(attempts, current=None):
        if first:
            first.clear()
            if rng.random() < 0.5:
                # one member, or everyone, is revoked once the attempts are recorded, just before they go
                for member in sorted(allowed) if rng.random() < 0.5 else [rng.choice(sorted(allowed))]:
                    await _change_member(factory, company_id, member, "revoke")
                    del allowed[member]
        try:
            return await transmit(attempts, current)
        except _Killed:
            cut_off.update(by_address[a.message["to"]] for a in attempts)
            raise
    monkeypatch.setattr(email_service, "transmit", tracked)

    key = f"bank:{uuid.uuid4()}"
    plan = {"asked": [], "note": "a", "members": list(members), "body_key": "acct.recon_questions_body",
            "params": {"count": 1, "n": 0}}
    async with factory() as s:
        await _claimed(s, company_id, user_id, key, {"note": "a"}, "ask", None, plan)
        await s.commit()

    async def run():
        async with factory() as s:
            try:
                answers.append(await email_ask(s, company_id=company_id, key=key))
                return answers[-1]
            except _Killed:
                return "killed"
            except HTTPException as exc:
                return exc.detail["message_key"]

    for _ in range(rng.randint(3, 6)):
        if rng.random() < 0.7 and allowed:
            retried = sorted(refused & allowed.keys())
            member = rng.choice(retried if retried and rng.random() < 0.5 else sorted(allowed))
            how = rng.choice(("revoke", "move"))
            address = await _change_member(factory, company_id, member, how)
            if how == "revoke":
                del allowed[member]
            else:
                allowed[member] = address
                by_address[address] = member
        modes[:] = [rng.choice(TRANSPORT) for _ in range(9)]
        await asyncio.gather(*(run() for _ in range(rng.randint(1, 3))))
    modes.clear()
    final = await run()

    assert misdirected == [], (seed, misdirected)
    for answer in answers:
        # an answer reads as sent only when it names a recipient and every one it names was sent to
        told = {r["state"] for r in answer["email"]["recipients"]}
        assert (answer["email"]["state"] == "sent") == (told == {"sent"}), (seed, answer)
        assert (answer["email"]["state"] == "not_sent") == (not told), (seed, answer)
    assert all(len(d) <= 1 for d in delivered.values()), (seed, delivered)
    if isinstance(final, str):  # refused: nobody answers now and no email ever went
        assert final == "reconciliation.no_client_user" and not any(delivered.values()) and not allowed, (seed, final)
        return
    states = {r["name"]: r["state"] for r in final["email"]["recipients"]}
    for member in members:
        state, ways = states.get(members[member]["name"]), delivered[member]
        if ways:
            # sent only when its own answer came back and was kept (a later email of its run may
            # have been killed before the run settled)
            sent = ways == ["accept"] and member not in cut_off
            assert state == ("sent" if sent else "unconfirmed"), (seed, delivered, cut_off, final)
        elif member in cut_off:
            assert state == "unconfirmed", (seed, member, cut_off, final)  # it may have gone: never resent
        else:
            assert member not in allowed, (seed, member, delivered, allowed)
    left_out = sorted(members[m]["name"] for m in members
                      if m not in allowed and not delivered[m] and m not in cut_off)
    sentence = str(t("acct.recon_asked_left_out", names=", ".join(left_out)))
    assert (sentence in final["outcome"]) == bool(left_out), (seed, left_out, final)
