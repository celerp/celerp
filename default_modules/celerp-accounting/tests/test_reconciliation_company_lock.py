# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every reconciliation writer takes the company's lock before its bank account, session,
statement line or rule rows: the lock its postings and chart reads take too, so the writer
never waits for the company while holding a reconciliation row, and the lock checker's census
finds no such row taken outside the company lock."""

from __future__ import annotations

import sys

import bank_statement_helpers as B
from test_reconciliation_v2 import _auth, _create_bank, _create_recon

_CLASSES = ("row:bank_accounts", "row:reconciliation_sessions", "row:reconciliation_rules",
            "row:bank_statement_lines")


def _ungated() -> set[str]:
    return sys.modules["lock_rank"].RECORDER.ungated_sites(_CLASSES, "celerp_accounting/")


async def test_every_reconciliation_writer_takes_the_company_lock_first(client):
    before = _ungated()
    h = await _auth(client)
    bank = await _create_bank(client, h)
    r = await client.patch(f"/accounting/bank-accounts/{bank['id']}", json={"bank_name": "Renamed"}, headers=h)
    assert r.status_code == 200, r.text
    recon = await _create_recon(client, h, bank["id"])
    sid = (await B.import_into(client, h, recon["id"], b"Date,Description,Amount\n2026-03-01,Fee,-500\n"
                                b"2026-03-02,Mixed,-1500\n2026-03-03,Skip,-100\n"))["session_id"]
    lines = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)).json()["items"]
    fee, mixed, skip = (line["id"] for line in sorted(lines, key=lambda line: line["line_date"]))
    r = await B.decide(client, h, sid, fee, "post", account_code="6100", memo="Bank fee")
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    r = await B.undo(client, h, sid, [fee])
    assert r.status_code == 200, r.text
    r = await B.decide(client, h, sid, mixed, "post", splits=[
        {"account_code": "5100", "amount": 1000}, {"account_code": "6300", "amount": 500}])
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    r = await B.decide(client, h, sid, skip, "no_entry")
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    r = await B.bank_post(client, h, sid, "suggest")
    assert r.status_code == 200, r.text
    r = await B.decide(client, h, sid, fee, "post", account_code="6100", memo="Bank fee")
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    r = await client.post("/accounting/rules", json={
        "bank_account_id": bank["id"], "match_pattern": "POS", "match_type": "contains",
        "target_account_code": "6100", "default_memo": "POS fee"}, headers=h)
    assert r.status_code == 200, r.text
    rule = r.json()["id"]
    r = await client.patch(f"/accounting/rules/{rule}", json={"is_active": False}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.delete(f"/accounting/rules/{rule}", headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/write-off", json={"adjustment_account": "6950"},
                          headers=h)
    assert r.status_code in (200, 422), r.text
    r = await B.bank_post(client, h, sid, "complete")
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/reopen", headers=h)
    assert r.status_code in (200, 409), r.text
    assert _ungated() - before == set()


async def _keyed(client, co, bk, amount: float, day: str) -> str:
    r = await client.post("/accounting/journal-entries", headers=co["h"], json={
        "ts": day, "memo": "keyed", "idempotency_token": B.key(),
        "entries": [{"account": "5100", "debit": amount, "credit": 0.0},
                    {"account": bk["chart_account_code"], "debit": 0.0, "credit": amount}]})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def test_every_resolve_and_accept_outcome_takes_the_company_lock_before_a_line(client):
    """A resolve's decisions (a match, a post, a post matched to the one entry already keyed, a
    post refused for several keyed entries with its suggestion written) and a group accept (a
    post and a match suggestion) take the company's lock before any statement line's."""
    before = _ungated()
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    rows = [("2026-01-05", "MATCH", "-10.00"), ("2026-01-06", "POST", "-11.00"),
            ("2026-01-07", "ONE TWIN", "-12.00"), ("2026-01-20", "TWO TWINS", "-13.00"),
            ("2026-01-22", "NETFLIX", "-14.00"), ("2026-01-24", "PAIRED", "-16.00")]
    await B.statement(client, co, bk["id"], rows, opening="0.00", closing="-76.00", start="2026-01-01",
                      end="2026-01-31")
    match = await _keyed(client, co, bk, 10.0, "2026-01-05")
    one = await _keyed(client, co, bk, 12.0, "2026-01-07")
    for day in ("2026-01-19", "2026-01-21"):
        await _keyed(client, co, bk, 13.0, day)
    await _keyed(client, co, bk, 16.0, "2026-01-24")
    table = await B.groups(client, co, bk["id"])
    ids = {line["description"]: line["id"] for line in B.all_lines(table)}
    r = await B.resolve(client, co, bk["id"], decisions=[
        {"line_ids": [ids["MATCH"]], "kind": "match", "je_ids": [match]},
        *({"line_ids": [ids[t]], "kind": "post", "account_code": "5100"} for t in ("POST", "ONE TWIN", "TWO TWINS"))])
    assert r.status_code == 200, r.text
    by_id = {row["line_id"]: row for row in r.json()["results"]}
    got = {t: ("instead" if by_id[i]["matched_instead"] else "done" if by_id[i]["ok"] else "refused")
           for t, i in ids.items() if i in by_id}
    assert got == {"MATCH": "done", "POST": "done", "ONE TWIN": "instead", "TWO TWINS": "refused"}, by_id
    assert by_id[ids["ONE TWIN"]]["je_ids"] == [one]
    r = await client.post("/accounting/rules", json={"match_pattern": "NETFLIX", "target_account_code": "5100",
                                                     "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    r = await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    table = await B.groups(client, co, bk["id"])
    accept = B.accept(table, [g["key"] for g in table["groups"]], [ids["NETFLIX"], ids["PAIRED"]])
    assert len(accept["line_ids"]) == 2, table
    r = await B.resolve(client, co, bk["id"], accept=accept)
    assert r.status_code == 200 and r.json()["refused"] == 0, r.text
    assert _ungated() - before == set()
