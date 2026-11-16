# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every path that matches book entries to a reconciliation accepts only entries that
belong in it: this company's posted entries with a line on this bank account, never
the opening entry, and never one already matched to another line of the session. A
transfer sits in both of its accounts' reconciliations. Post ticked on the bank page
counts the lines it could not post and says why."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from company_backup_support import token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import _harbor
import bank_statement_helpers as B
from test_reconciliation_history import OPENING, admin, bank, deposit, legacy_session, match, start

pytestmark = pytest.mark.asyncio


async def _session(client, h, b, day="2026-09-30", balance=OPENING) -> str:
    r = await start(client, h, b["id"], day, balance)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _lines(client, h, b, rows: list[tuple[str, str, str]]) -> tuple[str, list[dict]]:
    """A September statement on bank ``b`` that ties: its session id and its lines."""
    closing = OPENING + sum(float(amount) for _, _, amount in rows)
    return await B.lines_on(client, await B.co_of(client, h), b["id"], rows, opening=f"{OPENING:.2f}",
                            closing=f"{closing:.2f}", start="2026-09-01", end="2026-09-30")


async def _match_line(client, h, b, line: dict, je: str) -> dict:
    r = await B.resolve(client, await B.co_of(client, h), b["id"],
                        decisions=[{"line_ids": [line["id"]], "kind": "match", "je_ids": [je]}])
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


