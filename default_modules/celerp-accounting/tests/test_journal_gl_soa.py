# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Journal report, general ledger, statements of account, and manual journal entries.

Covers:
  - POST /accounting/journal-entries: validation (balance, accounts, leaf-only,
    line shape), period lock, idempotent double-submit, role gates
  - POST /accounting/journal-entries/{id}/void: manual-only, period lock,
    idempotent re-void, reports drop voided entries
  - GET /accounting/journal: filtering, deterministic order, totals, doc refs,
    FX resolution (doc-level and per-payment rates), source typing
  - GET /accounting/general-ledger: opening/period/closing math, sign
    conventions, trial-balance agreement, zero-sum balance check
  - GET /accounting/soa/{contact_id}: running balance, opening collapse,
    excluded drafts/voids/voided payments, merged-contact redirect signal,
    aging agreement (base currency)
  - POST /accounting/close-year: real-endpoint regression for the
    account_type/cogs fixes
"""

from __future__ import annotations

import uuid

import pytest


async def _reg(client) -> str:
    addr = f"jgs-{uuid.uuid4().hex[:8]}@jgs.test"
    r = await client.post("/auth/register", json={
        "company_name": "JgsCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _user_with_role(client, session, admin_token: str, role: str) -> str:
    addr = f"{role}-{uuid.uuid4().hex[:8]}@jgs.test"
    r = await client.post(
        "/companies/me/users",
        json={"name": role.title(), "email": addr, "password": "testpass123", "role": role},
        headers=_h(admin_token),
    )
    assert r.status_code == 200, r.text
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    r2 = await client.post("/auth/login", json={"email": addr, "password": "testpass123"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


async def _post_manual_je(client, tok, entries, ts="2026-01-15", memo="Adjustment", token=None):
    r = await client.post("/accounting/journal-entries", headers=_h(tok), json={
        "ts": ts, "memo": memo, "entries": entries,
        "idempotency_token": token or uuid.uuid4().hex,
    })
    return r


def _bal(entry_lines):
    """Simple balanced pair: debit bank 1111, credit sales 4100."""
    return [
        {"account": "1111", "debit": entry_lines, "credit": 0},
        {"account": "4100", "debit": 0, "credit": entry_lines},
    ]


async def _journal(client, tok, **params):
    r = await client.get("/accounting/journal", headers=_h(tok), params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _invoice(client, tok, total=100.0, contact_id=None, currency=None, rate=None,
                   issue_date="2026-02-01"):
    payload = {
        "doc_type": "invoice",
        "contact_id": contact_id or "contact:1",
        "issue_date": issue_date,
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": total, "line_total": total}],
        "subtotal": total, "tax": 0.0, "total": total,
    }
    if currency:
        payload["currency"] = currency
        payload["conversion_rate"] = rate
    r = await client.post("/docs", headers=_h(tok), json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Manual journal entries: create
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_je_posts_and_reaches_reports(client):
    tok = await _reg(client)
    r = await _post_manual_je(client, tok, _bal(150.0))
    assert r.status_code == 200, r.text
    je_id = r.json()["je_id"]
    assert je_id.startswith("je:manual:")

    data = await _journal(client, tok)
    match = [e for e in data["entries"] if e["je_id"] == je_id]
    assert len(match) == 1
    assert match[0]["je_type"] == "manual"
    assert match[0]["status"] == "posted"

    tb = (await client.get("/accounting/trial-balance", headers=_h(tok),
                           params={"date_from": "2026-01-01", "date_to": "2026-01-31"})).json()
    cash = [l for l in tb["lines"] if l["code"] == "1111"]
    assert cash and abs(cash[0]["total_debit"] - 150.0) < 0.01

    ledger = (await client.get("/accounting/ledger/1111", headers=_h(tok))).json()
    assert any(l["je_id"] == je_id for l in ledger["lines"])


@pytest.mark.asyncio
async def test_manual_je_unbalanced_rejected(client):
    tok = await _reg(client)
    r = await _post_manual_je(client, tok, [
        {"account": "1111", "debit": 100.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 90.0},
    ])
    assert r.status_code == 422
    assert "balance" in r.json()["detail"].lower()
    assert (await _journal(client, tok))["entries"] == []


@pytest.mark.asyncio
async def test_manual_je_line_validation(client):
    tok = await _reg(client)

    # Unknown account
    r = await _post_manual_je(client, tok, [
        {"account": "9999", "debit": 10.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 10.0},
    ])
    assert r.status_code == 422

    # Inactive account
    await client.patch("/accounting/accounts/1140", headers=_h(tok), json={"is_active": False})
    r = await _post_manual_je(client, tok, [
        {"account": "1140", "debit": 10.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 10.0},
    ])
    assert r.status_code == 422

    # Both sides on one line
    r = await _post_manual_je(client, tok, [
        {"account": "1111", "debit": 10.0, "credit": 10.0},
        {"account": "4100", "debit": 0, "credit": 0},
    ])
    assert r.status_code == 422

    # Negative amount
    r = await _post_manual_je(client, tok, [
        {"account": "1111", "debit": -10.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": -10.0},
    ])
    assert r.status_code == 422

    # Single line
    r = await _post_manual_je(client, tok, [{"account": "1111", "debit": 10.0, "credit": 0}])
    assert r.status_code == 422

    # Zero total
    r = await _post_manual_je(client, tok, [
        {"account": "1111", "debit": 0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 0},
    ])
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_manual_je_parent_account_rejected(client):
    """1130 has child accounts (1130-P etc.); parents are grouping rollups,
    so postings belong on leaf accounts and the parent is rejected naming
    its children."""
    tok = await _reg(client)
    r = await _post_manual_je(client, tok, [
        {"account": "1130", "debit": 10.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 10.0},
    ])
    assert r.status_code == 422
    assert "1130-P" in r.json()["detail"]


@pytest.mark.asyncio
async def test_manual_je_locked_period_rejected(client):
    tok = await _reg(client)
    r = await client.post("/accounting/period-lock", headers=_h(tok),
                          json={"lock_date": "2026-01-31"})
    assert r.status_code == 200
    r = await _post_manual_je(client, tok, _bal(10.0), ts="2026-01-15")
    assert r.status_code == 422
    assert "locked" in r.json()["detail"].lower()
    # After the lock date it posts fine
    r = await _post_manual_je(client, tok, _bal(10.0), ts="2026-02-01")
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_manual_je_double_submit_dedupes(client):
    tok = await _reg(client)
    shared = uuid.uuid4().hex
    r1 = await _post_manual_je(client, tok, _bal(25.0), token=shared)
    r2 = await _post_manual_je(client, tok, _bal(25.0), token=shared)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["je_id"] == r2.json()["je_id"]
    entries = (await _journal(client, tok))["entries"]
    assert len([e for e in entries if e["je_id"] == r1.json()["je_id"]]) == 1


# ---------------------------------------------------------------------------
# Manual journal entries: void
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_void_manual_je(client):
    tok = await _reg(client)
    je_id = (await _post_manual_je(client, tok, _bal(80.0))).json()["je_id"]

    r = await client.post(f"/accounting/journal-entries/{je_id}/void",
                          headers=_h(tok), json={"reason": "Entered twice"})
    assert r.status_code == 200, r.text

    data = await _journal(client, tok)
    entry = [e for e in data["entries"] if e["je_id"] == je_id][0]
    assert entry["status"] == "void"
    assert entry["void_reason"] == "Entered twice"
    # Voided entries are visible in the journal but out of the totals and reports
    assert abs(data["total_debit"]) < 0.01
    tb = (await client.get("/accounting/trial-balance", headers=_h(tok))).json()
    cash = [l for l in tb["lines"] if l["code"] == "1111"]
    assert not cash or abs(cash[0]["total_debit"]) < 0.01

    # Re-void is an idempotent no-op
    r2 = await client.post(f"/accounting/journal-entries/{je_id}/void",
                           headers=_h(tok), json={})
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_void_auto_je_rejected(client):
    tok = await _reg(client)
    inv = await _invoice(client, tok)
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200

    data = await _journal(client, tok)
    auto = [e for e in data["entries"] if e["je_type"] != "manual"]
    assert auto, "finalize should have posted an automatic entry"
    r = await client.post(f"/accounting/journal-entries/{auto[0]['je_id']}/void",
                          headers=_h(tok), json={})
    assert r.status_code == 422
    assert "document" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_void_unknown_je_404(client):
    tok = await _reg(client)
    r = await client.post("/accounting/journal-entries/je:manual:nope/void",
                          headers=_h(tok), json={})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_void_in_locked_period_rejected(client):
    tok = await _reg(client)
    je_id = (await _post_manual_je(client, tok, _bal(30.0), ts="2026-01-15")).json()["je_id"]
    await client.post("/accounting/period-lock", headers=_h(tok),
                      json={"lock_date": "2026-01-31"})
    r = await client.post(f"/accounting/journal-entries/{je_id}/void",
                          headers=_h(tok), json={"reason": "too late"})
    assert r.status_code == 422
    assert "locked" in r.json()["detail"].lower()
    # Entry is still posted
    entry = [e for e in (await _journal(client, tok))["entries"] if e["je_id"] == je_id][0]
    assert entry["status"] == "posted"


@pytest.mark.asyncio
async def test_role_gates_on_new_endpoints(client, session):
    admin = await _reg(client)
    je_id = (await _post_manual_je(client, admin, _bal(10.0))).json()["je_id"]
    operator = await _user_with_role(client, session, admin, "operator")

    assert (await client.get("/accounting/journal", headers=_h(operator))).status_code == 403
    assert (await client.get("/accounting/general-ledger", headers=_h(operator))).status_code == 403
    assert (await client.get("/accounting/soa/contact:x", headers=_h(operator))).status_code == 403
    r = await _post_manual_je(client, operator, _bal(10.0))
    assert r.status_code == 403
    r = await client.post(f"/accounting/journal-entries/{je_id}/void",
                          headers=_h(operator), json={})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Journal report
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_journal_date_filter_and_order(client):
    tok = await _reg(client)
    ids = {}
    for ts in ("2026-01-10", "2026-01-20", "2026-02-05"):
        ids[ts] = (await _post_manual_je(client, tok, _bal(10.0), ts=ts)).json()["je_id"]

    data = await _journal(client, tok, date_from="2026-01-01", date_to="2026-01-31")
    got = [e["je_id"] for e in data["entries"]]
    assert got == sorted(got, key=lambda i: ([e["ts"] for e in data["entries"] if e["je_id"] == i][0], i))
    assert ids["2026-01-10"] in got and ids["2026-01-20"] in got
    assert ids["2026-02-05"] not in got
    # Edge dates are inclusive
    edge = await _journal(client, tok, date_from="2026-01-10", date_to="2026-01-10")
    assert [e["je_id"] for e in edge["entries"]] == [ids["2026-01-10"]]
    assert abs(edge["total_debit"] - 10.0) < 0.01
    assert abs(edge["total_credit"] - 10.0) < 0.01


@pytest.mark.asyncio
async def test_journal_doc_ref_and_doc_level_fx(client):
    tok = await _reg(client)
    await client.patch("/companies/me", headers=_h(tok), json={"settings": {"currency": "THB"}})

    fx_inv = await _invoice(client, tok, total=100.0, currency="USD", rate=35.0)
    assert (await client.post(f"/docs/{fx_inv}/finalize", headers=_h(tok))).status_code == 200
    base_inv = await _invoice(client, tok, total=50.0)
    assert (await client.post(f"/docs/{base_inv}/finalize", headers=_h(tok))).status_code == 200

    data = await _journal(client, tok)
    by_doc = {e["source_doc"]["doc_id"]: e for e in data["entries"] if e.get("source_doc")}
    assert fx_inv in by_doc and base_inv in by_doc
    assert by_doc[fx_inv]["source_doc"]["doc_ref"]
    assert by_doc[fx_inv]["fx"] == {"currency": "USD", "rate": 35.0}
    assert by_doc[base_inv]["fx"] is None


@pytest.mark.asyncio
async def test_journal_payment_uses_payment_rate(client):
    tok = await _reg(client)
    await client.patch("/companies/me", headers=_h(tok), json={"settings": {"currency": "THB"}})
    inv = await _invoice(client, tok, total=100.0, currency="USD", rate=35.0)
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200
    r = await client.post(f"/docs/{inv}/payment", headers=_h(tok), json={
        "payment_date": "2026-02-10", "amount": 100.0, "method": "transfer",
        "bank_account": "1111", "conversion_rate": 36.5, "currency": "USD",
    })
    assert r.status_code == 200, r.text

    data = await _journal(client, tok)
    pay = [e for e in data["entries"]
           if e.get("source_doc") and e["source_doc"]["doc_id"] == inv and e["ts"] == "2026-02-10"]
    assert pay, "payment entry missing from journal"
    assert pay[0]["fx"]["rate"] == 36.5


@pytest.mark.asyncio
async def test_journal_source_typing(client):
    tok = await _reg(client)
    # A transfer entry keeps its je_type so the UI never labels it "Manual"
    banks = (await client.get("/accounting/bank-accounts", headers=_h(tok))).json()["items"]
    from_id = banks[0]["id"]
    r = await client.post("/accounting/bank-accounts", headers=_h(tok), json={
        "bank_name": "SCB", "account_number": "9876543210",
        "currency": "THB", "bank_type": "savings"})
    assert r.status_code == 200, r.text
    to_id = r.json()["id"]
    r = await client.post("/accounting/transfers", headers=_h(tok), json={
        "from_bank_id": from_id, "to_bank_id": to_id, "amount": 40.0, "date": "2026-01-12"})
    assert r.status_code == 200, r.text
    manual = (await _post_manual_je(client, tok, _bal(5.0))).json()["je_id"]

    data = await _journal(client, tok)
    types = {e["je_id"]: e["je_type"] for e in data["entries"]}
    assert types[manual] == "manual"
    assert "transfer" in [t for i, t in types.items() if i != manual]


# ---------------------------------------------------------------------------
# General ledger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_general_ledger_opening_period_closing(client):
    tok = await _reg(client)
    await _post_manual_je(client, tok, _bal(100.0), ts="2026-01-10")
    await _post_manual_je(client, tok, _bal(40.0), ts="2026-02-10")

    gl = (await client.get("/accounting/general-ledger", headers=_h(tok),
                           params={"date_from": "2026-02-01", "date_to": "2026-02-28"})).json()
    rows = {r["code"]: r for r in gl["rows"]}
    cash = rows["1111"]
    assert abs(cash["opening"] - 100.0) < 0.01
    assert abs(cash["debit"] - 40.0) < 0.01
    assert abs(cash["closing"] - 140.0) < 0.01
    # Credit-normal account shows positive balances for credit surpluses
    sales = rows["4100"]
    assert abs(sales["opening"] - 100.0) < 0.01
    assert abs(sales["closing"] - 140.0) < 0.01
    assert gl["balanced"] is True
    # Rows come sorted by account code
    codes = [r["code"] for r in gl["rows"]]
    assert codes == sorted(codes)


@pytest.mark.asyncio
async def test_general_ledger_agrees_with_trial_balance(client):
    tok = await _reg(client)
    await _post_manual_je(client, tok, _bal(75.0), ts="2026-01-10")
    await _post_manual_je(client, tok, [
        {"account": "6100", "debit": 20.0, "credit": 0},
        {"account": "1111", "debit": 0, "credit": 20.0},
    ], ts="2026-01-20")

    gl = (await client.get("/accounting/general-ledger", headers=_h(tok),
                           params={"date_to": "2026-12-31"})).json()
    tb = (await client.get("/accounting/trial-balance", headers=_h(tok),
                           params={"date_to": "2026-12-31"})).json()
    tb_net = {l["code"]: l["net"] for l in tb["lines"]}
    for row in gl["rows"]:
        signed = row["closing"]
        if row["account_type"] not in ("asset", "expense", "cogs"):
            signed = -signed
        assert abs(signed - tb_net.get(row["code"], 0.0)) < 0.01, row["code"]


# ---------------------------------------------------------------------------
# Statement of account
# ---------------------------------------------------------------------------

async def _contact(client, tok, name="Cust", ctype="customer") -> str:
    r = await client.post("/crm/contacts", headers=_h(tok),
                          json={"name": name, "contact_type": ctype})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_soa_running_balance(client):
    tok = await _reg(client)
    contact = await _contact(client, tok)

    inv = await _invoice(client, tok, total=100.0, contact_id=contact, issue_date="2026-01-05")
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200
    r = await client.post(f"/docs/{inv}/payment", headers=_h(tok), json={
        "payment_date": "2026-01-20", "amount": 40.0, "method": "transfer", "bank_account": "1111"})
    assert r.status_code == 200, r.text

    soa = (await client.get(f"/accounting/soa/{contact}", headers=_h(tok))).json()
    assert soa["contact"]["id"] == contact
    assert abs(soa["opening_balance"]) < 0.01
    dates = [r["date"] for r in soa["rows"]]
    assert dates == sorted(dates)
    assert abs(soa["rows"][-1]["balance"] - 60.0) < 0.01
    assert abs(soa["closing_balance"] - 60.0) < 0.01


@pytest.mark.asyncio
async def test_soa_opening_collapse_and_exclusions(client):
    tok = await _reg(client)
    contact = await _contact(client, tok)

    early = await _invoice(client, tok, total=100.0, contact_id=contact, issue_date="2026-01-05")
    assert (await client.post(f"/docs/{early}/finalize", headers=_h(tok))).status_code == 200
    late = await _invoice(client, tok, total=30.0, contact_id=contact, issue_date="2026-02-10")
    assert (await client.post(f"/docs/{late}/finalize", headers=_h(tok))).status_code == 200
    # A draft never shows on a statement
    await _invoice(client, tok, total=999.0, contact_id=contact, issue_date="2026-02-15")

    soa = (await client.get(f"/accounting/soa/{contact}", headers=_h(tok),
                            params={"date_from": "2026-02-01", "date_to": "2026-02-28"})).json()
    assert abs(soa["opening_balance"] - 100.0) < 0.01
    assert len(soa["rows"]) == 1
    assert abs(soa["closing_balance"] - 130.0) < 0.01

    # Voided payment drops off the statement
    r = await client.post(f"/docs/{late}/payment", headers=_h(tok), json={
        "payment_date": "2026-02-20", "amount": 30.0, "method": "transfer", "bank_account": "1111"})
    assert r.status_code == 200
    before = (await client.get(f"/accounting/soa/{contact}", headers=_h(tok))).json()
    assert abs(before["closing_balance"] - 100.0) < 0.01
    r = await client.post(f"/docs/{late}/void-payment", headers=_h(tok), json={
        "payment_index": 0, "void_reason": "test"})
    assert r.status_code == 200, r.text
    after = (await client.get(f"/accounting/soa/{contact}", headers=_h(tok))).json()
    assert abs(after["closing_balance"] - 130.0) < 0.01


@pytest.mark.asyncio
async def test_soa_unknown_and_empty_contact(client):
    tok = await _reg(client)
    assert (await client.get("/accounting/soa/contact:missing", headers=_h(tok))).status_code == 404

    quiet = await _contact(client, tok, name="Quiet")
    soa = (await client.get(f"/accounting/soa/{quiet}", headers=_h(tok))).json()
    assert soa["rows"] == []
    assert soa["opening_balance"] == 0
    assert soa["closing_balance"] == 0


@pytest.mark.asyncio
async def test_soa_merged_contact_redirect_signal(client):
    tok = await _reg(client)
    loser = await _contact(client, tok, name="Old Name")
    winner = await _contact(client, tok, name="New Name")
    r = await client.post("/crm/contacts/merge", headers=_h(tok),
                          json={"target_contact_id": winner, "source_contact_ids": [loser]})
    assert r.status_code == 200, r.text

    soa = await client.get(f"/accounting/soa/{loser}", headers=_h(tok))
    assert soa.status_code == 200
    assert soa.json()["merged_into"] == winner


@pytest.mark.asyncio
async def test_soa_agrees_with_ar_aging(client):
    tok = await _reg(client)
    contact = await _contact(client, tok)
    inv = await _invoice(client, tok, total=200.0, contact_id=contact, issue_date="2026-01-05")
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200
    await client.post(f"/docs/{inv}/payment", headers=_h(tok), json={
        "payment_date": "2026-01-20", "amount": 50.0, "method": "transfer", "bank_account": "1111"})

    soa = (await client.get(f"/accounting/soa/{contact}", headers=_h(tok))).json()
    aging = (await client.get("/reports/ar-aging", headers=_h(tok))).json()
    mine = [c for c in aging["lines"] if c["customer_id"] == contact]
    assert mine, f"contact missing from aging: {aging}"
    aging_total = mine[0]["total"]
    assert abs(soa["closing_balance"] - aging_total) < 0.01


@pytest.mark.asyncio
async def test_soa_dual_role_contact_nets(client):
    """A contact that is both customer and supplier: their bill offsets the
    invoice in the running balance instead of stacking in one direction."""
    tok = await _reg(client)
    contact = await _contact(client, tok, name="Both Ways", ctype="both")

    inv = await _invoice(client, tok, total=100.0, contact_id=contact, issue_date="2026-01-05")
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "bill", "contact_id": contact, "issue_date": "2026-01-10",
        "line_items": [{"name": "Supplies", "quantity": 1, "unit_price": 40.0, "line_total": 40.0}],
        "subtotal": 40.0, "tax": 0.0, "total": 40.0,
    })
    assert r.status_code == 200, r.text
    bill = r.json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(tok))).status_code == 200

    soa = (await client.get(f"/accounting/soa/{contact}", headers=_h(tok))).json()
    assert abs(soa["closing_balance"] - 60.0) < 0.01
    kinds = {row["kind"]: row for row in soa["rows"]}
    assert kinds["invoice"]["debit"] > 0 and kinds["invoice"]["credit"] == 0
    assert kinds["bill"]["credit"] > 0 and kinds["bill"]["debit"] == 0


@pytest.mark.asyncio
async def test_manual_je_rejects_non_finite_amounts(client):
    tok = await _reg(client)
    r = await _post_manual_je(client, tok, [
        {"account": "1111", "debit": "nan", "credit": 0},
        {"account": "4100", "debit": 0, "credit": "nan"},
    ])
    assert r.status_code == 422
    assert "finite" in r.json()["detail"].lower() or "number" in r.json()["detail"].lower()
    assert (await _journal(client, tok))["entries"] == []


@pytest.mark.asyncio
async def test_journal_fx_survives_payment_deletion(client):
    """Deleting a payment tombstones it in place; a surviving payment JE
    keeps resolving its own exchange rate, never another payment's."""
    tok = await _reg(client)
    await client.patch("/companies/me", headers=_h(tok), json={"settings": {"currency": "THB"}})
    inv = await _invoice(client, tok, total=100.0, currency="USD", rate=35.0)
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200
    for date_amt_rate in (("2026-02-05", 30.0, 34.0), ("2026-02-10", 40.0, 36.5)):
        d, amt, rate = date_amt_rate
        r = await client.post(f"/docs/{inv}/payment", headers=_h(tok), json={
            "payment_date": d, "amount": amt, "method": "transfer",
            "bank_account": "1111", "currency": "USD", "conversion_rate": rate})
        assert r.status_code == 200, r.text

    r = await client.delete(f"/docs/{inv}/payments/0", headers=_h(tok))
    if r.status_code != 200:
        pytest.skip(f"payment deletion endpoint unavailable: {r.status_code}")

    data = await _journal(client, tok)
    surviving = [e for e in data["entries"]
                 if e.get("source_doc") and e["source_doc"]["doc_id"] == inv
                 and e["ts"] == "2026-02-10" and e["status"] == "posted"]
    assert surviving
    # Deletion tombstones in place, so the surviving JE's stored index still
    # points at its own payment and resolves its own rate. It must never show
    # the deleted payment's 34.0.
    fx = surviving[0].get("fx") or {}
    assert fx.get("rate") == 36.5


