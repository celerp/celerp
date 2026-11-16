# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A resolve that stops part way: each decision keeps its own outcome with its effect, so a
repeat of the key runs only the decisions not yet carried out and answers the truth."""
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
