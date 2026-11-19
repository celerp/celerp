# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Bank suggestions against other writers of the same company, on real PostgreSQL
connections against committed data in a database of the test's own.

A suggest reads the open lines, asks the model with no lock held, then writes under the
company lock only when its key is still unused and the lines and what the ladder made of
them are as they were read; otherwise nothing is written."""

from __future__ import annotations

import asyncio
import json
import random
import uuid

import pytest
from sqlalchemy import text

import bank_statement_helpers as B
from test_bank_complete_race_pg import own_client  # noqa: F401

pytestmark = pytest.mark.asyncio

NEW, OTHER = "SOMETHING NEW CO", "ANOTHER NEW THING"


class Model:
    """A model whose calls are answered in turn by ``plans``: ("ok", reason) answers every
    group with 5100 and that reason, ("hold", reason) waits for ``go`` first, ("fail", _) raises."""

    def __init__(self, *plans):
        self.plans, self.calls, self.held, self.go = list(plans), 0, asyncio.Event(), asyncio.Event()

    async def __call__(self, groups, accounts):
        kind, reason = self.plans[min(self.calls, len(self.plans) - 1)]
        self.calls += 1
        if kind == "hold":
            self.held.set()
            await self.go.wait()
        if kind == "fail":
            raise RuntimeError("model down")
        return {g["key"]: {"account_code": "5100", "reason": reason} for g in groups}


async def _bank(c, rows=((NEW, "-40.00"), (OTHER, "-20.00"))):
    co = await B.company(c)
    bk = await B.bank(c, co["h"])
    total = sum(float(a) for _, a in rows)
    await B.statement(c, co, bk["id"], [(f"2026-01-0{i + 5}", d, a) for i, (d, a) in enumerate(rows)],
                      opening="0.00", closing=f"{total:.2f}", start="2026-01-01", end="2026-01-31")
    return co, bk


def _suggest(c, co, bk, key):
    return c.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": key}, headers=co["h"])


async def _outcomes(engine, co, key) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT count(*) FROM ledger WHERE company_id = :c AND idempotency_key = :k"),
            {"c": co["company_id"], "k": key})).scalar_one()


async def _suggestions(c, co, bk) -> dict:
    return {l["description"]: l["suggestion"] for l in B.all_lines(await B.groups(c, co, bk["id"], view="all"))}


class Service:
    """The model service as ``llm.complete`` reaches it, per operation id: the first send
    runs (the first run waits for ``go`` when ``hold``), a send while it runs is refused
    operation_in_progress, a later send of the same content answers the kept reply and one
    of other content is refused operation_mismatch. Like the real client it awaits
    ``before_send`` before anything reaches the service; ``lose`` charges the first run
    and then loses its reply in transport."""

    def __init__(self, reason: str, hold: bool = False, mismatch: int = 0, lose: bool = False):
        self.reason, self.hold, self.runs, self.refused, self.mismatch = reason, hold, 0, 0, mismatch
        self.lose = lose
        self.kept, self.running, self.sent = {}, set(), []
        self.held, self.go, self.waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def complete(self, messages, *, operation_id, before_send=None, **_):
        import httpx

        from celerp.ai.llm import ModelResult, RelayError

        content = json.dumps(messages, sort_keys=True)
        if before_send is not None:
            await before_send(len(content))
        self.sent.append((operation_id, json.loads(messages[-1]["content"])))
        if self.mismatch:  # a service that reports a mismatch however it was sent
            self.mismatch -= 1
            raise RelayError("operation_mismatch", "other content", status=422)
        if operation_id in self.running:
            self.refused += 1
            if self.refused >= 3:
                self.waiting.set()
            raise RelayError("operation_in_progress", "still running", status=409, retry_after=0.01)
        if operation_id in self.kept:
            if self.kept[operation_id][0] != content:
                raise RelayError("operation_mismatch", "other content", status=422)
            return self.kept[operation_id][1]
        self.running.add(operation_id)
        self.runs += 1
        try:
            if self.hold and self.runs == 1:
                self.held.set()
                await self.go.wait()
        finally:
            self.running.discard(operation_id)
        reply = {g["key"]: {"account_code": "5100", "reason": self.reason}
                 for g in json.loads(messages[-1]["content"])["groups"]}
        result = ModelResult(message={"content": json.dumps(reply)}, model_used="test", usage={"credits": 1},
                             reservation_id=None, remaining=None, operation_credits=1)
        self.kept[operation_id] = (content, result)
        if self.lose:
            raise httpx.ReadError("connection reset after the service charged")
        return result


def _service(monkeypatch, service: Service) -> Service:
    """Bank suggestions ask ``service`` through the real model rung."""
    from celerp.ai import llm
    from celerp_accounting import suggest

    monkeypatch.setattr(suggest, "model_rung", suggest.ask_model)
    monkeypatch.setattr(llm, "complete", service.complete)
    return service


def _said(suggestion) -> str | None:
    """The model's reason a suggestion starts with, None for no suggestion."""
    return None if suggestion is None else str(suggestion.get("reason") or "").split(" No record")[0]


