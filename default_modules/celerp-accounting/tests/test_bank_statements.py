# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Bank statements end to end through the API: import, the group table, suggest,
resolve with each decision kind, transfers, document payments, unmatch, bank-wide
completion, removing a statement, the opening balance, Suspense and the migration's
data steps."""

from __future__ import annotations

from decimal import Decimal

import pytest

import bank_statement_helpers as B
from ui.i18n import refusal_text, t

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


async def test_complete_from_the_bank_page_completes_the_statement(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "SUPPLIER ONE", "-40.00")], opening="0.00")
    await _post(client, co, bk, B.all_lines(await B.groups(client, co, bk["id"]))[0])
    async with B.browser(co["h"]) as ui:
        r = await ui.post(f"/accounting/reconcile/bank/{bk['id']}/complete", data={"idempotency_key": B.key()})
    assert r.status_code == 200, r.text[:500]
    assert t("bank.completed", n=1) in r.text
    r = await client.post(f"{B.API}/bank/{bk['id']}/complete", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200 and not r.json()["completed"], r.text


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
    r = await B.resolve(client, co, bk["id"], accept=B.accept(table, [netflix["key"]], keep))
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

async def test_split_decision_through_resolve_equals_old_split(client, session):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "MIXED PURCHASE", "-100.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "post", "splits": [
        {"account_code": "5100", "amount": 60.0}, {"account_code": "5200", "amount": 40.0}]}])
    assert r.status_code == 200, r.text
    je_id = r.json()["results"][0]["je_ids"][0]
    assert je_id == f"je:recon:split:{line['id']}"
    je = await B.journal(session, co, je_id)
    debits = sorted((e["account"], e["debit"]) for e in je["entries"] if e.get("debit"))
    assert debits == [("5100", 60.0), ("5200", 40.0)] and je["je_type"] == "recon_split"


async def test_an_answered_line_names_the_accounts_its_entries_went_to(client):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "MIXED PURCHASE", "-100.00"), ("2026-01-06", "FEE", "-5.00")])
    lines = {l["description"]: l for l in B.all_lines(await B.groups(client, co, bk["id"]))}
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [lines["MIXED PURCHASE"]["id"]], "kind": "post",
                                                          "splits": [{"account_code": "5200", "amount": 60.0},
                                                                     {"account_code": "5100", "amount": 40.0}]}])
    assert r.status_code == 200, r.text
    after = {l["description"]: l for l in B.all_lines(await B.groups(client, co, bk["id"], view="all"))}
    assert [a["code"] for a in after["MIXED PURCHASE"]["posted_to"]] == ["5100", "5200"]
    assert all(a["name"] and a["name"] != a["code"] for a in after["MIXED PURCHASE"]["posted_to"])
    assert after["FEE"]["posted_to"] == []


async def test_signed_split_amounts_are_refused_saying_to_drop_the_sign(client):
    from celerp_accounting.routes import _money_text
    from ui.i18n import t

    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "MIXED PURCHASE", "-100.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "post", "splits": [
        {"account_code": "5100", "amount": -60.0}, {"account_code": "5200", "amount": -40.0}]}])
    res = r.json()["results"][0]
    cur = bk.get("currency")
    assert res["ok"] is False and refusal_text(res["reason"]) == t(
        "acct.err_split_total", "en", total=_money_text(Decimal("-100"), cur), amount=_money_text(Decimal("100"), cur))
    assert "minus sign" in refusal_text(res["reason"])
    assert B.all_lines(await B.groups(client, co, bk["id"]))[0]["status"] == "unmatched"


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
    assert res["ok"] is False and refusal_text(res["reason"]) == refusal_text(period_locked_refusal("2026-01-31"))


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
        "opening_balance": "-5.00", "closing_balance": "40.00", "period_start": "2026-01-20", "period_end": "2026-02-28"})
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


async def test_a_tabular_file_that_would_lose_values_says_why_and_reads_nothing(client):
    """Two columns with one name would drop a value: the file is a failed step with the
    shared table reader's refusal, never a statement."""
    from ui.i18n import t
    co, bk = await _co_bank(client)
    preview = await B.read(client, co, [(b"Date,Description,Amount,Amount\n2026-01-01,x,-5000,123\n", "d.csv",
                                         "text/csv")], bank_id=bk["id"])
    assert preview["statements"] == []
    (step,) = preview["failed_steps"]
    assert step["filename"] == "d.csv" and step["retry"] is False
    assert step["reason"] == t("import.err_header_duplicate", first="C", column="D", header=repr("Amount"))


