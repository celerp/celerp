# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Bank suggestions against other writers of the same company, on real PostgreSQL
connections against committed data in a database of the test's own.

A suggest reads the open lines, asks the model with no lock held, then writes under the
company lock only when its key is still unused and the lines and what the ladder made of
them are as they were read; otherwise nothing is written."""

from __future__ import annotations

import asyncio
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