async def test_same_key_runs_write_once_and_the_later_answers_the_first_result(committed_engine, own_client,
                                                                               monkeypatch):
    from celerp_accounting import suggest

    c = own_client
    co, bk = await _bank(c)
    model = Model(("hold", "slow run"), ("ok", "fast run"))
    monkeypatch.setattr(suggest, "model_rung", model)
    key = B.key()
    slow = asyncio.create_task(_suggest(c, co, bk, key))
    await asyncio.wait_for(model.held.wait(), timeout=20)
    fast = await _suggest(c, co, bk, key)
    assert fast.status_code == 200, fast.text
    model.go.set()
    late = await asyncio.wait_for(slow, timeout=60)
    assert late.status_code == 200 and late.json() == fast.json(), late.text
    assert await _outcomes(committed_engine, co, key) == 1
    assert {_said(s) for s in (await _suggestions(c, co, bk)).values()} == {"fast run"}


async def test_a_line_resolved_while_the_model_runs_refuses_and_writes_nothing(committed_engine, own_client,
                                                                               monkeypatch):
    from celerp_accounting import suggest

    c = own_client
    co, bk = await _bank(c)
    model = Model(("hold", "stale"))
    monkeypatch.setattr(suggest, "model_rung", model)
    key = B.key()
    run = asyncio.create_task(_suggest(c, co, bk, key))
    await asyncio.wait_for(model.held.wait(), timeout=20)
    other = B.line_by_text(await B.groups(c, co, bk["id"]), OTHER)
    r = await B.resolve(c, co, bk["id"], decisions=[{"line_ids": [other["id"]], "kind": "post",
                                                     "account_code": "5100"}])
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    model.go.set()
    refused = await asyncio.wait_for(run, timeout=60)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["message_key"] == "acct.err_suggest_stale"
    assert await _outcomes(committed_engine, co, key) == 0
    after = await _suggestions(c, co, bk)
    assert after[NEW] is None and _said(after[OTHER]) != "stale", after
    again = await _suggest(c, co, bk, key)  # the same key, on the lines as they are now
    assert again.status_code == 200, again.text
    after = await _suggestions(c, co, bk)
    assert _said(after[NEW]) == "stale" and _said(after[OTHER]) != "stale", after