async def test_a_file_with_a_header_and_no_rows_is_named_as_holding_no_transactions(client):
    """A file with nothing to import is reported as that file, never shown as an empty statement."""
    from ui.i18n import t
    co, bk = await _co_bank(client)
    preview = await B.read(client, co, [(b"Date,Description,Amount\n", "e.csv", "text/csv")], bank_id=bk["id"])
    assert preview["statements"] == []
    (failed,) = preview["failed_steps"]
    assert failed["reason"] == t("acct.err_statement_no_lines", filename="e.csv") and failed["retry"] is False


async def test_import_refuses_a_column_map_naming_a_column_not_in_the_file(client):
    co, bk = await _co_bank(client)
    preview = await B.read(client, co, [(b"Posted,Narrative,Value\n2026-01-05,FEE,-5.00\n", "a.csv", "text/csv")],
                           bank_id=bk["id"])
    preview["column_map"] = {"date": "Posted", "description": "Other", "amount": "Value"}
    out = await B.import_statement(client, co, preview, bank_id=bk["id"], expect=422, header={
        "opening_balance": "0.00", "closing_balance": "-5.00", "period_start": "2026-01-01",
        "period_end": "2026-01-31"})
    assert refusal_text(out["detail"]) == t("acct.err_column_missing", column="Other")
    assert B.all_lines(await B.groups(client, co, bk["id"], view="all")) == []


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
    stop = r.json()["results"][0]
    assert not stop["ok"] and stop["candidates"] == [je], stop  # no identifier: the user confirms it
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "match", "je_ids": [je]}])
    assert r.json()["results"][0]["je_ids"] == [je]
    journal = (await client.get("/accounting/journal", headers=co["h"])).json()["entries"]
    assert len([e for e in journal if e.get("je_type") == "recon_transfer"]) == 1


async def test_transfer_to_its_own_bank_says_to_pick_another(client):
    from ui.i18n import t

    co, a = await _co_bank(client, name="A")
    await _one_month(client, co, a, [("2026-01-05", "TRANSFER", "-100.00")])
    line = B.all_lines(await B.groups(client, co, a["id"]))[0]
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [line["id"]], "kind": "transfer",
                                                          "target_bank_id": a["id"]}])
    res = r.json()["results"][0]
    assert res["ok"] is False and refusal_text(res["reason"]) == t("acct.err_transfer_same_bank", "en")
    assert B.all_lines(await B.groups(client, co, a["id"]))[0]["status"] == "unmatched"


async def test_transfer_to_foreign_bank_holds_its_currency(client, session):
    co, a = await _co_bank(client, name="A")
    eur = await B.bank(client, co["h"], name="EUR", currency="EUR")
    await _one_month(client, co, a, [("2026-01-05", "TRANSFER TO EUR", "-110.00")])
    la = B.all_lines(await B.groups(client, co, a["id"]))[0]
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer",
                                                          "target_bank_id": eur["id"], "to_amount": 100.0}])
    je = await B.journal(session, co, r.json()["results"][0]["je_ids"][0])
    dest = next(e for e in je["entries"] if e["account"] == eur["chart_account_code"])
    assert dest["fx_currency"] == "EUR" and Decimal(str(dest["fx_debit"])) == Decimal("100")