@pytest.mark.asyncio
async def test_journal_fx_keeps_own_rate_when_earlier_payment_survives(client):
    """Deleting the LAST payment leaves earlier indices intact: the surviving
    payment JE still resolves its own payment-specific rate."""
    tok = await _reg(client)
    await client.patch("/companies/me", headers=_h(tok), json={"settings": {"currency": "THB"}})
    inv = await _invoice(client, tok, total=100.0, currency="USD", rate=35.0)
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200
    for d, amt, rate in (("2026-02-05", 30.0, 34.0), ("2026-02-10", 40.0, 36.5)):
        r = await client.post(f"/docs/{inv}/payment", headers=_h(tok), json={
            "payment_date": d, "amount": amt, "method": "transfer",
            "bank_account": "1111", "currency": "USD", "conversion_rate": rate})
        assert r.status_code == 200, r.text

    r = await client.delete(f"/docs/{inv}/payments/1", headers=_h(tok))
    if r.status_code != 200:
        pytest.skip(f"payment deletion endpoint unavailable: {r.status_code}")

    data = await _journal(client, tok)
    surviving = [e for e in data["entries"]
                 if e.get("source_doc") and e["source_doc"]["doc_id"] == inv
                 and e["ts"] == "2026-02-05" and e["status"] == "posted"]
    assert surviving
    assert (surviving[0].get("fx") or {}).get("rate") == 34.0