async def test_a_failed_model_records_no_outcome_and_the_same_key_runs_again(committed_engine, own_client,
                                                                              monkeypatch):
    from celerp_accounting import suggest

    c = own_client
    co, bk = await _bank(c, rows=((NEW, "-40.00"),))
    model = Model(("fail", ""), ("ok", "second try"))
    monkeypatch.setattr(suggest, "model_rung", model)
    key = B.key()
    failed = await _suggest(c, co, bk, key)
    assert failed.status_code == 200 and failed.json()["model_ran"] is False, failed.text
    assert await _outcomes(committed_engine, co, key) == 0
    assert (await _suggestions(c, co, bk))[NEW]["source"] == "question"
    again = await _suggest(c, co, bk, key)
    assert again.status_code == 200 and again.json()["model_ran"] is True, again.text
    assert model.calls == 2 and await _outcomes(committed_engine, co, key) == 1
    assert _said((await _suggestions(c, co, bk))[NEW]) == "second try"


async def test_a_relay_error_other_than_a_mismatch_records_no_outcome(committed_engine, own_client, monkeypatch):
    """A RelayError the service reports for a reason other than a stale identity (here
    ``no_session``) is the same ``model could not answer`` outcome as any other model
    failure: no outcome recorded, the suggestions fall back to questions, and the key asks
    again. Distinct from ``operation_mismatch`` (refused 409, nothing written at all)."""
    from celerp.ai.llm import RelayError
    from celerp_accounting import suggest

    def model_rung(groups, accounts):
        raise RelayError("no_session", "signed out of Celerp Connect")

    c = own_client
    co, bk = await _bank(c, rows=((NEW, "-40.00"),))
    monkeypatch.setattr(suggest, "model_rung", model_rung)
    key = B.key()
    failed = await _suggest(c, co, bk, key)
    assert failed.status_code == 200 and failed.json()["model_ran"] is False, failed.text
    assert await _outcomes(committed_engine, co, key) == 0
    assert (await _suggestions(c, co, bk))[NEW]["source"] == "question"


async def _usage(engine, co) -> list:
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(text(
            "SELECT user_id, operation_id, credits, pending FROM ai_usage WHERE company_id = :c"),
            {"c": co["company_id"]})).mappings()]


