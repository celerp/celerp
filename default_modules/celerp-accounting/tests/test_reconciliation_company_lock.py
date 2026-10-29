# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every reconciliation writer takes the company's lock before its bank account, session,
statement line or rule rows: the lock its postings and chart reads take too, so the writer
never waits for the company while holding a reconciliation row, and the lock checker's census
finds no such row taken outside the company lock."""

from __future__ import annotations

import sys

from test_reconciliation_v2 import _auth, _create_bank, _create_recon, _import_csv

_CLASSES = ("row:bank_accounts", "row:reconciliation_sessions", "row:reconciliation_rules",
            "row:bank_statement_lines")


def _ungated() -> set[str]:
    classes = sys.modules["lock_rank"].RECORDER.dump()["classes"]
    return {s for c in _CLASSES for s in classes.get(c, {}).get("ungated_sites", ())
            if "celerp_accounting/routes.py" in s}


async def test_every_reconciliation_writer_takes_the_company_lock_first(client):
    before = _ungated()
    h = await _auth(client)
    bank = await _create_bank(client, h)
    r = await client.patch(f"/accounting/bank-accounts/{bank['id']}", json={"bank_name": "Renamed"}, headers=h)
    assert r.status_code == 200, r.text
    recon = await _create_recon(client, h, bank["id"])
    sid = recon["id"]
    await _import_csv(client, h, sid, b"Date,Description,Amount\n2026-03-01,Fee,-500\n2026-03-02,Mixed,-1500\n"
                                      b"2026-03-03,Skip,-100\n")
    lines = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)).json()["items"]
    fee, mixed, skip = (line["id"] for line in sorted(lines, key=lambda line: line["line_date"]))
    r = await client.post(f"/accounting/reconciliation/{sid}/lines/{fee}/create",
                          json={"account_code": "6100", "memo": "Bank fee"}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/lines/{fee}/unmatch", headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/lines/{mixed}/split", json={"splits": [
        {"account_code": "5100", "amount": 1000, "memo": "Supplies"},
        {"account_code": "6300", "amount": 500, "memo": "Utilities"}]}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.patch(f"/accounting/reconciliation/{sid}/lines/{skip}", json={"status": "skipped"}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/auto-match", headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/bulk-confirm", headers=h)
    assert r.status_code == 200, r.text
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
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code in (200, 409, 422), r.text
    assert _ungated() - before == set()