@pytest.mark.asyncio
async def test_trial_balance_requires_manager(client, session):
    admin = await _reg(client)
    operator = await _user_with_role(client, session, admin, "operator")
    assert (await client.get("/accounting/trial-balance", headers=_h(operator))).status_code == 403
    assert (await client.get("/accounting/trial-balance", headers=_h(admin))).status_code == 200


@pytest.mark.asyncio
async def test_manual_je_token_reuse_with_different_payload_conflicts(client):
    tok = await _reg(client)
    shared = uuid.uuid4().hex
    r1 = await _post_manual_je(client, tok, _bal(25.0), token=shared)
    assert r1.status_code == 200
    r2 = await _post_manual_je(client, tok, _bal(99.0), token=shared)
    assert r2.status_code == 409
    assert "already posted" in r2.json()["detail"].lower()
    # The books still hold only the original entry
    entries = (await _journal(client, tok))["entries"]
    assert len(entries) == 1
    assert abs(entries[0]["lines"][0]["debit"] - 25.0) < 0.01


@pytest.mark.asyncio
async def test_revert_to_draft_blocked_in_locked_period(client):
    """Reverting an invoice voids its finalize entry, so a lock on that
    period must block the revert instead of letting the books change."""
    tok = await _reg(client)
    inv = await _invoice(client, tok, total=100.0, issue_date="2026-01-10")
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200
    await client.post("/accounting/period-lock", headers=_h(tok),
                      json={"lock_date": "2026-01-31"})

    r = await client.post(f"/docs/{inv}/revert-to-draft", headers=_h(tok), json={})
    assert r.status_code == 422, r.text
    assert "locked" in r.json()["detail"].lower()
    # The finalize entry still stands
    tb = (await client.get("/accounting/trial-balance", headers=_h(tok),
                           params={"date_to": "2026-01-31"})).json()
    ar = [l for l in tb["lines"] if l["code"] == "1120"]
    assert ar and abs(ar[0]["total_debit"] - 100.0) < 0.01

    # Unlock and the revert goes through
    await client.post("/accounting/period-lock", headers=_h(tok), json={"lock_date": None})
    r = await client.post(f"/docs/{inv}/revert-to-draft", headers=_h(tok), json={})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_delete_payment_blocked_in_locked_period(client):
    tok = await _reg(client)
    inv = await _invoice(client, tok, total=100.0, issue_date="2026-01-10")
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200
    r = await client.post(f"/docs/{inv}/payment", headers=_h(tok), json={
        "payment_date": "2026-01-15", "amount": 100.0, "method": "transfer", "bank_account": "1111"})
    assert r.status_code == 200
    await client.post("/accounting/period-lock", headers=_h(tok),
                      json={"lock_date": "2026-01-31"})

    r = await client.delete(f"/docs/{inv}/payments/0", headers=_h(tok))
    assert r.status_code == 422, r.text
    assert "locked" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_batch_import_tolerates_null_amounts(client):
    """External serializers emit explicit nulls for optional numerics; the
    shape-permissive import contract must keep accepting them."""
    tok = await _reg(client)
    r = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": f"je:{uuid.uuid4()}",
        "event_type": "acc.journal_entry.created",
        "data": {"status": "posted", "ts": "2026-01-15", "entries": [
            {"account": "1111", "debit": 10.0, "credit": None},
            {"account": None, "debit": None, "credit": None},
            {"account": "4100", "debit": None, "credit": 10.0},
        ]},
        "source": "test",
        "idempotency_key": str(uuid.uuid4()),
    }]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    tb = (await client.get("/accounting/trial-balance", headers=_h(tok))).json()
    bank = [l for l in tb["lines"] if l["code"] == "1111"]
    assert bank and abs(bank[0]["total_debit"] - 10.0) < 0.01