async def test_undo_transfer_unmatches_paired_line(client):
    co, a = await _co_bank(client, name="A")
    b = await B.bank(client, co["h"], name="B")
    await _one_month(client, co, a, [("2026-01-05", "TRANSFER TO SAVINGS", "-100.00")])
    await _one_month(client, co, b, [("2026-01-06", "TRANSFER FROM CURRENT", "100.00")])
    la = B.all_lines(await B.groups(client, co, a["id"]))[0]
    lb = B.all_lines(await B.groups(client, co, b["id"]))[0]
    await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer", "target_bank_id": b["id"],
                                                     "paired_line_id": lb["id"]}])
    assert B.all_lines(await B.groups(client, co, b["id"], view="all"))[0]["status"] == "matched"
    r = await client.post(f"{B.API}/bank/{a['id']}/unmatch", json={"line_ids": [la["id"]], "idempotency_key": B.key()},
                          headers=co["h"])
    assert r.status_code == 200, r.text
    assert B.all_lines(await B.groups(client, co, b["id"], view="all"))[0]["status"] == "unmatched"
    assert lb["id"]


async def _transfer_banks(client, b_rows):
    co, a = await _co_bank(client, name="A")
    b = await B.bank(client, co["h"], name="B")
    await _one_month(client, co, a, [("2026-01-05", "TRANSFER TO SAVINGS", "-100.00")])
    await _one_month(client, co, b, b_rows)
    return co, a, b, B.all_lines(await B.groups(client, co, a["id"]))[0], \
        B.all_lines(await B.groups(client, co, b["id"]))


async def _transfer_entries(client, co) -> int:
    journal = (await client.get("/accounting/journal", headers=co["h"])).json()["entries"]
    return len([e for e in journal if e.get("je_type") == "recon_transfer" and e.get("status") != "void"])


async def test_transfer_with_two_counterparts_settles_neither(client):
    """Two lines on the other bank could be the counterpart: the transfer posts, neither is
    settled, the result names no pair and the suggestion names none either."""
    co, a, b, la, lbs = await _transfer_banks(client, [("2026-01-04", "TRANSFER FROM CURRENT", "100.00"),
                                                        ("2026-01-06", "REFUND ACME", "100.00")])
    await client.post(f"{B.API}/bank/{a['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    shown = B.all_lines(await B.groups(client, co, a["id"]))[0]["suggestion"]
    assert shown["kind"] == "transfer" and "paired_line_id" not in shown
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer",
                                                          "target_bank_id": b["id"]}])
    row = r.json()["results"][0]
    assert row["ok"] and row["paired_line_id"] is None, r.text
    assert {l["status"] for l in B.all_lines(await B.groups(client, co, b["id"], view="all"))} == {"unmatched"}


async def test_transfer_names_the_counterpart_it_settles(client):
    co, a, b, la, lbs = await _transfer_banks(client, [("2026-01-04", "TRANSFER FROM CURRENT", "100.00"),
                                                        ("2026-01-06", "REFUND ACME", "100.00")])
    want = next(l for l in lbs if l["description"] == "REFUND ACME")
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer",
                                                          "target_bank_id": b["id"], "paired_line_id": want["id"]}])
    assert r.json()["results"][0]["paired_line_id"] == want["id"], r.text
    after = {l["id"]: l["status"] for l in B.all_lines(await B.groups(client, co, b["id"], view="all"))}
    assert after == {l["id"]: "matched" if l["id"] == want["id"] else "unmatched" for l in lbs}


async def test_transfer_whose_named_counterpart_was_settled_meanwhile_posts_nothing(client):
    co, a, b, la, (lb,) = await _transfer_banks(client, [("2026-01-06", "TRANSFER FROM CURRENT", "100.00")])
    r = await B.resolve(client, co, b["id"], decisions=[{"line_ids": [lb["id"]], "kind": "no_entry"}])
    assert r.json()["results"][0]["ok"], r.text
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer",
                                                          "target_bank_id": b["id"], "paired_line_id": lb["id"]}])
    row = r.json()["results"][0]
    assert row["ok"] is False and refusal_text(row["reason"]) == t("reconciliation.line_not_open", "en"), r.text
    assert B.all_lines(await B.groups(client, co, a["id"]))[0]["status"] == "unmatched"
    assert await _transfer_entries(client, co) == 0


