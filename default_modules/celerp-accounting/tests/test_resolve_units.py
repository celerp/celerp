# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A resolve that stops part way: each decision, and each line of an accept, keeps its own
outcome with its effect, so a repeat of the key runs only the units not yet carried out and
answers the truth."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

import bank_statement_helpers as B
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio

_ROWS = [("2026-01-05", "FEE A", "-5.00"), ("2026-01-06", "FEE B", "-7.00"), ("2026-01-07", "FEE C", "-9.00")]


class Stopped(Exception):
    """The process stopping between two decisions."""


async def _setup(client):
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    await B.statement(client, co, bk["id"], _ROWS, opening="0.00", closing="-21.00", start="2026-01-01",
                      end="2026-01-31", tick_all=True)
    lines = sorted(B.all_lines(await B.groups(client, co, bk["id"], view="all")), key=lambda l: l["line_date"])
    return co, bk, lines


async def _entries(session, co) -> dict:
    """Every entry reconciliation wrote for ``co``: id to status."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == uuid.UUID(co["company_id"]), Projection.entity_type == "journal_entry",
        Projection.entity_id.like("je:recon:%")))).scalars()
    return {r.entity_id: (r.state or {}).get("status") for r in rows}


def _stop_after(k: int):
    """Let ``k`` decisions through, then stop the request."""
    import celerp_accounting.bank_statements as BS
    real, seen = BS._decide, []

    async def decide(*a, **kw):
        if len(seen) == k:
            raise Stopped
        seen.append(1)
        return await real(*a, **kw)
    return patch.object(BS, "_decide", decide)


def _stop_before_answer():
    import celerp_accounting.bank_statements as BS
    real = BS._recorded

    async def recorded(db, company_id, actor_id, key, request, operation, *a, **kw):
        if operation == "resolve":
            raise Stopped
        return await real(db, company_id, actor_id, key, request, operation, *a, **kw)
    return patch.object(BS, "_recorded", recorded)


@pytest.mark.parametrize("k", [0, 1, 2, 3])
async def test_a_repeat_after_a_stop_runs_only_the_rest(client, session, k):
    co, bk, lines = await _setup(client)
    idem = B.key()
    dec = [{"line_ids": [l["id"]], "kind": "post", "account_code": "5100"} for l in lines]
    with _stop_after(k) if k < len(lines) else _stop_before_answer(), pytest.raises(Stopped):
        await B.resolve(client, co, bk["id"], decisions=dec, idem=idem)
    assert len([s for s in (await _entries(session, co)).values() if s == "posted"]) == k

    again = await B.resolve(client, co, bk["id"], decisions=dec, idem=idem)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["refused"] == 0 and [r["line_id"] for r in body["results"]] == [l["id"] for l in lines]
    entries = await _entries(session, co)
    assert sorted(entries) == sorted(j for r in body["results"] for j in r["je_ids"])
    assert set(entries.values()) == {"posted"}
    rows = {l["id"]: l for l in B.all_lines(await B.groups(client, co, bk["id"], view="all"))}
    assert all(rows[r["line_id"]]["matched_je_ids"] == r["je_ids"] for r in body["results"])
    assert (await B.resolve(client, co, bk["id"], decisions=dec, idem=idem)).json() == body


async def test_an_undo_between_the_stop_and_the_repeat_stands(client, session):
    co, bk, lines = await _setup(client)
    idem = B.key()
    dec = [{"line_ids": [l["id"]], "kind": "post", "account_code": "5100"} for l in lines]
    with _stop_after(1), pytest.raises(Stopped):
        await B.resolve(client, co, bk["id"], decisions=dec, idem=idem)
    first = next(iter(await _entries(session, co)))
    r = await client.post(f"{B.API}/bank/{bk['id']}/unmatch", json={"line_ids": [lines[0]["id"]],
                                                                    "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text

    again = (await B.resolve(client, co, bk["id"], decisions=dec, idem=idem)).json()
    assert again["results"][0]["je_ids"] == [first]
    entries = await _entries(session, co)
    assert entries[first] == "void" and len([s for s in entries.values() if s == "posted"]) == 2
    rows = {l["id"]: l for l in B.all_lines(await B.groups(client, co, bk["id"], view="all"))}
    assert rows[lines[0]["id"]]["status"] == "unmatched"
    assert all(j in entries for row in rows.values() for j in row.get("matched_je_ids") or [])


async def test_a_refused_decision_keeps_its_answer_on_repeat(client, session):
    co, bk, lines = await _setup(client)
    idem = B.key()
    dec = [{"line_ids": [lines[0]["id"]], "kind": "post", "account_code": "NOPE"},
           {"line_ids": [lines[1]["id"]], "kind": "post", "account_code": "5100"}]
    first = await B.resolve(client, co, bk["id"], decisions=dec, idem=idem)
    assert first.json()["refused"] == 1
    again = await B.resolve(client, co, bk["id"], decisions=dec, idem=idem)
    assert again.json() == first.json()
    assert len(await _entries(session, co)) == 1


# Two lines answered by decisions and three accepted from two rule groups: five units.
_MIX = [("2026-01-05", "FEE A", "-5.00"), ("2026-01-06", "NETFLIX 1", "-15.99"), ("2026-01-07", "FEE B", "-7.00"),
        ("2026-01-08", "NETFLIX 2", "-15.99"), ("2026-01-09", "SPOTIFY 1", "-9.99")]


async def mixed(client) -> tuple[dict, dict, dict]:
    """A bank whose resolve answers the FEE lines by decision and accepts the rule groups'
    suggestions for the rest; the resolve's keyword arguments."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    await B.statement(client, co, bk["id"], _MIX, opening="0.00", closing="-53.97", start="2026-01-01",
                      end="2026-01-31")
    for pattern in ("NETFLIX", "SPOTIFY"):
        r = await client.post("/accounting/rules", json={"match_pattern": pattern, "target_account_code": "5100",
                                                         "idempotency_key": B.key()}, headers=co["h"])
        assert r.status_code == 200, r.text
    r = await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    table = await B.groups(client, co, bk["id"])
    lines = B.all_lines(table)
    fees = [l["id"] for l in lines if l["description"].startswith("FEE")]
    rule_lines = [l["id"] for l in lines if not l["description"].startswith("FEE") and l["suggestion"]]
    accept = B.accept(table, [g["key"] for g in table["groups"] if {l["id"] for l in g["lines"]} & set(rule_lines)],
                      rule_lines)
    assert len(fees) == 2 and len(accept["line_ids"]) == 3 and not set(fees) & set(accept["line_ids"])
    return co, bk, {"decisions": [{"line_ids": [f], "kind": "post", "account_code": "5100"} for f in fees],
                    "accept": accept}