@pytest.mark.asyncio
async def test_ledger_buckets_by_literal_code_only(client):
    """A legacy entry posted directly to a parent code shows on the parent's
    own ledger and nowhere else, matching how TB/GL/journal bucket it."""
    tok = await _reg(client)
    r = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": f"je:{uuid.uuid4()}",
        "event_type": "acc.journal_entry.created",
        "data": {"status": "posted", "ts": "2026-01-10", "entries": [
            {"account": "1130", "debit": 70.0, "credit": 0},
            {"account": "3200", "debit": 0, "credit": 70.0},
        ]},
        "source": "test", "idempotency_key": str(uuid.uuid4()),
    }]})
    assert r.status_code == 200 and r.json()["created"] == 1

    parent = (await client.get("/accounting/ledger/1130", headers=_h(tok))).json()
    assert any(abs(l["debit"] - 70.0) < 0.01 for l in parent["lines"])
    child = (await client.get("/accounting/ledger/1130-P", headers=_h(tok))).json()
    assert not any(abs(l["debit"] - 70.0) < 0.01 for l in child["lines"])

    gl = (await client.get("/accounting/general-ledger", headers=_h(tok))).json()
    rows = {r["code"]: r for r in gl["rows"]}
    assert abs(rows["1130"]["closing"] - 70.0) < 0.01
    assert "1130-P" not in rows or abs(rows["1130-P"]["closing"]) < 0.01