async def _held(client, h, sid) -> list[str]:
    return (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["reconciled_je_ids"]


# --- One validator on every match path -------------------------------------------------

async def test_match_refuses_an_entry_of_another_bank_account(client, session):
    _, h = await admin(client)
    a = await bank(client, h)
    other = await bank(client, h, name="Second Bank", number="0002")
    je = await deposit(client, h, other, "2026-09-05", 20.0)
    sid, (line,) = await _lines(client, h, a, [("2026-09-05", "Deposit", "20.00")])
    r = await match(client, h, sid, [je])
    assert r.status_code == 400, r.text
    assert je not in await _held(client, h, sid)
    assert (await _match_line(client, h, a, line, je))["ok"] is False


async def test_match_refuses_another_companys_entry(real_engine, real_client):
    boss, a, other_co = await _harbor(real_engine)
    ha, hb = auth(await token(real_engine, boss, a)), auth(await token(real_engine, boss, other_co))
    mine, theirs = await bank(real_client, ha), await bank(real_client, hb)
    je = await deposit(real_client, hb, theirs, "2026-09-05", 20.0)
    sid = await _session(real_client, ha, mine)
    r = await match(real_client, ha, sid, [je])
    assert r.status_code == 400, r.text
    assert je not in await _held(real_client, ha, sid)


async def test_match_refuses_a_voided_entry(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-05", 20.0)
    assert (await client.post(f"/accounting/journal-entries/{je}/void", headers=h, json={})).status_code == 200
    sid = await _session(client, h, b)
    r = await match(client, h, sid, [je])
    assert r.status_code == 400, r.text


async def test_match_refuses_the_opening_entry(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    sid = await _session(client, h, b)
    r = await match(client, h, sid, [f"je:opening:{b['id']}"])
    assert r.status_code == 400, r.text


async def test_match_refuses_an_entry_already_matched_to_another_line(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-05", 20.0)
    _, (first, second) = await _lines(client, h, b, [("2026-09-05", "Deposit", "20.00"),
                                                      ("2026-09-06", "Deposit again", "20.00")])
    assert (await _match_line(client, h, b, first, je))["ok"] is True
    assert (await _match_line(client, h, b, second, je))["ok"] is False
    # A statement held twice (rows from before one session per date): the other session
    # of the same date already counts the entry.
    other = await bank(client, h, name="Second Bank", number="0002")
    je2 = await deposit(client, h, other, "2026-09-05", 25.0)
    sid2 = await _session(client, h, other, balance=OPENING + 25.0)
    await legacy_session(session, other, "2026-09-30", je_ids=[je2], balance=OPENING + 25.0)
    r = await match(client, h, sid2, [je2])
    assert r.status_code == 409, r.text
    assert je2 not in await _held(client, h, sid2)


async def test_transfer_can_be_matched_in_both_accounts(client, session):
    _, h = await admin(client)
    a = await bank(client, h)
    b = await bank(client, h, name="Second Bank", number="0002")
    r = await client.post("/accounting/transfers", headers=h, json={
        "idempotency_key": uuid.uuid4().hex, "from_bank_id": a["id"], "to_bank_id": b["id"], "amount": 30.0, "date": "2026-09-05"})
    assert r.status_code == 200, r.text
    body = r.json()
    je = body.get("je_id") or body.get("id")
    sa = await _session(client, h, a, balance=OPENING - 30.0)
    sb = await _session(client, h, b, balance=OPENING + 30.0)
    assert (await match(client, h, sa, [je])).status_code == 200
    assert (await match(client, h, sb, [je])).status_code == 200
    assert je in await _held(client, h, sa) and je in await _held(client, h, sb)


async def test_repeated_match_is_idempotent(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-05", 20.0)
    sid = await _session(client, h, b, balance=OPENING + 20.0)
    assert (await match(client, h, sid, [je])).status_code == 200
    assert (await match(client, h, sid, [je])).status_code == 200
    assert (await _held(client, h, sid)).count(je) == 1


async def test_suggest_offers_only_eligible_entries(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-05", 20.0)
    # An earlier statement (a row from before statements were imported) already counts
    # the entry, so no line of this one may be offered it.
    await legacy_session(session, b, "2026-08-31", je_ids=[je], balance=OPENING + 20.0)
    _, (line,) = await _lines(client, h, b, [("2026-09-05", "Deposit", "20.00")])
    co = await B.co_of(client, h)
    assert (await client.post(f"{B.API}/bank/{b['id']}/suggest", json={"idempotency_key": B.key()},
                              headers=h)).status_code == 200
    got = next(l for l in B.all_lines(await B.groups(client, co, b["id"], view="all")) if l["id"] == line["id"])
    assert je not in ((got.get("suggestion") or {}).get("je_ids") or []), "an entry another statement counts was offered"


# --- Post ticked ----------------------------------------------------------------------

async def test_post_ticked_counts_the_lines_it_could_not_post_and_says_why(client, session):
    """One group's suggestion posts, the other names an account the chart does not hold:
    the outcome counts the refused line and gives its reason."""
    from test_reconciliation_reopen import _ui_as
    tok, h = await admin(client)
    b = await bank(client, h)
    _, (good, bad) = await _lines(client, h, b, [("2026-09-05", "ALPHA SHOP", "-20.00"),
                                                  ("2026-09-06", "BETA SHOP", "-30.00")])
    for line, code in ((good, "6800"), (bad, "9999")):
        await session.execute(text("UPDATE bank_statement_lines SET suggestion = CAST(:s AS JSON) WHERE id = :l"),
                              {"s": f'{{"kind": "post", "account_code": "{code}", "source": "rule", "reason": "r"}}',
                               "l": line["id"]})
    await session.commit()
    table = await B.groups(client, await B.co_of(client, h), b["id"])
    ticked = B.accept(table, [g["key"] for g in table["groups"]])
    async with _ui_as(tok) as ui:
        page = (await ui.post(f"/accounting/reconcile/bank/{b['id']}/post-ticked", data={
            "group_key": ticked["group_keys"], "line_id": ticked["line_ids"],
            "version": [ticked["versions"][i] for i in ticked["line_ids"]], "idempotency_key": B.key()})).text
    assert "Posted 1 lines." in page
    assert "1 lines were not posted:" in page


# --- Lock order -----------------------------------------------------------------------

async def test_matcher_and_bulk_void_of_one_bank_do_not_deadlock(real_engine, real_client):
    boss, a, _ = await _harbor(real_engine)
    h = auth(await token(real_engine, boss, a))
    b = await bank(real_client, h)
    ids = [await deposit(real_client, h, b, f"2026-09-{d:02d}", 5.0 + d) for d in range(5, 10)]
    sid = await _session(real_client, h, b)
    matching = match(real_client, h, sid, ids[:2])
    voiding = real_client.post("/accounting/journal-entries/bulk-void", headers=h,
                               json={"je_ids": ids[2:], "reversal_date": "2026-10-15"})
    m, v = await asyncio.wait_for(asyncio.gather(matching, voiding), 60)
    assert m.status_code == 200, m.text
    assert v.status_code == 200, v.text
    async with maker(real_engine)() as s:
        held = (await s.execute(text("SELECT reconciled_je_ids FROM reconciliation_sessions WHERE id = :s"),
                                {"s": sid})).scalar()
    assert set(ids[:2]) <= set(held)
