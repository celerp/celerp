# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Bank statements end to end through the API: import, the group table, suggest,
resolve with each decision kind, transfers, document payments, unmatch, bank-wide
completion, removing a statement, the opening balance, Suspense and the migration's
data steps."""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest

import bank_statement_helpers as B

pytestmark = pytest.mark.asyncio


async def _co_bank(client, **kw):
    co = await B.company(client)
    bk = await B.bank(client, co["h"], **kw)
    return co, bk


async def _one_month(client, co, bk, rows, *, opening="0.00", closing=None, start="2026-01-01", end="2026-01-31"):
    if closing is None:
        closing = f"{Decimal(opening) + sum(Decimal(a) for _, _, a in rows):.2f}"
    return await B.statement(client, co, bk["id"], rows, opening=opening, closing=closing, start=start, end=end)


async def _post(client, co, bk, line, account="5100", **extra):
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "post",
                                                          "account_code": account, **extra}])
    assert r.status_code == 200, r.text
    return r.json()


async def _invoice(client, h, total, *, currency=None, rate=None, contact_id="contact:1"):
    body = {"doc_type": "invoice", "contact_id": contact_id, "issue_date": "2026-01-02",
            "line_items": [{"name": "Work", "quantity": 1, "unit_price": total, "line_total": total}],
            "subtotal": total, "tax": 0.0, "total": total}
    if currency:
        body.update(currency=currency, conversion_rate=rate)
    r = await client.post("/docs", json=body, headers=h)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", json={}, headers=h)
    assert r.status_code == 200, r.text
    return doc_id


async def _open_balance(client, h, doc_id):
    r = await client.get(f"/docs/{doc_id}", headers=h)
    return Decimal(str(r.json().get("amount_outstanding", r.json().get("balance_due"))))


# ── Brought forward and seeding ─────────────────────────────────────────────

async def test_second_statement_balances_without_rematching_first(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "SUPPLIER ONE", "-40.00")], opening="0.00")
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    await _post(client, co, bk, line)
    r = await client.post(f"{B.API}/bank/{bk['id']}/complete", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    await _one_month(client, co, bk, [("2026-02-03", "SUPPLIER TWO", "-10.00")], opening="-40.00",
                     start="2026-02-01", end="2026-02-28")
    line2 = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    await _post(client, co, bk, line2)
    r = await client.post(f"{B.API}/bank/{bk['id']}/complete", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    assert r.json()["completed"] and not r.json().get("stopped_at")


async def test_first_session_seeds_only_when_opening_ties(client):
    co, bk = await _co_bank(client, opening=500.0, opening_date="2025-12-01")
    # An entry before the statement: a transfer out on 2025-12-20.
    other = await B.bank(client, co["h"], name="Other")
    r = await client.post("/accounting/transfers", json={"from_bank_id": bk["id"], "to_bank_id": other["id"],
                                                           "amount": 100.0, "date": "2025-12-20",
                                                           "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    tied = await _one_month(client, co, bk, [("2026-01-05", "FEE", "-5.00")], opening="400.00")
    session_id = tied["sessions"][0]["id"]
    rec = (await client.get(f"{B.API}/{session_id}", headers=co["h"])).json()
    assert r.json()["je_id"] in rec["reconciled_je_ids"]


async def test_first_session_untied_seeds_nothing_and_says_why(client):
    co2, bk2 = await _co_bank(client, opening=500.0, opening_date="2025-12-01")
    other2 = await B.bank(client, co2["h"], name="Other")
    await client.post("/accounting/transfers", json={"from_bank_id": bk2["id"], "to_bank_id": other2["id"],
                                                      "amount": 100.0, "date": "2025-12-20",
                                                      "idempotency_key": B.key()}, headers=co2["h"])
    untied = await _one_month(client, co2, bk2, [("2026-01-05", "FEE", "-5.00")], opening="450.00")
    rec2 = (await client.get(f"{B.API}/{untied['sessions'][0]['id']}", headers=co2["h"])).json()
    assert rec2["reconciled_je_ids"] == []
    table = await B.groups(client, co2, bk2["id"])
    assert "Book balance before 2026-01-01 is 400.00, statement opens at 450.00" in table["difference"]["notes"]


# ── Suggest ─────────────────────────────────────────────────────────────────

async def test_suggest_never_matches(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "ACME LTD", "-40.00"), ("2026-01-09", "ACME LTD", "40.00")])
    r = await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    lines = B.all_lines(await B.groups(client, co, bk["id"]))
    assert all(l["status"] == "unmatched" and l["matched_je_ids"] == [] for l in lines)
    assert all(l["suggestion"] for l in lines)


async def test_bank_reversal_pair_no_entry(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "ACME LTD", "-40.00"), ("2026-01-07", "ACME LTD", "40.00")])
    await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    kinds = {l["suggestion"]["kind"] for l in B.all_lines(await B.groups(client, co, bk["id"]))}
    assert kinds == {"no_entry"}


async def test_suggest_without_model_states_why(client, monkeypatch):
    from celerp_accounting import suggest
    monkeypatch.setattr(suggest, "model_rung", None)  # no credits: the model cannot run
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "SOMETHING NEW CO", "-40.00")])
    r = await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    assert r.json()["model_ran"] is False and r.json()["summary"]


# ── Groups and accept ───────────────────────────────────────────────────────

async def test_groups_span_every_open_statement_of_the_bank(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "GRAB TRIP", "-10.00")])
    await _one_month(client, co, bk, [("2026-02-05", "GRAB TRIP", "-12.00")], opening="-10.00",
                     start="2026-02-01", end="2026-02-28")
    table = await B.groups(client, co, bk["id"])
    grab = next(g for g in table["groups"] if g["key"] == "GRAB TRIP")
    assert grab["count"] == 2 and len({l["statement_id"] for l in grab["lines"]}) == 2


async def test_accept_posts_only_ticked_groups_and_lines(client):
    co, bk = await _co_bank(client)
    rows = [("2026-01-0%d" % d, "NETFLIX.COM", "-15.99") for d in (2, 3, 4)] + [("2026-01-06", "SPOTIFY P", "-9.99")]
    await _one_month(client, co, bk, rows)
    for pattern in ("NETFLIX", "SPOTIFY"):
        r = await client.post("/accounting/rules", json={"match_pattern": pattern, "target_account_code": "5100",
                                                         "idempotency_key": B.key()}, headers=co["h"])
        assert r.status_code == 200, r.text
    await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    table = await B.groups(client, co, bk["id"])
    netflix = next(g for g in table["groups"] if g["key"].startswith("NETFLIX"))
    keep = [l["id"] for l in netflix["lines"][:2]]
    r = await B.resolve(client, co, bk["id"], accept={"group_keys": [netflix["key"]], "line_ids": keep})
    assert r.status_code == 200, r.text
    after = {l["id"]: l["status"] for l in B.all_lines(await B.groups(client, co, bk["id"], view="all"))}
    assert [after[i] for i in keep] == ["posted", "posted"]
    assert after[netflix["lines"][2]["id"]] == "unmatched"
    assert sum(1 for s in after.values() if s == "posted") == 2


async def test_ai_guess_and_sparse_groups_start_unticked(client, monkeypatch):
    from celerp_accounting import suggest
    monkeypatch.setattr(suggest, "model_rung", lambda groups, accounts: {
        g["key"]: {"account_code": "5100", "contact_id": None, "reason": "guess"} for g in groups})
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "SOMETHING NEW CO", "-40.00"), ("2026-01-06", "WITHDRAWAL", "-20.00")])
    await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    table = await B.groups(client, co, bk["id"])
    assert table["groups"] and all(g["ticked"] is False for g in table["groups"])


# ── Resolve ─────────────────────────────────────────────────────────────────

async def test_split_decision_through_resolve_equals_old_split(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "MIXED PURCHASE", "-100.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "post", "splits": [
        {"account_code": "5100", "amount": 60.0}, {"account_code": "5200", "amount": 40.0}]}])
    assert r.status_code == 200, r.text
    je_id = r.json()["results"][0]["je_ids"][0]
    assert je_id == f"je:recon:split:{line['id']}"
    je = await B.journal(client, co["h"], je_id)
    debits = sorted((e["account"], e["debit"]) for e in je["entries"] if e.get("debit"))
    assert debits == [("5100", 60.0), ("5200", 40.0)] and je["je_type"] == "recon_split"


async def test_resolve_replay_after_undo_does_not_post(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "FEE", "-5.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    idem = B.key()
    dec = [{"line_ids": [line["id"]], "kind": "post", "account_code": "5100"}]
    first = await B.resolve(client, co, bk["id"], decisions=dec, idem=idem)
    assert first.status_code == 200
    r = await client.post(f"{B.API}/bank/{bk['id']}/unmatch", json={"line_ids": [line["id"]], "idempotency_key": B.key()},
                          headers=co["h"])
    assert r.status_code == 200, r.text
    again = await B.resolve(client, co, bk["id"], decisions=dec, idem=idem)
    assert again.status_code == 200 and again.json() == first.json()
    assert B.all_lines(await B.groups(client, co, bk["id"]))[0]["status"] == "unmatched"


async def test_remember_replay_writes_one_rule(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "TRANSFER TO 1234567890 SOMCHAI", "-500.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    idem = B.key()
    dec = [{"line_ids": [line["id"]], "kind": "post", "account_code": "5100", "remember": True}]
    for _ in range(2):
        assert (await B.resolve(client, co, bk["id"], decisions=dec, idem=idem)).status_code == 200
    rules = (await client.get("/accounting/rules", headers=co["h"])).json()
    rules = rules.get("items", rules)
    assert len([r for r in rules if r["origin"] == "answer"]) == 1
    assert rules[0]["match_field"] == "counterparty_account" and rules[0]["bank_account_id"] is None


async def test_one_refused_line_rolls_back_alone(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "A", "-5.00"), ("2026-01-06", "B", "-6.00")])
    a, b = sorted(B.all_lines(await B.groups(client, co, bk["id"])), key=lambda l: l["line_date"])
    r = await B.resolve(client, co, bk["id"], decisions=[
        {"line_ids": [a["id"]], "kind": "post", "account_code": "NOPE"},
        {"line_ids": [b["id"]], "kind": "post", "account_code": "5100"}])
    assert r.status_code == 200, r.text
    res = {x["line_id"]: x for x in r.json()["results"]}
    assert res[a["id"]]["ok"] is False and res[a["id"]]["reason"]
    assert res[b["id"]]["ok"] is True


async def test_locked_line_reports_engine_refusal(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "FEE", "-5.00")])
    r = await client.post("/accounting/period-lock", json={"lock_date": "2026-01-31"}, headers=co["h"])
    assert r.status_code == 200, r.text
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "post", "account_code": "5100"}])
    res = r.json()["results"][0]
    from celerp.events.engine import period_locked_refusal
    from ui.i18n import refusal_text
    assert res["ok"] is False and res["reason"] == refusal_text(period_locked_refusal("2026-01-31"))


async def test_resolve_across_statements_respects_each_session_guard(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "FEE", "-5.00"), ("2026-01-06", "CHARGE", "-7.00"),
                                      ("2026-01-07", "CHARGE REVERSAL", "7.00")])
    jan = sorted(B.all_lines(await B.groups(client, co, bk["id"])), key=lambda l: l["line_date"])
    await _post(client, co, bk, jan[0])
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [jan[1]["id"], jan[2]["id"]], "kind": "no_entry"}])
    assert r.status_code == 200
    assert (await client.post(f"{B.API}/bank/{bk['id']}/complete", json={"idempotency_key": B.key()},
                              headers=co["h"])).json()["completed"]
    await _one_month(client, co, bk, [("2026-02-05", "FEE", "-5.00")], opening="-5.00", start="2026-02-01", end="2026-02-28")
    feb = [l for l in B.all_lines(await B.groups(client, co, bk["id"])) if l["line_date"].startswith("2026-02")]
    r = await B.resolve(client, co, bk["id"], decisions=[
        {"line_ids": [jan[0]["id"]], "kind": "post", "account_code": "5200"},
        {"line_ids": [feb[0]["id"]], "kind": "post", "account_code": "5100"}])
    res = {x["line_id"]: x for x in r.json()["results"]}
    assert res[jan[0]["id"]]["ok"] is False and res[feb[0]["id"]]["ok"] is True


async def test_foreign_bank_answer_requires_rate(client):
    co, bk = await _co_bank(client, currency="EUR")
    await _one_month(client, co, bk, [("2026-01-05", "FEE", "-5.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "post", "account_code": "5100"}])
    assert r.json()["results"][0]["ok"] is False
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "post",
                                                          "account_code": "5100", "rate": 1.1}])
    assert r.json()["results"][0]["ok"] is True


async def test_one_deposit_matches_three_payments(client):
    co, bk = await _co_bank(client)
    jes = []
    for d in ("2026-01-02", "2026-01-03", "2026-01-04"):
        r = await client.post("/accounting/journal-entries", json={"memo": "receipt", "ts": d, "idempotency_token": B.key(), "entries": [
            {"account": bk["chart_account_code"], "debit": 100.0, "credit": 0},
            {"account": "4100", "debit": 0, "credit": 100.0}]}, headers=co["h"])
        assert r.status_code == 200, r.text
        jes.append(r.json()["je_id"])
    await _one_month(client, co, bk, [("2026-01-05", "DEPOSIT BATCH", "300.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "match", "je_ids": jes}])
    assert r.json()["results"][0]["ok"] is True
    line = B.all_lines(await B.groups(client, co, bk["id"], view="all"))[0]
    assert sorted(line["matched_je_ids"]) == sorted(jes)


async def test_two_lines_match_one_entry(client):
    co, bk = await _co_bank(client)
    r = await client.post("/accounting/journal-entries", json={"memo": "receipt", "ts": "2026-01-02", "idempotency_token": B.key(), "entries": [
        {"account": bk["chart_account_code"], "debit": 300.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 300.0}]}, headers=co["h"])
    je = r.json()["je_id"]
    await _one_month(client, co, bk, [("2026-01-05", "PART A", "100.00"), ("2026-01-06", "PART B", "200.00")])
    lines = B.all_lines(await B.groups(client, co, bk["id"]))
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [l["id"] for l in lines], "kind": "match", "je_ids": [je]}])
    assert all(x["ok"] for x in r.json()["results"])
    bad = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [lines[0]["id"]], "kind": "match", "je_ids": [je]}])
    assert bad.json()["results"][0]["ok"] is False


async def test_fingerprint_refuses_overlapping_lines(client):
    co, bk = await _co_bank(client)
    rows = [("2026-01-05", "FEE", "-5.00"), ("2026-01-20", "WIRE", "50.00")]
    await _one_month(client, co, bk, rows)
    overlap = rows[1:] + [("2026-02-03", "FEE", "-5.00")]
    preview = await B.read(client, co, [(B.csv_bytes(overlap), "feb.csv", "text/csv")], bank_id=bk["id"])
    first = preview["statements"][0]["lines"][0]
    assert first["duplicate"] and first["ticked"] is False
    out = await B.import_statement(client, co, preview, bank_id=bk["id"], header={
        "opening_balance": "45.00", "closing_balance": "40.00", "period_start": "2026-01-20", "period_end": "2026-02-28"})
    assert out["sessions"][0]["skipped_duplicates"] == 1
    lines = B.all_lines(await B.groups(client, co, bk["id"], view="all"))
    assert sum(1 for l in lines if l["description"] == "WIRE") == 1


async def test_saved_column_map_reused_for_identical_headers(client):
    co, bk = await _co_bank(client)
    content = b"Posted,Narrative,Value\n2026-01-05,FEE,-5.00\n"
    preview = await B.read(client, co, [(content, "a.csv", "text/csv")], bank_id=bk["id"])
    assert preview["needs_mapping"]
    preview["column_map"] = {"date": "Posted", "description": "Narrative", "amount": "Value"}
    r = await client.post(f"{B.API}/import-statement", json={
        "job_id": preview["job_id"], "bank_id": bk["id"], "column_map": preview["column_map"],
        "idempotency_key": B.key(), "statements": [{"header": {
            "opening_balance": "0.00", "closing_balance": "-5.00", "period_start": "2026-01-01",
            "period_end": "2026-01-31", "currency": "USD"}, "lines": None}]}, headers=co["h"])
    assert r.status_code == 200, r.text
    again = await B.read(client, co, [(b"Posted,Narrative,Value\n2026-02-05,FEE,-6.00\n", "b.csv", "text/csv")], bank_id=bk["id"])
    assert again["needs_mapping"] is False and again["layout_saved"] is True
    assert again["statements"][0]["lines"][0]["amount"] == "-6.00"


# ── Transfers ───────────────────────────────────────────────────────────────

async def test_transfer_pair_posts_once_from_either_side(client):
    co, a = await _co_bank(client, name="A")
    b = await B.bank(client, co["h"], name="B")
    await _one_month(client, co, a, [("2026-01-05", "TRANSFER TO SAVINGS", "-100.00")])
    await _one_month(client, co, b, [("2026-01-06", "TRANSFER FROM CURRENT", "100.00")])
    la = B.all_lines(await B.groups(client, co, a["id"]))[0]
    lb = B.all_lines(await B.groups(client, co, b["id"]))[0]
    r = await B.resolve(client, co, b["id"], decisions=[{"line_ids": [lb["id"]], "kind": "transfer",
                                                          "target_bank_id": a["id"]}])
    assert r.json()["results"][0]["ok"], r.text
    je = r.json()["results"][0]["je_ids"][0]
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer",
                                                          "target_bank_id": b["id"]}])
    assert r.json()["results"][0]["je_ids"] == [je]
    journal = (await client.get("/accounting/journal", headers=co["h"])).json()["entries"]
    assert len([e for e in journal if e.get("je_type") == "recon_transfer"]) == 1


async def test_transfer_to_foreign_bank_holds_its_currency(client):
    co, a = await _co_bank(client, name="A")
    eur = await B.bank(client, co["h"], name="EUR", currency="EUR")
    await _one_month(client, co, a, [("2026-01-05", "TRANSFER TO EUR", "-110.00")])
    la = B.all_lines(await B.groups(client, co, a["id"]))[0]
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer",
                                                          "target_bank_id": eur["id"], "to_amount": 100.0}])
    je = await B.journal(client, co["h"], r.json()["results"][0]["je_ids"][0])
    dest = next(e for e in je["entries"] if e["account"] == eur["chart_account_code"])
    assert dest["currency"] == "EUR" and Decimal(str(dest["amount"])) == Decimal("100")


async def test_undo_transfer_unmatches_paired_line(client):
    co, a = await _co_bank(client, name="A")
    b = await B.bank(client, co["h"], name="B")
    await _one_month(client, co, a, [("2026-01-05", "TRANSFER TO SAVINGS", "-100.00")])
    await _one_month(client, co, b, [("2026-01-06", "TRANSFER FROM CURRENT", "100.00")])
    la = B.all_lines(await B.groups(client, co, a["id"]))[0]
    lb = B.all_lines(await B.groups(client, co, b["id"]))[0]
    await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer", "target_bank_id": b["id"]}])
    assert B.all_lines(await B.groups(client, co, b["id"], view="all"))[0]["status"] == "matched"
    r = await client.post(f"{B.API}/bank/{a['id']}/unmatch", json={"line_ids": [la["id"]], "idempotency_key": B.key()},
                          headers=co["h"])
    assert r.status_code == 200, r.text
    assert B.all_lines(await B.groups(client, co, b["id"], view="all"))[0]["status"] == "unmatched"
    assert lb["id"]


async def test_transfer_retry_posts_once(client):
    co, a = await _co_bank(client, name="A")
    b = await B.bank(client, co["h"], name="B")
    body = {"from_bank_id": a["id"], "to_bank_id": b["id"], "amount": 10.0, "date": "2026-01-05", "idempotency_key": B.key()}
    r1 = await client.post("/accounting/transfers", json=body, headers=co["h"])
    r2 = await client.post("/accounting/transfers", json=body, headers=co["h"])
    assert r1.status_code == r2.status_code == 200 and r1.json()["je_id"] == r2.json()["je_id"]
    same = await client.post("/accounting/transfers", json={**body, "to_bank_id": a["id"], "idempotency_key": B.key()},
                             headers=co["h"])
    assert same.status_code in (409, 422)


async def test_cash_withdrawal_goes_to_cash_account(client):
    co, a = await _co_bank(client)
    cash = await B.bank(client, co["h"], name="Petty cash", bank_type="cash")
    await _one_month(client, co, a, [("2026-01-05", "ATM WITHDRAWAL", "-200.00")])
    line = B.all_lines(await B.groups(client, co, a["id"]))[0]
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [line["id"]], "kind": "cash"}])
    assert r.json()["results"][0]["ok"], r.text
    assert (await B.bank_row(client, co["h"], cash["id"]))["balance"] == 200.0


# ── Document payments ───────────────────────────────────────────────────────

async def test_pay_doc_refuses_over_open_balance(client):
    co, bk = await _co_bank(client)
    inv = await _invoice(client, co["h"], 100.0)
    await _one_month(client, co, bk, [("2026-01-05", "ACME LTD", "150.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "pay_doc",
                                                          "docs": [{"doc_id": inv, "amount": 150.0}]}])
    res = r.json()["results"][0]
    assert res["ok"] is False and res["reason"]
    assert await _open_balance(client, co["h"], inv) == Decimal("100")


async def test_pay_doc_with_fee_part_equals_line(client):
    co, bk = await _co_bank(client)
    inv = await _invoice(client, co["h"], 100.0)
    await _one_month(client, co, bk, [("2026-01-05", "ACME LTD", "97.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    bad = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "pay_doc",
                                                            "docs": [{"doc_id": inv, "amount": 100.0}]}])
    assert bad.json()["results"][0]["ok"] is False
    good = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "pay_doc",
                                                             "docs": [{"doc_id": inv, "amount": 100.0}],
                                                             "parts": [{"account_code": "5100", "amount": -3.0}]}])
    assert good.json()["results"][0]["ok"], good.text
    assert await _open_balance(client, co["h"], inv) == Decimal("0")


async def test_pay_doc_needs_record_payments(client, session):
    co, bk = await _co_bank(client)
    inv = await _invoice(client, co["h"], 100.0)
    await _one_month(client, co, bk, [("2026-01-05", "ACME LTD", "100.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    from test_helpers import grant_permission
    await grant_permission(client, co["h"], "record_payments", "admin")
    mgr = await B.member(client, session, co, "manager")
    r = await B.resolve(client, co, bk["id"], headers=mgr, decisions=[{"line_ids": [line["id"]], "kind": "pay_doc",
                                                                      "docs": [{"doc_id": inv, "amount": 100.0}]}])
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["ok"] is False and "Record payments" in res["reason"]
    assert await _open_balance(client, co["h"], inv) == Decimal("100")


# ── Unmatch, complete, delete ───────────────────────────────────────────────

async def test_bulk_unmatch_list_of_one_equals_old_unmatch(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "FEE", "-5.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    out = await _post(client, co, bk, line)
    je_id = out["results"][0]["je_ids"][0]
    r = await client.post(f"{B.API}/bank/{bk['id']}/unmatch", json={"line_ids": [line["id"]], "idempotency_key": B.key()},
                          headers=co["h"])
    assert r.status_code == 200, r.text
    after = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    assert after["status"] == "unmatched" and after["matched_je_ids"] == []
    assert (await B.journal(client, co["h"], je_id))["status"] == "void"


async def test_complete_balanced_stops_at_first_unbalanced(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "FEE", "-5.00")])
    await _one_month(client, co, bk, [("2026-02-05", "FEE", "-6.00")], opening="-5.00", start="2026-02-01", end="2026-02-28")
    jan = [l for l in B.all_lines(await B.groups(client, co, bk["id"])) if l["line_date"].startswith("2026-01")][0]
    await _post(client, co, bk, jan)
    r = await client.post(f"{B.API}/bank/{bk['id']}/complete", json={"idempotency_key": B.key()}, headers=co["h"])
    out = r.json()
    assert len(out["completed"]) == 1 and out["stopped_at"]["statement_date"] == "2026-02-28"
    assert out["stopped_at"]["reason"]


async def test_delete_statement_refused_with_posted_line(client):
    co, bk = await _co_bank(client)
    imp = await _one_month(client, co, bk, [("2026-01-05", "FEE", "-5.00")])
    sid = imp["sessions"][0]["id"]
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    await _post(client, co, bk, line)
    r = await client.request("DELETE", f"{B.API}/{sid}", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 409 and r.json()["detail"]
    await client.post(f"{B.API}/bank/{bk['id']}/unmatch", json={"line_ids": [line["id"]], "idempotency_key": B.key()},
                      headers=co["h"])
    r = await client.request("DELETE", f"{B.API}/{sid}", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text


# ── Opening, Suspense ───────────────────────────────────────────────────────

async def test_set_opening_once_then_adjustment(client):
    co, bk = await _co_bank(client)
    url = f"/accounting/bank-accounts/{bk['id']}/opening"
    r = await client.put(url, json={"amount": 500.0, "date": "2026-01-01", "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    first = r.json()["je_id"]
    r = await client.put(url, json={"amount": 520.0, "date": "2026-01-01", "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    adj = r.json()["je_id"]
    assert adj != first
    je = await B.journal(client, co["h"], adj)
    assert je["ts"].startswith("2026-01-01")
    bank_side = next(e for e in je["entries"] if e["account"] == bk["chart_account_code"])
    assert bank_side["debit"] == 20.0
    assert (await B.journal(client, co["h"], first))["status"] == "posted"
    await client.post("/accounting/period-lock", json={"lock_date": "2026-01-31"}, headers=co["h"])
    r = await client.put(url, json={"amount": 530.0, "date": "2026-01-01", "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code in (409, 422) and "2026-01-31" in r.text


async def test_reclassify_refused_in_locked_period(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "WITHDRAWAL", "-50.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "suspense"}])
    assert r.json()["results"][0]["ok"], r.text
    je = await B.journal(client, co["h"], r.json()["results"][0]["je_ids"][0])
    assert all("contact" not in e or not e["contact"] for e in je["entries"])
    await client.post("/accounting/period-lock", json={"lock_date": "2026-01-31"}, headers=co["h"])
    r = await client.post(f"{B.API}/lines/{line['id']}/reclassify", json={"account_code": "5100", "idempotency_key": B.key()},
                          headers=co["h"])
    assert r.status_code in (409, 422) and "2026-01-31" in r.text


async def test_reclassify_moves_suspense_to_answer(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "WITHDRAWAL", "-50.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "suspense"}])
    assert (await B.groups(client, co, bk["id"]))["suspense_count"] == 1
    r = await client.post(f"{B.API}/lines/{line['id']}/reclassify", json={"account_code": "5100", "idempotency_key": B.key()},
                          headers=co["h"])
    assert r.status_code == 200, r.text
    assert r.json()["je_id"] == f"je:recon:reclass:{line['id']}:1"
    assert (await B.groups(client, co, bk["id"]))["suspense_count"] == 0


# ── Rules ───────────────────────────────────────────────────────────────────

async def test_answer_rule_is_company_wide(client):
    co, a = await _co_bank(client, name="A")
    b = await B.bank(client, co["h"], name="B")
    await _one_month(client, co, a, [("2026-01-05", "TRANSFER TO 1234567890 SOMCHAI", "-500.00")])
    la = B.all_lines(await B.groups(client, co, a["id"]))[0]
    await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "post", "account_code": "5100",
                                                      "remember": True}])
    await _one_month(client, co, b, [("2026-01-09", "TRANSFER TO 1234567890 SOMCHAI", "-300.00")])
    await client.post(f"{B.API}/bank/{b['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    lb = B.all_lines(await B.groups(client, co, b["id"]))[0]
    assert lb["suggestion"]["source"] == "rule" and lb["suggestion"]["account_code"] == "5100"


async def test_sparse_rule_requires_amount(client):
    co, _bk = await _co_bank(client)
    body = {"match_field": "counterparty_key", "match_pattern": "SPARSE:WITHDRAWAL:out", "match_type": "exact",
            "target_account_code": "5100", "idempotency_key": B.key()}
    r = await client.post("/accounting/rules", json=body, headers=co["h"])
    assert r.status_code == 422
    r = await client.post("/accounting/rules", json={**body, "match_amount": 200.0}, headers=co["h"])
    assert r.status_code == 200, r.text


# ── Migration data steps ────────────────────────────────────────────────────

def _migration():
    import glob
    import os
    here = os.path.join(os.path.dirname(__file__), "..", "..", "..", "celerp", "migrations", "versions")
    name = next(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(here, "*_bank_statement_lines.py")))
    return importlib.import_module(f"celerp.migrations.versions.{name}")


async def test_fingerprint_backfill_never_collides():
    m = _migration()
    rows = [{"id": f"id{i}", "bank_account_id": "b1", "statement_date": "2026-01-31", "created_at": "t", "line_date": "2026-01-02",
             "amount": Decimal("-3.0000"), "description": "ATM FEE", "reference": None, "raw_balance": None} for i in range(4)]
    rows.append({**rows[0], "id": "x", "bank_account_id": "b2"})
    fps = m.backfill_fingerprints(rows)
    per_bank = {}
    for r in rows:
        per_bank.setdefault(r["bank_account_id"], []).append(fps[r["id"]])
    assert all(len(v) == len(set(v)) for v in per_bank.values())
    assert all(len(fp) == 16 for fp in fps.values())


async def test_suggested_rows_migrate_to_suggestion():
    m = _migration()
    row = {"status": "suggested", "matched_je_id": "je:7"}
    assert m.migrated_line(row) == {"status": "unmatched", "matched_je_ids": [],
                                    "suggestion": {"kind": "match", "je_ids": ["je:7"], "source": "match"}}
    assert m.migrated_line({"status": "matched", "matched_je_id": "je:8"}) == {
        "status": "matched", "matched_je_ids": ["je:8"], "suggestion": None}
    assert m.migrated_line({"status": "created", "matched_je_id": "je:9"})["status"] == "posted"