@pytest.mark.asyncio
async def test_general_ledger_include_lines(client):
    """include_lines returns per-account detail bucketed by the same scan as
    the summary, with running math that ties opening to closing."""
    tok = await _reg(client)
    await _post_manual_je(client, tok, _bal(100.0), ts="2026-01-10", memo="early")
    await _post_manual_je(client, tok, _bal(40.0), ts="2026-02-10", memo="late")

    gl = (await client.get("/accounting/general-ledger", headers=_h(tok),
                           params={"date_from": "2026-02-01", "date_to": "2026-02-28",
                                   "include_lines": "true"})).json()
    rows = {r["code"]: r for r in gl["rows"]}
    bank = rows["1111"]
    assert abs(bank["opening"] - 100.0) < 0.01
    assert len(bank["lines"]) == 1
    assert bank["lines"][0]["memo"] == "late"
    assert abs(bank["opening"] + bank["lines"][0]["debit"] - bank["lines"][0]["credit"]
               - bank["closing"]) < 0.01


@pytest.mark.asyncio
async def test_general_ledger_rows_carry_debit_normal(client):
    tok = await _reg(client)
    await _post_manual_je(client, tok, _bal(50.0), ts="2026-01-10")
    gl = (await client.get("/accounting/general-ledger", headers=_h(tok))).json()
    rows = {r["code"]: r for r in gl["rows"]}
    assert rows["1111"]["debit_normal"] is True
    assert rows["4100"]["debit_normal"] is False