async def test_a_reply_lost_after_the_service_charged_leaves_a_pending_usage_row_the_reconciler_settles(
        committed_engine, own_client, monkeypatch):
    """The usage row is committed before the model step is sent, so a reply lost in
    transport after the service charged still has its row: pending, and settled from the
    operation's status by the background reconcile pass."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from celerp.ai import llm, usage

    c = own_client
    co, bk = await _bank(c, rows=((NEW, "-40.00"),))
    _service(monkeypatch, Service("lost", lose=True))
    failed = await _suggest(c, co, bk, B.key())
    assert failed.status_code == 200 and failed.json()["model_ran"] is False, failed.text
    rows = await _usage(committed_engine, co)
    assert [(r["credits"], r["pending"]) for r in rows] == [(0, True)], rows

    async def _operations_status(ids):
        return [{"operation_id": str(i), "state": "charged", "credits": 1} for i in ids]

    monkeypatch.setattr(llm, "operations_status", _operations_status)
    async with committed_engine.begin() as conn:
        await conn.execute(text("UPDATE ai_usage SET reconcile_at = now() - interval '1 second' WHERE company_id = :c"),
                           {"c": co["company_id"]})
    async with async_sessionmaker(bind=committed_engine, expire_on_commit=False)() as session:
        await usage.reconcile_usage(session)
    assert [(r["credits"], r["pending"]) for r in await _usage(committed_engine, co)] == [(1, False)]


async def test_an_answered_model_step_leaves_one_settled_usage_row(committed_engine, own_client, monkeypatch):
    c = own_client
    co, bk = await _bank(c, rows=((NEW, "-40.00"),))
    service = _service(monkeypatch, Service("answered"))
    done = await _suggest(c, co, bk, B.key())
    assert done.status_code == 200 and done.json()["model_ran"] is True, done.text
    rows = await _usage(committed_engine, co)
    assert [(r["operation_id"], r["credits"], r["pending"]) for r in rows] == [(service.sent[0][0], 1, False)], rows


async def test_interleavings_write_only_when_the_recheck_matches(committed_engine, own_client, monkeypatch):
    """Seeded: while the model runs, any of a resolved line, a rule that answers a line (the
    ladder changes), a rule that answers nothing (the ladder does not), a same-key run that
    finishes first. The held run writes iff its key is unused and its ladder is unchanged."""
    from celerp_accounting import suggest

    c = own_client
    rng = random.Random(4290)
    for trial in range(10):
        line_change, relevant, irrelevant, same_key = (rng.random() < 0.4 for _ in range(4))
        co, bk = await _bank(c)
        model = Model(("hold", f"held {trial}"), ("ok", f"rival {trial}"))
        monkeypatch.setattr(suggest, "model_rung", model)
        key = B.key()
        run = asyncio.create_task(_suggest(c, co, bk, key))
        await asyncio.wait_for(model.held.wait(), timeout=20)
        if irrelevant:
            r = await c.post("/accounting/rules", headers=co["h"], json={
                "match_pattern": "NOTHING MATCHES THIS", "target_account_code": "5200", "idempotency_key": B.key()})
            assert r.status_code == 200, r.text
        if relevant:
            r = await c.post("/accounting/rules", headers=co["h"], json={
                "match_pattern": "SOMETHING NEW", "target_account_code": "5200", "idempotency_key": B.key()})
            assert r.status_code == 200, r.text
        if line_change:
            other = B.line_by_text(await B.groups(c, co, bk["id"]), OTHER)
            r = await B.resolve(c, co, bk["id"], decisions=[{"line_ids": [other["id"]], "kind": "post",
                                                             "account_code": "5100"}])
            assert r.status_code == 200, r.text
        rival = await _suggest(c, co, bk, key) if same_key else None
        before = await _suggestions(c, co, bk)
        model.go.set()
        held = await asyncio.wait_for(run, timeout=60)
        after = await _suggestions(c, co, bk)
        case = (trial, line_change, relevant, irrelevant, same_key)
        if same_key:
            assert rival.status_code == 200 and held.status_code == 200 and held.json() == rival.json(), case
            assert after == before, case
        elif line_change or relevant:
            assert held.status_code == 409, (case, held.text)
            assert after == before, case
        else:
            assert held.status_code == 200, (case, held.text)
            assert _said(after[NEW]) == _said(after[OTHER]) == f"held {trial}", case
        assert await _outcomes(committed_engine, co, key) == (1 if same_key or not (line_change or relevant) else 0), case


async def test_an_identical_request_waits_for_the_running_model_step_and_answers_its_result(
        committed_engine, own_client, monkeypatch):
    """A same-key request whose model step the service still runs (operation_in_progress) is
    no model failure: it writes nothing while the first runs, then answers the first result."""
    c = own_client
    co, bk = await _bank(c)
    service = _service(monkeypatch, Service("one model run", hold=True))
    key = B.key()
    first = asyncio.create_task(_suggest(c, co, bk, key))
    await asyncio.wait_for(service.held.wait(), timeout=20)
    second = asyncio.create_task(_suggest(c, co, bk, key))
    waiting = asyncio.create_task(service.waiting.wait())
    await asyncio.wait({second, waiting}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
    assert not second.done(), f"answered while the first model step ran: {second.result().text}"
    assert await _outcomes(committed_engine, co, key) == 0
    assert set((await _suggestions(c, co, bk)).values()) == {None}
    service.go.set()
    done = [await asyncio.wait_for(task, timeout=60) for task in (first, second)]
    assert [r.status_code for r in done] == [200, 200], [r.text for r in done]
    assert done[0].json() == done[1].json() and done[0].json()["model_ran"] is True, done[1].text
    assert service.runs == 1 and await _outcomes(committed_engine, co, key) == 1
    assert {_said(s) for s in (await _suggestions(c, co, bk)).values()} == {"one model run"}


async def test_a_request_still_waiting_on_the_running_step_at_its_deadline_refuses_and_writes_nothing(
        committed_engine, own_client, monkeypatch):
    """Past the synchronous wait, the later request says the same request is still running
    (409), writes no suggestion and records no outcome; the first then finishes as usual."""
    from celerp.ai import deadlines

    c = own_client
    co, bk = await _bank(c)
    service = _service(monkeypatch, Service("first run", hold=True))
    key = B.key()
    first = asyncio.create_task(_suggest(c, co, bk, key))
    await asyncio.wait_for(service.held.wait(), timeout=20)
    monkeypatch.setattr(deadlines, "SYNC_WAIT_S", 0.2)
    refused = await asyncio.wait_for(_suggest(c, co, bk, key), timeout=20)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["message_key"] == "acct.err_suggest_running"
    assert await _outcomes(committed_engine, co, key) == 0
    assert set((await _suggestions(c, co, bk)).values()) == {None}
    service.go.set()
    done = await asyncio.wait_for(first, timeout=60)
    assert done.status_code == 200 and done.json()["model_ran"] is True, done.text
    assert await _outcomes(committed_engine, co, key) == 1


async def test_a_retry_after_a_stale_refusal_with_changed_lines_is_a_new_model_step_under_the_same_key(
        committed_engine, own_client, monkeypatch):
    """The model step's identity is the request plus everything the model sees: after a stale
    refusal, the same key on changed lines is a distinct step, asked afresh (never refused as
    sent with other content), and its suggestions are recorded under the original key."""
    c = own_client
    co, bk = await _bank(c, rows=((NEW, "-40.00"), (OTHER, "-20.00"), ("A THIRD NEW ONE", "-10.00")))
    service = _service(monkeypatch, Service("asked", hold=True))
    key = B.key()
    run = asyncio.create_task(_suggest(c, co, bk, key))
    await asyncio.wait_for(service.held.wait(), timeout=20)
    first_op, first_input = service.sent[0]
    keep = first_input["groups"][0]["sample"]  # the first group stays; another line is resolved
    lines = B.all_lines(await B.groups(c, co, bk["id"]))
    gone = next(l for l in lines if l["description"] not in str(keep))
    r = await B.resolve(c, co, bk["id"], decisions=[{"line_ids": [gone["id"]], "kind": "post",
                                                     "account_code": "5100"}])
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    service.go.set()
    refused = await asyncio.wait_for(run, timeout=60)
    assert refused.status_code == 409 and refused.json()["detail"]["message_key"] == "acct.err_suggest_stale"
    again = await _suggest(c, co, bk, key)
    assert again.status_code == 200 and again.json()["model_ran"] is True, again.text
    op, seen = service.sent[-1]
    assert op != first_op and seen != first_input and service.runs == 2
    assert await _outcomes(committed_engine, co, key) == 1
    after = await _suggestions(c, co, bk)
    assert after[gone["description"]] is None or _said(after[gone["description"]]) != "asked", after
    assert {_said(v) for d, v in after.items() if d != gone["description"]} == {"asked"}, after
    replay = await _suggest(c, co, bk, key)
    assert replay.json() == again.json() and service.runs == 2


async def test_a_reported_mismatch_is_refused_with_nothing_written_and_the_key_asks_again(
        committed_engine, own_client, monkeypatch):
    """A service that still reports the step as sent with other content is no plain model
    failure: 409, no suggestion, no outcome, and the same key then asks and is answered."""
    c = own_client
    co, bk = await _bank(c)
    service = _service(monkeypatch, Service("after mismatch", mismatch=1))
    key = B.key()
    refused = await _suggest(c, co, bk, key)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["message_key"] == "acct.err_suggest_mismatch"
    assert await _outcomes(committed_engine, co, key) == 0
    assert set((await _suggestions(c, co, bk)).values()) == {None}
    again = await _suggest(c, co, bk, key)
    assert again.status_code == 200 and again.json()["model_ran"] is True, again.text
    assert await _outcomes(committed_engine, co, key) == 1