async def projections(session, co) -> dict:
    """Every stored record of ``co``: (entity type, id) to its state."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == uuid.UUID(co["company_id"])))).scalars()
    return {(r.entity_type, r.entity_id): r.state for r in rows}


async def every_line_once(client, session, co, bk, body: dict, done_before: dict) -> None:
    """The repeat's answer names each line once, ok; the lines done before it keep the entries
    they had; every line carries its answer's entries; a rebuild from the ledger gives the
    stored records."""
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company

    ids = [r["line_id"] for r in body["results"]]
    lines = {l["id"]: l for l in B.all_lines(await B.groups(client, co, bk["id"], view="all"))}
    assert body["refused"] == 0 and sorted(ids) == sorted(lines), body
    assert all(r["je_ids"] == done_before[r["line_id"]] for r in body["results"] if r["line_id"] in done_before)
    assert all(lines[r["line_id"]]["status"] == "posted" and lines[r["line_id"]]["matched_je_ids"] == r["je_ids"]
               for r in body["results"])
    entries = await _entries(session, co)
    assert sorted(entries) == sorted(j for r in body["results"] for j in r["je_ids"]) and len(entries) == len(ids)
    stored = await projections(session, co)
    await lock_company(session, uuid.UUID(co["company_id"]))
    await ProjectionEngine.rebuild(session, company_id=uuid.UUID(co["company_id"]))
    await session.commit()
    assert await projections(session, co) == stored


async def done_lines(client, co, bk) -> dict:
    return {l["id"]: l["matched_je_ids"] for l in B.all_lines(await B.groups(client, co, bk["id"], view="all"))
            if l["status"] == "posted"}


@pytest.mark.parametrize("k", [0, 1, 2, 3, 4, 5])
async def test_decisions_and_accepted_groups_repeat_after_a_stop_at_every_unit(client, session, k):
    """Stopped after each k of the five units (k = 5: the answer is lost), the repeat of the key
    answers the k lines done from their recorded outcomes and runs the rest."""
    co, bk, kw = await mixed(client)
    idem = B.key()
    with _stop_after(k) if k < 5 else _stop_before_answer(), pytest.raises(Stopped):
        await B.resolve(client, co, bk["id"], idem=idem, **kw)
    before = await done_lines(client, co, bk)
    assert len(before) == k

    again = await B.resolve(client, co, bk["id"], idem=idem, **kw)
    assert again.status_code == 200, again.text
    await every_line_once(client, session, co, bk, again.json(), before)
    assert (await B.resolve(client, co, bk["id"], idem=idem, **kw)).json() == again.json()


async def test_an_accepted_line_settled_by_someone_else_is_refused_unseen_never_posted(client, session):
    """The run reads only the confirmed lines: one another request answered under its own key
    is not open, so the accept is refused and nothing posts twice."""
    co, bk, kw = await mixed(client)
    other = kw["accept"]["line_ids"][0]
    first = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [other], "kind": "no_entry"}])
    assert first.json()["refused"] == 0, first.text
    r = await B.resolve(client, co, bk["id"], accept=kw["accept"])
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "reconciliation.line_not_open", r.text
    assert await _entries(session, co) == {}