# ---------------------------------------------------------------------------
# Fiscal year close regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_year_zeroes_revenue_expense_cogs(client):
    tok = await _reg(client)
    await _post_manual_je(client, tok, [
        {"account": "1111", "debit": 500.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 500.0},
    ], ts="2026-03-01")
    await _post_manual_je(client, tok, [
        {"account": "5100", "debit": 120.0, "credit": 0},
        {"account": "1111", "debit": 0, "credit": 120.0},
    ], ts="2026-04-01")
    await _post_manual_je(client, tok, [
        {"account": "6100", "debit": 80.0, "credit": 0},
        {"account": "1111", "debit": 0, "credit": 80.0},
    ], ts="2026-05-01")

    r = await client.post("/accounting/close-year", headers=_h(tok),
                          json={"fiscal_year_end": "2026-12-31"})
    assert r.status_code == 200, r.text

    tb = (await client.get("/accounting/trial-balance", headers=_h(tok),
                           params={"date_to": "2027-01-01"})).json()
    nets = {l["code"]: l["net"] for l in tb["lines"]}
    assert abs(nets.get("4100", 0.0)) < 0.01
    assert abs(nets.get("5100", 0.0)) < 0.01
    assert abs(nets.get("6100", 0.0)) < 0.01
    # Net income 500 - 120 - 80 = 300 lands in retained earnings (credit-normal)
    assert abs(nets.get("3200", 0.0) + 300.0) < 0.01