async def test_accept_refuses_a_line_whose_suggestion_changed_since_it_was_shown(client):
    """The accept names each line's suggestion version; a line whose suggestion changed
    since is refused and nothing posts. A line without its version is not accepted at all."""
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "NETFLIX.COM", "-15.00")])
    r = await client.post("/accounting/rules", json={"match_pattern": "NETFLIX", "target_account_code": "5100",
                                                     "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    table = await B.groups(client, co, bk["id"])
    accept = B.accept(table, [table["groups"][0]["key"]])
    stale = {**accept, "versions": {i: "0" * 16 for i in accept["line_ids"]}}
    r = await B.resolve(client, co, bk["id"], accept=stale)
    row = r.json()["results"][0] if r.status_code == 200 else None
    assert r.status_code == 409 or (row and not row["ok"]), r.text
    r = await B.resolve(client, co, bk["id"], accept={**accept, "versions": {}})
    assert r.status_code == 422, r.text
    assert {l["status"] for l in B.all_lines(await B.groups(client, co, bk["id"], view="all"))} == {"unmatched"}
    r = await B.resolve(client, co, bk["id"], accept=accept)
    assert r.json()["refused"] == 0, r.text


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
    assert res["ok"] is False and "Record payments" in refusal_text(res["reason"])
    assert await _open_balance(client, co["h"], inv) == Decimal("100")


def _account_total(projections: dict, account: str) -> Decimal:
    """Debits less credits on ``account`` over the posted entries."""
    return sum((Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
                for s in projections.values() if s.get("entries") and s.get("status") == "posted"
                for e in s["entries"] if e.get("account") == account), Decimal(0))


async def test_undo_of_a_document_payment_line_restores_everything(client, session):
    """A line that paid an invoice (with a fee taken out) is undone: the payment is deleted
    with its index, its entry and the fee entry are voided together, and the invoice, the
    ledger, the bank account and the line are as before; repeating the first request then
    answers its first result and a new payment works."""
    co, bk = await _co_bank(client)
    inv = await _invoice(client, co["h"], 100.0)
    await _one_month(client, co, bk, [("2026-01-05", "ACME LTD", "97.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    bank_code = bk["chart_account_code"]
    before = await _projections(session, co)
    pay = {"line_ids": [line["id"]], "kind": "pay_doc", "docs": [{"doc_id": inv, "amount": 100.0}],
           "parts": [{"account_code": "5100", "amount": -3.0}]}
    idem = B.key()
    first = await B.resolve(client, co, bk["id"], decisions=[pay], idem=idem)
    assert first.json()["results"][0]["ok"], first.text
    paid = await _projections(session, co)
    assert _account_total(paid, bank_code) == Decimal("97")
    r = await client.post(f"{B.API}/bank/{bk['id']}/unmatch", json={"line_ids": [line["id"]],
                                                                     "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    after = await _projections(session, co)
    assert await _open_balance(client, co["h"], inv) == Decimal("100")
    payments = after[inv].get("payments") or []
    assert [(p.get("index"), p.get("status")) for p in payments] == [(1, "deleted")] or \
        all(p.get("status") != "active" for p in payments), payments
    assert {e for e, s in after.items() if s.get("status") == "void"} >= {
        e for e in paid if e not in before and e.startswith("je:")}
    for account in {e.get("account") for s in paid.values() for e in s.get("entries") or []}:
        assert _account_total(after, account) == _account_total(before, account), account
    assert B.all_lines(await B.groups(client, co, bk["id"]))[0]["status"] == "unmatched"
    assert await _held(session, co, bk["id"]) == (set(), set())
    again = await B.resolve(client, co, bk["id"], decisions=[pay], idem=idem)
    assert again.json() == first.json()
    assert await _open_balance(client, co["h"], inv) == Decimal("100")
    fresh = await B.resolve(client, co, bk["id"], decisions=[pay])
    assert fresh.json()["results"][0]["ok"], fresh.text
    assert await _open_balance(client, co["h"], inv) == Decimal("0")
    assert _account_total(await _projections(session, co), bank_code) == Decimal("97")


async def test_undo_of_a_document_payment_line_needs_record_payments(client, session):
    """Undoing a payment is held to the permission that records one: a member who keeps the
    books but may not record payments is refused, and nothing changes."""
    co, bk = await _co_bank(client)
    inv = await _invoice(client, co["h"], 100.0)
    await _one_month(client, co, bk, [("2026-01-05", "ACME LTD", "100.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "pay_doc",
                                                          "docs": [{"doc_id": inv, "amount": 100.0}]}])
    assert r.json()["results"][0]["ok"], r.text
    from test_helpers import grant_permission
    await grant_permission(client, co["h"], "record_payments", "admin")
    mgr = await B.member(client, session, co, "manager")
    r = await client.post(f"{B.API}/bank/{bk['id']}/unmatch", json={"line_ids": [line["id"]],
                                                                     "idempotency_key": B.key()}, headers=mgr)
    assert r.status_code == 403, r.text
    assert await _open_balance(client, co["h"], inv) == Decimal("0")
    assert B.all_lines(await B.groups(client, co, bk["id"], view="all"))[0]["status"] == "posted"


# ── Unmatch, complete, delete ───────────────────────────────────────────────

async def test_bulk_unmatch_list_of_one_equals_old_unmatch(client, session):
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
    assert (await B.journal(session, co, je_id))["status"] == "void"


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

async def test_set_opening_once_then_adjustment(client, session):
    co, bk = await _co_bank(client)
    url = f"/accounting/bank-accounts/{bk['id']}/opening"
    r = await client.put(url, json={"amount": 500.0, "date": "2026-01-01", "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    first = r.json()["je_id"]
    r = await client.put(url, json={"amount": 520.0, "date": "2026-01-01", "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    adj = r.json()["je_id"]
    assert adj != first
    je = await B.journal(session, co, adj)
    assert je["ts"].startswith("2026-01-01")
    bank_side = next(e for e in je["entries"] if e["account"] == bk["chart_account_code"])
    assert bank_side["debit"] == 20.0
    assert (await B.journal(session, co, first))["status"] == "posted"
    await client.post("/accounting/period-lock", json={"lock_date": "2026-01-31"}, headers=co["h"])
    r = await client.put(url, json={"amount": 530.0, "date": "2026-01-01", "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code in (409, 422) and "2026-01-31" in r.text


async def test_reclassify_refused_in_locked_period(client, session):
    co, bk = await _co_bank(client)
    await _one_month(client, co, bk, [("2026-01-05", "WITHDRAWAL", "-50.00")])
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "suspense"}])
    assert r.json()["results"][0]["ok"], r.text
    je = await B.journal(session, co, r.json()["results"][0]["je_ids"][0])
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

async def test_fingerprint_backfill_never_collides():
    """A restored older backup numbers identical lines of a bank by statement date, creation
    time and id, whatever order the backup holds them in, so no two lines of a bank collide and
    each gets the fingerprint the schema upgrade gives it. A line of the same size in the other
    direction (a refund of a charge) is a different line, numbered on its own."""
    from celerp.services import company_backup as cb
    from celerp.services.field_fingerprints import statement_line

    statements = [{"id": "s1", "bank_account_id": "b1", "statement_date": "2026-02-28"},
                  {"id": "s2", "bank_account_id": "b1", "statement_date": "2026-01-31"},
                  {"id": "s3", "bank_account_id": "b2", "statement_date": "2026-01-31"}]
    lines = [{"id": f"id{i}", "reconciliation_id": s, "created_at": f"t{9 - i}", "line_date": "2026-01-02",
              "amount": "\x00-3.0000\x00" if i % 2 else "\x003.0000\x00", "description": "ATM FEE" if i % 3 else " atm fee",
              "reference": None, "raw_balance": None, "status": "unmatched", "matched_je_id": None}
             for i, s in enumerate(("s1", "s2", "s1", "s3", "s2", "s1", "s3"))]
    tables = {"reconciliation_sessions": statements, "bank_statement_lines": lines}
    got = {r["id"]: (r["bank_account_id"], r["fingerprint"]) for r in cb._bank_lines(lambda t: iter(tables[t]))}
    # b1: s2 (Jan) lines id4, id1 by creation time, then s1 (Feb) lines id5, id2, id0; even ids are 3, odd -3.
    assert got == {i: (b, statement_line("2026-01-02", amount, "ATM FEE", None, None, n))
                   for b, amount, order in (("b1", "3.0000", ("id4", "id2", "id0")), ("b1", "-3.0000", ("id1", "id5")),
                                            ("b2", "3.0000", ("id6",)), ("b2", "-3.0000", ("id3",)))
                   for n, i in enumerate(order)}


async def test_suggested_rows_migrate_to_suggestion():
    from celerp.services import statement_line_upgrade as m
    row = {"status": "suggested", "matched_je_id": "je:7"}
    assert m.migrated_line(row) == {"status": "unmatched", "matched_je_ids": [],
                                    "suggestion": {"kind": "match", "je_ids": ["je:7"], "source": "match"}}
    assert m.migrated_line({"status": "matched", "matched_je_id": "je:8"}) == {
        "status": "matched", "matched_je_ids": ["je:8"], "suggestion": None}
    assert m.migrated_line({"status": "created", "matched_je_id": "je:9"})["status"] == "posted"


async def test_match_validates_the_book_entry(client):
    """Matching checks the entry itself: an unknown entry is refused, one entry cannot be
    held by two lines, a line already matched is not matched again, and Undo frees the
    entry for the other line."""
    from ui.i18n import t

    co, bk = await _co_bank(client)
    r = await client.post("/accounting/journal-entries", json={"memo": "wire", "ts": "2026-01-02", "idempotency_token": B.key(), "entries": [
        {"account": bk["chart_account_code"], "debit": 0, "credit": 1234.56},
        {"account": "5100", "debit": 1234.56, "credit": 0}]}, headers=co["h"])
    je = r.json()["je_id"]
    await _one_month(client, co, bk, [("2026-01-05", "WIRE A", "-1234.56"), ("2026-01-06", "WIRE B", "-1234.56")])
    first, second = sorted(B.all_lines(await B.groups(client, co, bk["id"])), key=lambda l: l["description"])

    async def match(line):
        r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "match", "je_ids": [je]}])
        assert r.status_code == 200, r.text
        return r.json()["results"][0]

    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [first["id"]], "kind": "match", "je_ids": ["je:does-not-exist"]}])
    assert r.json()["results"][0]["ok"] is False and r.json()["results"][0]["reason"]
    assert (await match(first))["ok"] is True
    held = await match(second)
    assert held["ok"] is False and refusal_text(held["reason"]) == t("acct.err_recon_book_entry_matched", "en")
    assert (await match(first))["ok"] is False
    line = B.line_by_text(await B.groups(client, co, bk["id"], view="all"), "WIRE A")
    assert line["matched_je_ids"] == [je]

    r = await client.post(f"/accounting/reconciliation/bank/{bk['id']}/unmatch", headers=co["h"],
                          json={"line_ids": [first["id"]], "idempotency_key": B.key()})
    assert r.status_code == 200, r.text
    assert (await match(second))["ok"] is True


async def _held(session, co, bank_id) -> tuple[set, set]:
    """What the statement holds and what its lines hold, as stored."""
    import uuid as _uuid

    from sqlalchemy import select

    from celerp_accounting.models import BankStatementLine, ReconciliationSession
    session.expire_all()
    cid, bid = _uuid.UUID(co["company_id"]), _uuid.UUID(bank_id)
    recon = (await session.execute(select(ReconciliationSession).where(
        ReconciliationSession.company_id == cid, ReconciliationSession.bank_account_id == bid))).scalar_one()
    on_lines = {je for ids in (await session.execute(select(BankStatementLine.matched_je_ids).where(
        BankStatementLine.reconciliation_id == recon.id))).scalars() for je in ids or []}
    return set(recon.reconciled_je_ids or []), on_lines


async def _projections(session, co) -> dict:
    import uuid as _uuid

    from sqlalchemy import select

    from celerp.models.projections import Projection
    session.expire_all()
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == _uuid.UUID(co["company_id"])))).all()
    return {e: s for e, s in rows}


@pytest.mark.parametrize("seed", range(6))
async def test_match_and_undo_sequences_keep_the_statement_equal_to_its_lines(client, session, seed):
    """Any order of matching lines (alone or several to one entry), posting them and undoing
    some of them leaves the statement holding exactly the entries its lines hold, and the
    records equal a replay of the ledger."""
    import random

    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company

    rng = random.Random(seed)
    co, bk = await _co_bank(client)
    amounts = ["-3.00", "-2.00", "-4.00", "-1.50"]
    await _one_month(client, co, bk, [(f"2026-01-0{n + 2}", f"LINE {n}", a) for n, a in enumerate(amounts)])
    lines = B.all_lines(await B.groups(client, co, bk["id"]))
    amount = {l["id"]: Decimal(str(l["amount"])) for l in lines}
    _opening, start = await _held(session, co, bk["id"])
    assert start == set()
    base = _opening
    for step in range(10):
        status = {l["id"]: l["status"] for l in B.all_lines(await B.groups(client, co, bk["id"], view="all"))}
        free = [i for i, s in status.items() if s == "unmatched"]
        done = [i for i, s in status.items() if s in ("matched", "posted")]
        action = rng.choice(["match", "post", "undo"] if free and done else ["match", "post"] if free else ["undo"])
        if action == "undo":
            picked = rng.sample(done, rng.randint(1, len(done)))
            r = await client.post(f"{B.API}/bank/{bk['id']}/unmatch",
                                  json={"line_ids": picked, "idempotency_key": B.key()}, headers=co["h"])
            assert r.status_code == 200, r.text
        elif action == "post":
            decision = {"line_ids": [rng.choice(free)], "kind": "post", "account_code": "5100"}
            row = (await B.resolve(client, co, bk["id"], decisions=[decision])).json()["results"][0]
            if row["candidates"]:  # an undone spend of the same money on another account: post anyway
                row = (await B.resolve(client, co, bk["id"], decisions=[
                    {**decision, "separate_from": row["candidates"]}])).json()["results"][0]
            assert row["ok"] is True, row
        else:
            picked = rng.sample(free, rng.randint(1, len(free)))
            total = -sum(amount[i] for i in picked)
            r = await client.post("/accounting/journal-entries", json={
                "memo": f"spend {step}", "ts": "2026-01-02", "idempotency_token": B.key(), "entries": [
                    {"account": bk["chart_account_code"], "debit": 0, "credit": float(total)},
                    {"account": "5300", "debit": float(total), "credit": 0}]}, headers=co["h"])
            assert r.status_code == 200, r.text
            r = await B.resolve(client, co, bk["id"], decisions=[
                {"line_ids": picked, "kind": "match", "je_ids": [r.json()["je_id"]]}])
            assert all(x["ok"] for x in r.json()["results"]), r.text
        held, on_lines = await _held(session, co, bk["id"])
        assert held - base == on_lines, (seed, step, action)

    stored = await _projections(session, co)
    import uuid as _uuid
    await lock_company(session, _uuid.UUID(co["company_id"]))
    await ProjectionEngine.rebuild(session, _uuid.UUID(co["company_id"]))
    await session.commit()
    assert await _projections(session, co) == stored


async def test_resolve_naming_only_another_banks_line_posts_nothing_and_says_why(client):
    co, a, b, la, (lb,) = await _transfer_banks(client, [("2026-01-06", "TRANSFER FROM CURRENT", "100.00")])
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [lb["id"]], "kind": "no_entry"}])
    row = r.json()["results"][0]
    assert r.status_code == 200 and row["ok"] is False, r.text
    assert row["reason"]["message_key"] == "acct.err_statement_line_not_found", r.text
    assert B.all_lines(await B.groups(client, co, b["id"]))[0]["status"] == "unmatched"
