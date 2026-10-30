# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every path that matches book entries to a reconciliation accepts only entries that
belong in it: this company's posted entries with a line on this bank account, never
the opening entry, and never one already matched to another line of the session. A
transfer sits in both of its accounts' reconciliations. Bulk confirm revalidates each
suggestion and reports the ones it skips, the same way on every retry."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from company_backup_support import token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import _harbor
from test_reconciliation_history import OPENING, admin, bank, deposit, legacy_session, match, start

pytestmark = pytest.mark.asyncio


async def _session(client, h, b, day="2026-09-30", balance=OPENING) -> str:
    r = await start(client, h, b["id"], day, balance)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _lines(client, h, sid, rows: list[tuple[str, str, float]]) -> list[dict]:
    csv = "Date,Description,Amount\n" + "".join(f"{d},{desc},{amt}\n" for d, desc, amt in rows)
    r = await client.post(f"/accounting/reconciliation/{sid}/import-csv", headers=h,
                          files={"file": ("s.csv", csv.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    return await _read_lines(client, h, sid)


async def _read_lines(client, h, sid) -> list[dict]:
    body = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)).json()
    return body.get("items") if isinstance(body, dict) else body


async def _suggest(session, line_id: str, je_id: str) -> None:
    """A suggestion as auto-match leaves it on a statement line."""
    await session.execute(text(
        "UPDATE bank_statement_lines SET status = 'suggested', matched_je_id = :je WHERE id = :l"),
        {"je": je_id, "l": line_id})
    await session.commit()


async def _held(client, h, sid) -> list[str]:
    return (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["reconciled_je_ids"]


# --- One validator on every match path -------------------------------------------------

async def test_match_refuses_an_entry_of_another_bank_account(client, session):
    _, h = await admin(client)
    a = await bank(client, h)
    other = await bank(client, h, name="Second Bank", number="0002")
    je = await deposit(client, h, other, "2026-09-05", 20.0)
    sid = await _session(client, h, a)
    r = await match(client, h, sid, [je])
    assert r.status_code == 400, r.text
    assert je not in await _held(client, h, sid)
    line = (await _lines(client, h, sid, [("2026-09-05", "Deposit", "20.00")]))[0]
    r = await client.post(f"/accounting/reconciliation/{sid}/lines/{line['id']}/match",
                          headers=h, json={"je_id": je})
    assert r.status_code == 400, r.text


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
    sid = await _session(client, h, b, balance=OPENING + 20.0)
    first, second = await _lines(client, h, sid, [("2026-09-05", "Deposit", "20.00"),
                                                    ("2026-09-06", "Deposit again", "20.00")])
    r = await client.post(f"/accounting/reconciliation/{sid}/lines/{first['id']}/match",
                          headers=h, json={"je_id": je})
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/lines/{second['id']}/match",
                          headers=h, json={"je_id": je})
    assert r.status_code == 409, r.text
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
        "from_bank_id": a["id"], "to_bank_id": b["id"], "amount": 30.0, "date": "2026-09-05"})
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


async def test_auto_match_offers_only_eligible_entries(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-05", 20.0)
    sid = await _session(client, h, b, balance=OPENING + 20.0)
    # The same statement held a second time (a row from before one session per date)
    # already counts the entry, so this session may not take it.
    await legacy_session(session, b, "2026-09-30", je_ids=[je], balance=OPENING + 20.0)
    line = (await _lines(client, h, sid, [("2026-09-05", "Deposit", "20.00")]))[0]
    assert (await client.post(f"/accounting/reconciliation/{sid}/auto-match", headers=h)).status_code == 200
    got = {l["id"]: l for l in await _read_lines(client, h, sid)}[line["id"]]
    assert got["matched_je_id"] != je, "an entry the same statement already counts was offered"
    assert je not in await _held(client, h, sid)


# --- Bulk confirm ---------------------------------------------------------------------

async def _bulk_setup(client, session, h):
    b = await bank(client, h)
    good = await deposit(client, h, b, "2026-09-05", 20.0)
    gone = await deposit(client, h, b, "2026-09-06", 30.0)
    held = await deposit(client, h, b, "2026-09-07", 40.0)
    sid = await _session(client, h, b, balance=OPENING + 60.0)
    l1, l2, l3, l4 = await _lines(client, h, sid, [
        ("2026-09-05", "A", "20.00"), ("2026-09-06", "B", "30.00"),
        ("2026-09-07", "C", "40.00"), ("2026-09-07", "D", "40.00")])
    r = await client.post(f"/accounting/reconciliation/{sid}/lines/{l3['id']}/match",
                          headers=h, json={"je_id": held})
    assert r.status_code == 200, r.text
    await _suggest(session, l1["id"], good)
    await _suggest(session, l2["id"], gone)
    await _suggest(session, l4["id"], held)
    assert (await client.post(f"/accounting/journal-entries/{gone}/void", headers=h, json={})).status_code == 200
    return sid, good, gone, held, l1, l2, l4


async def test_bulk_confirm_skips_stale_lines_and_reports_each(client, session):
    _, h = await admin(client)
    sid, good, gone, held, l1, l2, l4 = await _bulk_setup(client, session, h)
    r = await client.post(f"/accounting/reconciliation/{sid}/bulk-confirm", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["confirmed"] == 1
    assert {s["line_id"]: s["reason"] for s in body["skipped"]} == {
        l2["id"]: "acct.err_recon_book_entry_missing", l4["id"]: "acct.err_recon_book_entry_matched"}
    held_ids = await _held(client, h, sid)
    assert good in held_ids and gone not in held_ids and held_ids.count(held) == 1
    lines = {l["id"]: l for l in await _read_lines(client, h, sid)}
    assert lines[l2["id"]]["status"] == "unmatched" and lines[l4["id"]]["status"] == "unmatched"


async def test_bulk_confirm_retry_is_deterministic(client, session):
    _, h = await admin(client)
    sid, *_ = await _bulk_setup(client, session, h)
    first = (await client.post(f"/accounting/reconciliation/{sid}/bulk-confirm", headers=h)).json()
    held = await _held(client, h, sid)
    second = (await client.post(f"/accounting/reconciliation/{sid}/bulk-confirm", headers=h)).json()
    assert first["skipped"] and second == {"confirmed": 0, "skipped": []}
    lines = {l["id"]: l for l in await _read_lines(client, h, sid)}
    assert all(lines[s["line_id"]]["status"] == "unmatched" for s in first["skipped"])
    assert await _held(client, h, sid) == held


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