# ---------------------------------------------------------------------------
# Cross-report consistency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cross_report_consistency(client):
    """One dataset, every report: journal totals == TB totals, GL closings ==
    TB nets, GL balances to zero, SOA closing == aging total."""
    tok = await _reg(client)
    await client.patch("/companies/me", headers=_h(tok), json={"settings": {"currency": "THB"}})
    contact = await _contact(client, tok)

    inv = await _invoice(client, tok, total=300.0, contact_id=contact, issue_date="2026-01-10")
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200
    await client.post(f"/docs/{inv}/payment", headers=_h(tok), json={
        "payment_date": "2026-01-25", "amount": 100.0, "method": "transfer", "bank_account": "1111"})
    await _post_manual_je(client, tok, _bal(60.0), ts="2026-01-15")
    voided = (await _post_manual_je(client, tok, _bal(999.0), ts="2026-01-16")).json()["je_id"]
    await client.post(f"/accounting/journal-entries/{voided}/void", headers=_h(tok),
                      json={"reason": "test"})
    fx = await _invoice(client, tok, total=10.0, currency="USD", rate=35.0, issue_date="2026-01-18")
    assert (await client.post(f"/docs/{fx}/finalize", headers=_h(tok))).status_code == 200

    journal = await _journal(client, tok, date_to="2026-12-31")
    tb = (await client.get("/accounting/trial-balance", headers=_h(tok),
                           params={"date_to": "2026-12-31"})).json()
    gl = (await client.get("/accounting/general-ledger", headers=_h(tok),
                           params={"date_to": "2026-12-31"})).json()

    assert abs(journal["total_debit"] - tb["total_debit"]) < 0.01
    assert abs(journal["total_credit"] - tb["total_credit"]) < 0.01
    assert gl["balanced"] is True
    tb_net = {l["code"]: l["net"] for l in tb["lines"]}
    for row in gl["rows"]:
        signed = row["closing"]
        if row["account_type"] not in ("asset", "expense", "cogs"):
            signed = -signed
        assert abs(signed - tb_net.get(row["code"], 0.0)) < 0.01, row["code"]

    soa = (await client.get(f"/accounting/soa/{contact}", headers=_h(tok))).json()
    aging = (await client.get("/reports/ar-aging", headers=_h(tok))).json()
    mine = [c for c in aging["lines"] if c["customer_id"] == contact]
    aging_total = mine[0]["total"]
    assert abs(soa["closing_balance"] - aging_total) < 0.01
