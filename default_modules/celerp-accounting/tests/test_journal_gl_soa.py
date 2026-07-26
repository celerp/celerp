# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Journal report, general ledger, statements of account, and manual journal entries.

Covers:
  - POST /accounting/journal-entries: validation (balance, accounts, leaf-only,
    line shape), period lock, idempotent double-submit, role gates
  - POST /accounting/journal-entries/{id}/void: manual-only, period lock,
    idempotent re-void, reports drop voided entries
  - POST /accounting/journal-entries/bulk-void: per-entry refusals, the batch
    carrying on past one, the selection cap, idempotence, the permission gate
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
    payload = {
        "ts": ts, "memo": memo, "entries": entries,
        "idempotency_token": token or uuid.uuid4().hex,
    }
    r = await client.post("/accounting/journal-entries", headers=_h(tok), json=payload)
    return r


def _fx(line: dict, currency: str, rate: float) -> dict:
    """The same line, typed in a foreign currency at a rate."""
    return {**line, "currency": currency, "rate": rate}


async def _thb(client, tok):
    """Put the company on a base currency that is not the tested foreign one."""
    await client.patch("/companies/me", headers=_h(tok), json={"settings": {"currency": "THB"}})


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
    """The 404 must come from the handler rejecting an unknown entry, not from
    the route being absent. The app flattens every 404 detail to "Not found"
    (celerp/main.py:330), so the discriminator is that the very same route
    voids a real entry successfully in this test.
    """
    tok = await _reg(client)
    je_id = (await _post_manual_je(client, tok, _bal(10.0))).json()["je_id"]

    real = await client.post(f"/accounting/journal-entries/{je_id}/void",
                             headers=_h(tok), json={})
    assert real.status_code == 200, real.text  # the route exists and works

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
# Manual journal entries: bulk void
# ---------------------------------------------------------------------------

async def _bulk_void(client, tok, je_ids, reason=None):
    return await client.post("/accounting/journal-entries/bulk-void",
                             headers=_h(tok), json={"je_ids": je_ids, "reason": reason})


def _by_id(body: dict) -> dict:
    return {r["je_id"]: r for r in body["results"]}


@pytest.mark.asyncio
async def test_bulk_void_voids_every_manual_entry(client):
    tok = await _reg(client)
    a = (await _post_manual_je(client, tok, _bal(15.0))).json()["je_id"]
    b = (await _post_manual_je(client, tok, _bal(25.0))).json()["je_id"]

    r = await _bulk_void(client, tok, [a, b], reason="Duplicated batch")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["voided"], body["refused"]) == (2, 0)
    assert all(x["status"] == "void" for x in body["results"])

    entries = {e["je_id"]: e for e in (await _journal(client, tok))["entries"]}
    for je_id in (a, b):
        assert entries[je_id]["status"] == "void"
        assert entries[je_id]["void_reason"] == "Duplicated batch"


@pytest.mark.asyncio
async def test_bulk_void_refuses_document_driven_entries_per_entry(client):
    """A refusal is recorded against its own entry and the rest of the batch proceeds."""
    tok = await _reg(client)
    manual = (await _post_manual_je(client, tok, _bal(20.0))).json()["je_id"]
    inv = await _invoice(client, tok)
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(tok))).status_code == 200
    auto = [e["je_id"] for e in (await _journal(client, tok))["entries"]
            if e["je_type"] != "manual"]
    assert auto, "finalize should have posted an automatic entry"

    r = await _bulk_void(client, tok, [manual, auto[0]], reason="Cleanup")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["voided"], body["refused"]) == (1, 1)
    results = _by_id(body)
    assert results[manual]["status"] == "void"
    assert results[auto[0]]["status"] == "refused"
    # The entry is named by its own je_id, which for an automatic entry carries the
    # document it mirrors, and the detail says what to do with it instead.
    assert "document" in results[auto[0]]["detail"].lower()

    entries = {e["je_id"]: e for e in (await _journal(client, tok))["entries"]}
    assert entries[manual]["status"] == "void"
    assert entries[auto[0]]["status"] != "void"


@pytest.mark.asyncio
async def test_bulk_void_respects_the_period_lock(client):
    tok = await _reg(client)
    locked = (await _post_manual_je(client, tok, _bal(30.0), ts="2026-01-15")).json()["je_id"]
    await client.post("/accounting/period-lock", headers=_h(tok),
                      json={"lock_date": "2026-01-31"})

    r = await _bulk_void(client, tok, [locked], reason="too late")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["voided"], body["refused"]) == (0, 1)
    assert "locked" in body["results"][0]["detail"].lower()
    assert "2026-01-31" in body["results"][0]["detail"]

    entry = [e for e in (await _journal(client, tok))["entries"] if e["je_id"] == locked][0]
    assert entry["status"] == "posted"


@pytest.mark.asyncio
async def test_bulk_void_continues_after_a_locked_period_entry(client, session):
    """The middle entry is refused; the two around it are voided.

    A refusal in the middle of a selection has to leave the entries after it alone.
    The order matters: an implementation that raised on the first refusal, or that
    left the session unusable for the entries behind it, would void the first and
    lose the third.
    """
    tok = await _reg(client)
    first = (await _post_manual_je(client, tok, _bal(11.0), ts="2026-03-05")).json()["je_id"]
    middle = (await _post_manual_je(client, tok, _bal(12.0), ts="2026-01-20")).json()["je_id"]
    last = (await _post_manual_je(client, tok, _bal(13.0), ts="2026-03-07")).json()["je_id"]
    await client.post("/accounting/period-lock", headers=_h(tok),
                      json={"lock_date": "2026-01-31"})

    r = await _bulk_void(client, tok, [first, middle, last], reason="Reversed")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["voided"], body["refused"]) == (2, 1)
    results = _by_id(body)
    assert results[first]["status"] == "void"
    assert results[middle]["status"] == "refused"
    assert results[last]["status"] == "void"

    entries = {e["je_id"]: e for e in (await _journal(client, tok))["entries"]}
    assert entries[first]["status"] == "void"
    assert entries[middle]["status"] == "posted"
    assert entries[last]["status"] == "void"


@pytest.mark.asyncio
async def test_bulk_void_is_idempotent(client, session):
    """The same request twice leaves one void event, and the second is not an error."""
    from sqlalchemy import select
    from celerp.models.ledger import LedgerEntry

    tok = await _reg(client)
    je_id = (await _post_manual_je(client, tok, _bal(40.0))).json()["je_id"]

    first = await _bulk_void(client, tok, [je_id], reason="Once")
    assert first.status_code == 200, first.text
    second = await _bulk_void(client, tok, [je_id], reason="Twice")
    assert second.status_code == 200, second.text
    assert second.json()["voided"] == 1
    assert second.json()["results"][0]["void_reason"] == "Once"

    events = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id == je_id,
        LedgerEntry.event_type == "acc.journal_entry.voided"))).scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_bulk_void_requires_manage_accounting(client, session):
    """Reading the books is not changing them: a report-only grant cannot void."""
    from test_helpers import grant_permission
    admin = await _reg(client)
    je_id = (await _post_manual_je(client, admin, _bal(10.0))).json()["je_id"]
    reader = await _user_with_role(client, session, admin, "operator")
    await grant_permission(client, _h(admin), "view_financial_reports", "operator")
    assert (await client.get("/accounting/journal", headers=_h(reader))).status_code == 200

    r = await _bulk_void(client, reader, [je_id], reason="Not mine to void")
    assert r.status_code == 403, r.text
    assert "manage_accounting" in r.json()["detail"]
    entry = [e for e in (await _journal(client, admin))["entries"] if e["je_id"] == je_id][0]
    assert entry["status"] == "posted"


@pytest.mark.asyncio
async def test_bulk_void_caps_the_selection(client):
    """Over the cap is refused with the count and the limit, not truncated silently."""
    tok = await _reg(client)
    r = await _bulk_void(client, tok, [f"je:manual:{i}" for i in range(201)])
    assert r.status_code == 422, r.text
    assert "201" in r.json()["detail"] and "200" in r.json()["detail"]

    empty = await _bulk_void(client, tok, [" ", ""])
    assert empty.status_code == 422, empty.text


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
# Extended journal
# ---------------------------------------------------------------------------

async def _extended(client, tok, **params):
    r = await client.get("/accounting/extended-journal", headers=_h(tok), params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _doc_with_items(client, tok, doc_type, items, *, tax=0.0, shipping=0.0,
                          currency=None, rate=None, issue_date="2026-02-01"):
    """A finalized document whose lines are the ones handed in.

    items are (sku, quantity, unit_price) triples; the totals are computed from
    them so the posting and the lines agree unless a test deliberately adds
    shipping the lines do not account for.
    """
    line_items = [{"sku": sku, "name": sku, "quantity": qty, "unit_price": price,
                   "line_total": round(qty * price, 2)}
                  for sku, qty, price in items]
    subtotal = round(sum(li["line_total"] for li in line_items), 2)
    payload = {
        "doc_type": doc_type, "contact_id": "contact:1", "issue_date": issue_date,
        "line_items": line_items, "subtotal": subtotal, "tax": tax,
        "shipping": shipping, "total": round(subtotal + tax + shipping, 2),
    }
    if currency:
        payload["currency"] = currency
        payload["conversion_rate"] = rate
    r = await client.post("/docs", headers=_h(tok), json=payload)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=_h(tok))).status_code == 200
    return doc_id


def _entry_for(data, doc_id):
    hits = [e for e in data["entries"]
            if (e.get("source_doc") or {}).get("doc_id") == doc_id]
    assert hits, f"no journal entry for {doc_id}"
    return hits[0]


@pytest.mark.asyncio
async def test_extended_journal_splits_a_sale_into_one_row_per_item(client):
    tok = await _reg(client)
    doc = await _doc_with_items(client, tok, "invoice",
                                [("WIDGET", 2, 30.0), ("GIZMO", 1, 40.0)])

    entry = _entry_for(await _extended(client, tok), doc)
    assert entry["items_status"] == "expanded"
    sold = [l for l in entry["lines"] if l.get("item")]
    assert [(l["item"], l["quantity"], l["unit_price"], l["credit"]) for l in sold] == [
        ("WIDGET", 2, 30.0, 60.0), ("GIZMO", 1, 40.0, 40.0)]
    # The revenue it split is gone, not duplicated: the entry still foots.
    assert sum(l["debit"] for l in entry["lines"]) == sum(l["credit"] for l in entry["lines"]) == 100.0


@pytest.mark.asyncio
async def test_extended_journal_names_the_item_on_each_line_of_a_purchase(client):
    tok = await _reg(client)
    doc = await _doc_with_items(client, tok, "bill",
                                [("BOLT", 10, 1.5), ("NUT", 20, 0.25)])

    entry = _entry_for(await _extended(client, tok), doc)
    assert entry["items_status"] == "expanded"
    bought = [l for l in entry["lines"] if l.get("item")]
    assert [(l["item"], l["quantity"], l["debit"]) for l in bought] == [
        ("BOLT", 10, 15.0), ("NUT", 20, 5.0)]
    # A purchase already posts a line per item, so nothing was split.
    assert len(entry["lines"]) == len(_entry_for(await _journal(client, tok), doc)["lines"])


@pytest.mark.asyncio
async def test_extended_journal_leaves_a_manual_entry_untouched(client):
    tok = await _reg(client)
    je_id = (await _post_manual_je(client, tok, _bal(10.0))).json()["je_id"]

    entry = [e for e in (await _extended(client, tok))["entries"] if e["je_id"] == je_id][0]
    assert entry["items_status"] == "no_items"
    assert all("item" not in l for l in entry["lines"])


@pytest.mark.asyncio
async def test_extended_journal_withholds_item_detail_when_amounts_do_not_tie(client):
    tok = await _reg(client)
    # Shipping is charged on the document but is not one of its lines, so the
    # revenue posted is more than the lines account for.
    doc = await _doc_with_items(client, tok, "invoice", [("WIDGET", 1, 60.0)], shipping=40.0)

    entry = _entry_for(await _extended(client, tok), doc)
    assert entry["items_status"] == "untied"
    assert all("item" not in l for l in entry["lines"])


@pytest.mark.asyncio
async def test_extended_journal_leaves_a_payment_alone(client):
    tok = await _reg(client)
    doc = await _doc_with_items(client, tok, "invoice", [("WIDGET", 1, 100.0)])
    r = await client.post(f"/docs/{doc}/payment", headers=_h(tok), json={
        "payment_date": "2026-02-10", "amount": 100.0, "method": "transfer",
        "bank_account": "1111"})
    assert r.status_code == 200, r.text

    data = await _extended(client, tok)
    pay = [e for e in data["entries"]
           if (e.get("source_doc") or {}).get("doc_id") == doc and e["ts"] == "2026-02-10"]
    assert pay and pay[0]["items_status"] == "no_items"
    assert all("item" not in l for l in pay[0]["lines"])


@pytest.mark.asyncio
async def test_extended_journal_survives_a_deleted_source_document(client, session):
    tok = await _reg(client)
    doc = await _doc_with_items(client, tok, "invoice", [("WIDGET", 1, 100.0)])
    from sqlalchemy import delete
    from celerp.models.projections import Projection
    await session.execute(delete(Projection).where(Projection.entity_id == doc))
    await session.commit()

    entry = _entry_for(await _extended(client, tok), doc)
    assert entry["items_status"] == "no_document"
    assert sum(l["credit"] for l in entry["lines"]) == 100.0


@pytest.mark.asyncio
async def test_extended_journal_foreign_rows_foot_to_the_posting(client):
    tok = await _reg(client)
    await _thb(client, tok)
    # Three thirds of a dollar: converting each and converting their sum differ,
    # which is exactly the case the expanded rows have to absorb.
    doc = await _doc_with_items(client, tok, "invoice",
                                [("A", 1, 33.33), ("B", 1, 33.33), ("C", 1, 33.34)],
                                currency="USD", rate=35.0)

    ext = _entry_for(await _extended(client, tok), doc)
    classical = _entry_for(await _journal(client, tok), doc)
    assert ext["items_status"] == "expanded"
    rows = [l for l in ext["lines"] if l.get("item")]
    assert [l["fx_credit"] for l in rows] == [33.33, 33.33, 33.34]
    posted = [l["credit"] for l in classical["lines"] if l["account"] == "4100"][0]
    assert sum(l["credit"] for l in rows) == posted


@pytest.mark.asyncio
async def test_extended_journal_agrees_with_the_classical_journal(client):
    tok = await _reg(client)
    await _doc_with_items(client, tok, "invoice", [("WIDGET", 2, 30.0), ("GIZMO", 1, 40.0)])
    await _doc_with_items(client, tok, "bill", [("BOLT", 10, 1.5)], issue_date="2026-02-03")
    await _post_manual_je(client, tok, _bal(12.0), ts="2026-02-05")

    classical = await _journal(client, tok)
    extended = await _extended(client, tok)
    assert [e["je_id"] for e in extended["entries"]] == [e["je_id"] for e in classical["entries"]]
    assert extended["total_debit"] == classical["total_debit"]
    assert extended["total_credit"] == classical["total_credit"]


@pytest.mark.asyncio
async def test_extended_journal_requires_view_financial_reports(client, session):
    admin = await _reg(client)
    operator = await _user_with_role(client, session, admin, "operator")
    assert (await client.get("/accounting/extended-journal",
                             headers=_h(operator))).status_code == 403


# ---------------------------------------------------------------------------
# Journal filters
# ---------------------------------------------------------------------------

async def _two_entries(client, tok) -> tuple[str, str]:
    """A receivable sale and an unrelated bank payment, in that order."""
    sale = (await _post_manual_je(client, tok, [
        {"account": "1120", "debit": 40.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 40.0},
    ], ts="2026-01-10", memo="Quarterly retainer")).json()["je_id"]
    bank = (await _post_manual_je(client, tok, [
        {"account": "2110", "debit": 25.0, "credit": 0},
        {"account": "1111", "debit": 0, "credit": 25.0},
    ], ts="2026-01-11", memo="Supplier settled")).json()["je_id"]
    return sale, bank


@pytest.mark.asyncio
async def test_journal_search_by_account_returns_whole_entries(client):
    """An entry is shown whole or not at all: half an entry does not balance."""
    tok = await _reg(client)
    sale, bank = await _two_entries(client, tok)

    data = await _journal(client, tok, q="4100")
    assert [e["je_id"] for e in data["entries"]] == [sale]
    assert bank not in [e["je_id"] for e in data["entries"]]
    assert sorted(l["account"] for l in data["entries"][0]["lines"]) == ["1120", "4100"]
    assert data["filtered"] is True


@pytest.mark.asyncio
async def test_journal_search_totals_are_the_filtered_totals(client):
    """The totals foot to what was shown, so a filtered page cross-foots."""
    tok = await _reg(client)
    await _two_entries(client, tok)

    data = await _journal(client, tok, q="4100")
    assert abs(data["total_debit"] - 40.0) < 0.01
    assert abs(data["total_credit"] - 40.0) < 0.01
    whole = await _journal(client, tok)
    assert abs(whole["total_debit"] - 65.0) < 0.01
    assert whole["filtered"] is False


@pytest.mark.asyncio
async def test_journal_search_reaches_memo_account_and_document(client):
    """One term reaches every place a reader would look for an entry: the memo,
    an account code, an account name, and the source document."""
    tok = await _reg(client)
    sale, bank = await _two_entries(client, tok)
    doc_id = await _doc_with_items(client, tok, "invoice", [("Widget", 1, 30.0)],
                                   issue_date="2026-01-12")
    ref = _entry_for(await _journal(client, tok), doc_id)["source_doc"]["doc_ref"]

    by_memo = await _journal(client, tok, q="RETAINER")
    assert [e["je_id"] for e in by_memo["entries"]] == [sale]

    by_account_name = await _journal(client, tok, q="accounts payable")
    assert [e["je_id"] for e in by_account_name["entries"]] == [bank]

    by_account_code = await _journal(client, tok, q="2110")
    assert [e["je_id"] for e in by_account_code["entries"]] == [bank]

    by_doc = await _journal(client, tok, q=ref.lower())
    assert [(e.get("source_doc") or {}).get("doc_id") for e in by_doc["entries"]] == [doc_id]


@pytest.mark.asyncio
async def test_journal_search_by_amount_matches_either_side(client):
    """A reader chasing a figure does not know which side it was posted on."""
    tok = await _reg(client)
    sale, bank = await _two_entries(client, tok)

    assert [e["je_id"] for e in (await _journal(client, tok, q="40"))["entries"]] == [sale]
    assert [e["je_id"] for e in (await _journal(client, tok, q="25.00"))["entries"]] == [bank]
    none = await _journal(client, tok, q="99")
    assert none["entries"] == []
    assert none["total_debit"] == 0
    assert none["filtered"] is True


@pytest.mark.asyncio
async def test_journal_search_commas_are_or(client):
    """A comma widens the search: each term is tried on its own, the way the
    inventory box reads a run of scanned codes."""
    tok = await _reg(client)
    sale, bank = await _two_entries(client, tok)

    both = await _journal(client, tok, q="4100, 2110")
    assert [e["je_id"] for e in both["entries"]] == [sale, bank]
    # A text term and a figure term in the same box, still an OR.
    mixed = await _journal(client, tok, q="settled, 40")
    assert [e["je_id"] for e in mixed["entries"]] == [sale, bank]
    # One term alone still narrows to what it matches.
    assert [e["je_id"] for e in (await _journal(client, tok, q="4100"))["entries"]] == [sale]


@pytest.mark.asyncio
async def test_blank_search_is_no_filter(client):
    """Clearing the box submits it empty, which is not an invalid value, and a
    stray comma leaves no term rather than matching everything."""
    tok = await _reg(client)
    await _two_entries(client, tok)

    for blank in ("", "  ", ",", " , "):
        data = await _journal(client, tok, q=blank)
        assert len(data["entries"]) == 2, blank
        assert data["filtered"] is False, blank


@pytest.mark.asyncio
async def test_journal_search_unmatched_term_is_empty_not_refused(client):
    """A term that matches nothing answers with an empty, clearly filtered book,
    not a refusal: any string is a valid thing to type into a search box."""
    tok = await _reg(client)
    await _two_entries(client, tok)

    for term in ("9999", "abc", "-5"):
        data = await _journal(client, tok, q=term)
        assert data["entries"] == [], term
        assert data["filtered"] is True, term


@pytest.mark.asyncio
async def test_journal_search_cap_is_422(client):
    """The search is capped, and the refusal says what the cap is."""
    tok = await _reg(client)
    r = await client.get("/accounting/journal", headers=_h(tok), params={"q": "x" * 201})
    assert r.status_code == 422, r.text
    assert "200" in r.json()["detail"]
    assert (await client.get("/accounting/journal", headers=_h(tok),
                             params={"q": "x" * 200})).status_code == 200


@pytest.mark.asyncio
async def test_extended_journal_applies_the_same_search(client):
    """The twin narrows identically, or the two books disagree under a search."""
    tok = await _reg(client)
    sale, _bank = await _two_entries(client, tok)

    data = await _extended(client, tok, q="4100")
    assert [e["je_id"] for e in data["entries"]] == [sale]
    assert data["filtered"] is True
    classical = await _journal(client, tok, q="4100")
    assert [e["je_id"] for e in data["entries"]] == [e["je_id"] for e in classical["entries"]]
    assert data["total_debit"] == classical["total_debit"]
    assert data["total_credit"] == classical["total_credit"]

    blank = await _extended(client, tok, q="")
    assert len(blank["entries"]) == 2
    assert blank["filtered"] is False


@pytest.mark.asyncio
async def test_journal_search_keeps_voids_out_of_the_totals(client):
    """A search changes which entries are shown, not how a void is counted."""
    tok = await _reg(client)
    sale, _bank = await _two_entries(client, tok)
    assert (await client.post(f"/accounting/journal-entries/{sale}/void",
                              headers=_h(tok),
                              json={"reason": "Duplicated"})).status_code == 200

    data = await _journal(client, tok, q="4100")
    assert [e["je_id"] for e in data["entries"]] == [sale]
    assert data["entries"][0]["status"] == "void"
    assert data["total_debit"] == 0
    assert data["total_credit"] == 0


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
    assert r.status_code == 200, r.text

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
    assert r.status_code == 200, r.text

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

    # The journal renders the same entry with the account-less line skipped
    # rather than crashing. This endpoint did not exist before this change, so
    # the assertion is red against the pre-change tree.
    entries = (await _journal(client, tok))["entries"]
    assert len(entries) == 1
    assert [l["account"] for l in entries[0]["lines"]] == ["1111", "4100"]


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
# Party subledgers: contact filtering on the ledger and the general ledger
# ---------------------------------------------------------------------------

async def _finalized_invoice(client, tok, contact, total, issue_date):
    """An invoice that has reached the books, so its JE carries the party."""
    doc = await _invoice(client, tok, total=total, contact_id=contact, issue_date=issue_date)
    r = await client.post(f"/docs/{doc}/finalize", headers=_h(tok))
    assert r.status_code == 200, r.text
    return doc


async def _ledger(client, tok, code, **params):
    r = await client.get(f"/accounting/ledger/{code}", headers=_h(tok), params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _gl_rows(client, tok, **params):
    r = await client.get("/accounting/general-ledger", headers=_h(tok), params=params)
    assert r.status_code == 200, r.text
    data = r.json()
    return data, {row["code"]: row for row in data["rows"]}


@pytest.mark.asyncio
async def test_ledger_rejects_a_contact_filter_that_matches_no_contact(client):
    """A mistyped contact must not read as "this customer owes nothing".

    The contact filter is what turns a control account into a subledger, so an
    id that matches nothing has to be refused rather than answered with an
    empty statement the reader cannot tell from a settled account.
    """
    tok = await _reg(client)
    r = await client.get("/accounting/ledger/1120", headers=_h(tok),
                         params={"contact_id": "contact:not-a-real-id"})
    assert r.status_code == 422, r.text
    assert "contact_id" in r.json()["detail"]


@pytest.mark.asyncio
async def test_ledger_reports_lines_with_no_party_under_the_empty_contact(client):
    """Every line on the control account belongs to exactly one bucket: a
    contact, or the unattributed one. The buckets must sum to the account."""
    tok = await _reg(client)
    contact = await _contact(client, tok, name="Alpha")
    await _finalized_invoice(client, tok, contact, 100.0, "2026-01-05")
    await _post_manual_je(client, tok, [
        {"account": "1120", "debit": 25.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 25.0},
    ], ts="2026-01-07")

    everything = await _ledger(client, tok, "1120")
    party = await _ledger(client, tok, "1120", contact_id=contact)
    unattributed = await _ledger(client, tok, "1120", contact_id="")

    assert abs(party["closing_balance"] - 100.0) < 0.01
    assert abs(unattributed["closing_balance"] - 25.0) < 0.01
    assert abs(party["closing_balance"] + unattributed["closing_balance"]
               - everything["closing_balance"]) < 0.01


@pytest.mark.asyncio
async def test_general_ledger_filters_by_contact(client):
    """The same filter as the account ledger, on the report it drills down from,
    and the two agree account by account."""
    tok = await _reg(client)
    alpha = await _contact(client, tok, name="Alpha")
    beta = await _contact(client, tok, name="Beta")
    await _finalized_invoice(client, tok, alpha, 100.0, "2026-01-05")
    await _finalized_invoice(client, tok, beta, 250.0, "2026-01-06")

    data, rows = await _gl_rows(client, tok, contact_id=alpha)
    assert data["contact_id"] == alpha
    assert abs(rows["1120"]["closing"] - 100.0) < 0.01
    assert abs(rows["4100"]["closing"] - 100.0) < 0.01

    led = await _ledger(client, tok, "1120", contact_id=alpha)
    assert abs(led["closing_balance"] - rows["1120"]["closing"]) < 0.01


@pytest.mark.asyncio
async def test_general_ledger_contact_filter_surfaces_unattributed_lines(client):
    """A filtered general ledger never quietly loses a line: the per-contact
    views plus the unattributed view add back up to the unfiltered report."""
    tok = await _reg(client)
    alpha = await _contact(client, tok, name="Alpha")
    beta = await _contact(client, tok, name="Beta")
    await _finalized_invoice(client, tok, alpha, 100.0, "2026-01-05")
    await _finalized_invoice(client, tok, beta, 250.0, "2026-01-06")
    await _post_manual_je(client, tok, [
        {"account": "1120", "debit": 25.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 25.0},
    ], ts="2026-01-07")

    _, everything = await _gl_rows(client, tok)
    _, for_alpha = await _gl_rows(client, tok, contact_id=alpha)
    _, for_beta = await _gl_rows(client, tok, contact_id=beta)
    _, unattributed = await _gl_rows(client, tok, contact_id="")

    assert abs(unattributed["1120"]["closing"] - 25.0) < 0.01
    parts = sum(part["1120"]["closing"] for part in (for_alpha, for_beta, unattributed))
    assert abs(parts - everything["1120"]["closing"]) < 0.01


@pytest.mark.asyncio
async def test_general_ledger_rejects_a_contact_filter_that_matches_no_contact(client):
    tok = await _reg(client)
    r = await client.get("/accounting/general-ledger", headers=_h(tok),
                         params={"contact_id": "contact:not-a-real-id"})
    assert r.status_code == 422, r.text
    assert "contact_id" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Account codes with no chart entry
# ---------------------------------------------------------------------------

async def _import_je(client, tok, entries, ts="2026-01-10", fx=None):
    """Post straight to the projection, the only way a code with no chart entry
    gets a posted line (manual entry refuses an account that does not exist).

    `fx` writes the entry-level shape used before currency moved to the line,
    which is the only way to produce one now that the writer is gone.
    """
    data = {"status": "posted", "ts": ts, "entries": entries}
    if fx is not None:
        data["fx"] = fx
    r = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": f"je:{uuid.uuid4()}",
        "event_type": "acc.journal_entry.created",
        "data": data,
        "source": "test", "idempotency_key": str(uuid.uuid4()),
    }]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text


@pytest.mark.asyncio
async def test_ledger_opens_a_posted_code_that_has_no_account_row(client):
    """The general ledger lists such a code, so its drilldown must open. The
    type is reported as unknown rather than guessed."""
    tok = await _reg(client)
    await _import_je(client, tok, [
        {"account": "9999", "debit": 70.0, "credit": 0},
        {"account": "3200", "debit": 0, "credit": 70.0},
    ])

    led = await _ledger(client, tok, "9999")
    assert led["account_type"] == "unknown"
    assert abs(led["closing_balance"] - 70.0) < 0.01


@pytest.mark.asyncio
async def test_ledger_and_general_ledger_agree_on_the_normal_side(client):
    """One debit-normal rule for both endpoints: a drilldown can never
    contradict the sign of the report row it was opened from.

    Both accounts here sit outside the five everyday types: "other" is a type
    the chart importer accepts, and 9999 has no chart entry at all.
    """
    tok = await _reg(client)
    r = await client.post("/accounting/accounts", headers=_h(tok), json={
        "code": "9100", "name": "Suspense", "account_type": "other"})
    assert r.status_code == 200, r.text
    await _import_je(client, tok, [
        {"account": "9100", "debit": 0, "credit": 40.0},
        {"account": "9999", "debit": 40.0, "credit": 0},
    ])

    _, rows = await _gl_rows(client, tok)
    for code in ("9100", "9999"):
        led = await _ledger(client, tok, code)
        assert abs(led["closing_balance"] - rows[code]["closing"]) < 0.01, code
        assert led["account_type"] == rows[code]["account_type"], code
        assert led["debit_normal"] is rows[code]["debit_normal"], code


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


@pytest.mark.asyncio
async def test_close_year_rerun_posts_residual(client):
    """Re-closing a year after an unlock-edit cycle posts a NEW residual
    closing entry instead of silently deduping into the first close."""
    tok = await _reg(client)
    await _post_manual_je(client, tok, [
        {"account": "1111", "debit": 500.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 500.0},
    ], ts="2026-03-01")
    r = await client.post("/accounting/close-year", headers=_h(tok),
                          json={"fiscal_year_end": "2026-12-31"})
    assert r.status_code == 200, r.text

    # Unlock, backdate more revenue into the closed year, re-close.
    await client.post("/accounting/period-lock", headers=_h(tok), json={"lock_date": None})
    r = await _post_manual_je(client, tok, [
        {"account": "1111", "debit": 200.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 200.0},
    ], ts="2026-06-01")
    assert r.status_code == 200, r.text
    r = await client.post("/accounting/close-year", headers=_h(tok),
                          json={"fiscal_year_end": "2026-12-31"})
    assert r.status_code == 200, r.text

    tb = (await client.get("/accounting/trial-balance", headers=_h(tok),
                           params={"date_to": "2027-01-01"})).json()
    nets = {l["code"]: l["net"] for l in tb["lines"]}
    assert abs(nets.get("4100", 0.0)) < 0.01  # revenue fully closed after the re-run
    assert abs(nets.get("3200", 0.0) + 700.0) < 0.01  # both closes in retained earnings


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


def _dated_report_paths(contact: str) -> list[str]:
    """Every report endpoint that takes a date_from/date_to pair.

    One list, so a date filter that is refused on one report is refused on all
    of them and the tests below cannot drift apart from each other.
    """
    return [
        "/accounting/journal",
        "/accounting/ledger/4100",
        "/accounting/trial-balance",
        "/accounting/general-ledger",
        "/accounting/pnl",
        "/accounting/cash-flow",
        f"/accounting/soa/{contact}",
    ]


@pytest.mark.parametrize("param", ["date_from", "date_to"])
@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-45", "20260105", "2026-W01"])
async def test_report_date_filters_reject_unparsable_dates(client, param, bad):
    """An unparsable filter is refused, not answered with an empty report.

    Silently returning zero rows reads as "no activity in this period" when it
    really means the question was malformed, and an accountant cannot tell the
    two apart from the screen.
    """
    tok = await _reg(client)
    await _post_manual_je(client, tok, _bal(100.0))
    contact = await _contact(client, tok)

    for path in _dated_report_paths(contact):
        r = await client.get(path, headers=_h(tok), params={param: bad})
        assert r.status_code == 422, f"{path} accepted {bad!r}: {r.text}"
        assert param in r.json()["detail"]


async def test_report_date_filters_reject_an_inverted_range(client):
    """A start date after the end date is refused for the same reason a
    malformed one is: it can only ever produce an empty report, and an empty
    report reads as a settled, quiet period rather than as a bad question.
    """
    tok = await _reg(client)
    await _post_manual_je(client, tok, _bal(100.0))
    contact = await _contact(client, tok)

    for path in _dated_report_paths(contact):
        r = await client.get(path, headers=_h(tok), params={
            "date_from": "2026-12-31", "date_to": "2026-01-01"})
        assert r.status_code == 422, f"{path} accepted an inverted range: {r.text}"
        detail = r.json()["detail"]
        assert "date_from" in detail and "date_to" in detail, detail


async def test_report_date_filters_accept_valid_dates(client):
    """The strict check does not reject the dates the UI actually sends,
    including a single-day range where the two ends are equal."""
    tok = await _reg(client)
    await _post_manual_je(client, tok, _bal(100.0))
    contact = await _contact(client, tok)

    for path in _dated_report_paths(contact):
        r = await client.get(path, headers=_h(tok),
                             params={"date_from": "2026-01-01", "date_to": "2026-12-31"})
        assert r.status_code == 200, f"{path}: {r.text}"
        same_day = await client.get(path, headers=_h(tok),
                                    params={"date_from": "2026-01-15", "date_to": "2026-01-15"})
        assert same_day.status_code == 200, f"{path}: {same_day.text}"


# --- Foreign-currency manual journal entries (J2) ---------------------------


async def test_manual_je_line_fx_converts_to_base_and_keeps_the_typed_figures(client):
    """The author types foreign amounts on a line; the server stores base
    currency and keeps the typed figures so an auditor can tie them back."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "USD", 35.0),
        _fx({"account": "4100", "debit": 0, "credit": 10.0}, "USD", 35.0),
    ])
    assert r.status_code == 200, r.text

    entry = (await _journal(client, tok))["entries"][0]
    debit_line = next(l for l in entry["lines"] if l["debit"])
    credit_line = next(l for l in entry["lines"] if l["credit"])
    assert debit_line["debit"] == 350.0 and debit_line["fx_debit"] == 10.0
    assert credit_line["credit"] == 350.0 and credit_line["fx_credit"] == 10.0
    assert debit_line["fx_currency"] == "USD" and debit_line["fx_rate"] == 35.0


async def test_manual_je_accepts_rate_below_one_ten_thousandth(client):
    """A rate far below 0.0001 posts and reads back at the precision typed.

    1 LBP is about 0.00001117318 THB. A four-place floor would have refused this
    rate or flattened it to 0.0000, and the base figures derived from it would
    have been wrong by orders of magnitude.
    """
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10_000_000.0, "credit": 0}, "LBP", 0.00001117318),
        {"account": "4100", "debit": 0, "credit": 111.73},
    ])
    assert r.status_code == 200, r.text

    entry = (await _journal(client, tok))["entries"][0]
    fx_line = next(l for l in entry["lines"] if l["debit"])
    assert fx_line["fx_rate"] == 0.00001117318, "the rate must survive the round trip intact"
    assert fx_line["fx_debit"] == 10_000_000.0
    # 10,000,000 LBP * 0.00001117318 = 111.7318, at THB's two places.
    assert fx_line["debit"] == 111.73


async def test_manual_je_carries_a_different_currency_on_each_line(client):
    """One entry, two currencies. Both sides convert to base and balance there,
    which is the whole reason currency belongs to the line."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "USD", 35.0),
        _fx({"account": "4100", "debit": 0, "credit": 100.0}, "CNY", 3.5),
    ])
    assert r.status_code == 200, r.text

    entry = (await _journal(client, tok))["entries"][0]
    by_account = {l["account"]: l for l in entry["lines"]}
    assert by_account["1111"]["fx_currency"] == "USD"
    assert by_account["4100"]["fx_currency"] == "CNY"
    assert by_account["1111"]["debit"] == by_account["4100"]["credit"] == 350.0


async def test_manual_je_mixes_a_foreign_line_with_a_base_currency_line(client):
    """A line with no currency is a base-currency line and stays annotation-free,
    beside a foreign one in the same entry."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "USD", 35.0),
        {"account": "4100", "debit": 0, "credit": 350.0},
    ])
    assert r.status_code == 200, r.text

    entry = (await _journal(client, tok))["entries"][0]
    plain = next(l for l in entry["lines"] if l["account"] == "4100")
    assert plain["fx_currency"] is None and plain["fx_rate"] is None
    assert plain["fx_debit"] is None and plain["fx_credit"] is None


async def test_manual_je_line_in_base_currency_at_rate_one_is_accepted(client):
    """Naming the company currency at par says nothing new, so it is not an
    error; it simply carries no annotation."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "THB", 1.0),
        {"account": "4100", "debit": 0, "credit": 10.0},
    ])
    assert r.status_code == 200, r.text

    entry = (await _journal(client, tok))["entries"][0]
    assert all(l["fx_currency"] is None for l in entry["lines"])


async def test_manual_je_line_in_base_currency_at_another_rate_rejected(client):
    """A rate against the company's own currency is a mistake, and the message
    names the line so the author knows which one."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "THB", 2.0),
        {"account": "4100", "debit": 0, "credit": 20.0},
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "THB" in detail and "Line 1" in detail


async def test_manual_je_line_invalid_currency_code_rejected(client):
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "QQQ", 2.0),
        {"account": "4100", "debit": 0, "credit": 20.0},
    ])
    assert r.status_code == 422
    assert "QQQ" in r.json()["detail"]


@pytest.mark.parametrize("rate", [0, -5.0])
async def test_manual_je_line_nonpositive_rate_rejected(client, rate):
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "USD", rate),
        {"account": "4100", "debit": 0, "credit": 10.0},
    ])
    assert r.status_code == 422
    assert "rate" in r.json()["detail"].lower()


async def test_manual_je_line_currency_without_a_rate_rejected(client):
    """A currency with no rate is incomplete, not a default of 1.0."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        {"account": "1111", "debit": 10.0, "credit": 0, "currency": "USD"},
        {"account": "4100", "debit": 0, "credit": 10.0},
    ])
    assert r.status_code == 422
    assert "rate" in r.json()["detail"].lower()


async def test_manual_je_line_rate_without_a_currency_rejected(client):
    """A rate on its own says nothing: a bare number cannot convert an amount
    from a currency nobody named."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        {"account": "1111", "debit": 10.0, "credit": 0, "rate": 35.0},
        {"account": "4100", "debit": 0, "credit": 10.0},
    ])
    assert r.status_code == 422
    assert "currency" in r.json()["detail"].lower()


async def test_manual_je_balance_is_checked_in_base_currency(client):
    """Balance is judged on the converted figures, because those are what the
    books hold, and the message names the base currency and the short side."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "USD", 35.0),
        _fx({"account": "4100", "debit": 0, "credit": 9.0}, "USD", 35.0),
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "THB" in detail and "credit" in detail


async def test_manual_je_conversion_difference_is_refused_not_plugged(client):
    """Lines that balance in the foreign currency can convert to base amounts
    that do not. The author decides where that difference belongs, so the entry
    comes back with the gap named rather than a line appearing on its own."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "6100", "debit": 33.33, "credit": 0}, "USD", 3.0025),
        _fx({"account": "6200", "debit": 33.33, "credit": 0}, "USD", 3.0025),
        _fx({"account": "6300", "debit": 33.34, "credit": 0}, "USD", 3.0025),
        _fx({"account": "1111", "debit": 0, "credit": 100.00}, "USD", 3.0025),
    ])
    # 33.33 and 33.34 convert to 100.07, 100.07 and 100.10 = 300.24, while the
    # 100.00 credit converts to 300.25: one satang the debit side is short of.
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "0.01" in detail and "debit" in detail
    assert (await _journal(client, tok))["entries"] == []


async def test_manual_je_the_author_posts_the_difference_themselves(client):
    """The same entry with the difference written as its own line goes through,
    and that line is the author's, on the account they chose."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "6100", "debit": 33.33, "credit": 0}, "USD", 3.0025),
        _fx({"account": "6200", "debit": 33.33, "credit": 0}, "USD", 3.0025),
        _fx({"account": "6300", "debit": 33.34, "credit": 0}, "USD", 3.0025),
        {"account": "6960", "debit": 0.01, "credit": 0},
        _fx({"account": "1111", "debit": 0, "credit": 100.00}, "USD", 3.0025),
    ])
    assert r.status_code == 200, r.text

    j = await _journal(client, tok)
    entry = j["entries"][0]
    assert len(entry["lines"]) == 5
    difference = next(l for l in entry["lines"] if l["account"] == "6960")
    assert difference["debit"] == 0.01
    # A base-currency line: no foreign figure, because none was typed.
    assert difference["fx_debit"] is None and difference["fx_currency"] is None
    assert j["total_debit"] == j["total_credit"] == 300.25


async def test_manual_je_nothing_is_posted_to_the_difference_account_on_its_own(client):
    """Nothing writes to 6960 automatically any more, so an entry that balances
    in base currency posts exactly the lines it was given."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 50.0, "credit": 0}, "USD", 2.0),
        _fx({"account": "4100", "debit": 0, "credit": 50.0}, "USD", 2.0),
    ])
    assert r.status_code == 200, r.text

    entry = (await _journal(client, tok))["entries"][0]
    assert len(entry["lines"]) == 2
    assert entry["lines"][0]["debit"] == 100.0
    assert all(l["account"] != "6960" for l in entry["lines"])


async def test_manual_je_line_respects_its_own_currency_decimal_places(client):
    """JPY has no minor unit, so a yen line quantizes to whole yen before it
    converts, at the same place the currency itself uses."""
    tok = await _reg(client)
    await _thb(client, tok)
    r = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 100.5, "credit": 0}, "JPY", 0.25),
        {"account": "4100", "debit": 0, "credit": 25.25},
    ])
    assert r.status_code == 200, r.text

    line = next(l for l in (await _journal(client, tok))["entries"][0]["lines"]
                if l["account"] == "1111")
    # 100.5 rounds to 101 yen, and 101 at 0.25 is 25.25 base.
    assert line["fx_debit"] == 101.0 and line["debit"] == 25.25


async def test_manual_je_token_reuse_with_a_different_rate_conflicts(client):
    """Same amounts at a different rate is a different entry, not a retry."""
    tok = await _reg(client)
    await _thb(client, tok)
    token = uuid.uuid4().hex
    first = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "USD", 35.0),
        _fx({"account": "4100", "debit": 0, "credit": 10.0}, "USD", 35.0),
    ], token=token)
    assert first.status_code == 200, first.text
    second = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "USD", 36.0),
        _fx({"account": "4100", "debit": 0, "credit": 10.0}, "USD", 36.0),
    ], token=token)
    assert second.status_code == 409, second.text


async def test_manual_je_token_reuse_conflicts_when_rate_rounds_to_the_same_base(client):
    """Two rates can convert to identical base figures. Comparing only those
    would read a corrected rate as a retry and silently post nothing."""
    tok = await _reg(client)
    await _thb(client, tok)
    token = uuid.uuid4().hex
    first = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 1.0, "credit": 0}, "USD", 35.001),
        _fx({"account": "4100", "debit": 0, "credit": 1.0}, "USD", 35.001),
    ], token=token)
    assert first.status_code == 200, first.text

    second = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 1.0, "credit": 0}, "USD", 35.004),
        _fx({"account": "4100", "debit": 0, "credit": 1.0}, "USD", 35.004),
    ], token=token)
    assert second.status_code == 409, second.text


async def test_seed_chart_includes_the_fx_difference_account(client):
    tok = await _reg(client)
    chart = (await client.get("/accounting/chart", headers=_h(tok))).json()
    items = chart["items"] if isinstance(chart, dict) else chart
    row = next(a for a in items if a["code"] == "6960")
    assert row["name"] == "Foreign Exchange Difference"
    assert row["account_type"] == "expense" and row["parent_code"] == "6000"


async def test_seed_chart_endpoint_backfills_the_fx_difference_account(client):
    """Re-seeding an existing chart adds 6960 without disturbing the rest."""
    tok = await _reg(client)
    r = await client.post("/accounting/chart/seed", headers=_h(tok))
    assert r.status_code in (200, 409), r.text

    chart = (await client.get("/accounting/chart", headers=_h(tok))).json()
    items = chart["items"] if isinstance(chart, dict) else chart
    row = next(a for a in items if a["code"] == "6960")
    assert row["account_type"] == "expense" and row["parent_code"] == "6000"


async def test_journal_reads_pre_change_entry_level_fx(client):
    """An entry posted before currency moved to the line still reads correctly.

    Stored events are immutable, so entries written in the old shape carry one
    `fx` for the whole entry. Their lines inherit it on read; nothing rewrites
    the event.
    """
    tok = await _reg(client)
    await _thb(client, tok)
    await _import_je(client, tok, [
        {"account": "1111", "debit": 350.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 350.0},
    ], fx={"currency": "USD", "rate": 35.0})

    entry = (await _journal(client, tok))["entries"][0]
    assert all(l["fx_currency"] == "USD" for l in entry["lines"])
    assert all(l["fx_rate"] == 35.0 for l in entry["lines"])


async def test_manual_je_without_fx_is_unchanged(client):
    """J1: an ordinary entry carries no currency, no rate and no foreign
    amounts at all."""
    tok = await _reg(client)
    r = await _post_manual_je(client, tok, _bal(100.0))
    assert r.status_code == 200, r.text

    entry = (await _journal(client, tok))["entries"][0]
    assert entry["fx"] is None
    assert all(l["fx_debit"] is None and l["fx_credit"] is None
               and l["fx_currency"] is None and l["fx_rate"] is None
               for l in entry["lines"])


async def test_void_manual_je_preserves_the_stored_line_fx(client):
    """Voiding removes the entry from the books but not from the record: the
    foreign amounts, currency and rate stay for the audit trail."""
    tok = await _reg(client)
    await _thb(client, tok)
    posted = await _post_manual_je(client, tok, [
        _fx({"account": "1111", "debit": 10.0, "credit": 0}, "USD", 35.0),
        _fx({"account": "4100", "debit": 0, "credit": 10.0}, "USD", 35.0),
    ])
    je_id = posted.json()["je_id"]

    v = await client.post(f"/accounting/journal-entries/{je_id}/void", headers=_h(tok),
                          json={"reason": "keyed twice"})
    assert v.status_code == 200, v.text

    entry = next(e for e in (await _journal(client, tok))["entries"] if e["je_id"] == je_id)
    assert entry["status"] == "void"
    line = next(l for l in entry["lines"] if l["fx_debit"])
    assert line["fx_debit"] == 10.0 and line["fx_currency"] == "USD"
    # Out of the books: a voided entry contributes nothing to the totals.
    assert (await _journal(client, tok))["total_debit"] == 0.0


async def test_an_entry_posted_before_per_line_currency_still_reads_back(client, session):
    """Stored events are immutable, so an entry recorded when the currency and
    rate belonged to the whole entry has to render now. Its lines inherit it."""
    from celerp.events.engine import emit_event
    from celerp.models.company import Company
    from sqlalchemy import select as _select

    tok = await _reg(client)
    await _thb(client, tok)
    company_id = (await session.execute(_select(Company.id))).scalars().first()
    je_id = f"je:manual:{uuid.uuid4().hex}"
    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={
            "ts": "2026-01-15", "memo": "Historic", "status": "posted", "je_type": "manual",
            "entries": [
                {"account": "1111", "debit": 350.0, "credit": 0.0, "fx_debit": 10.0},
                {"account": "4100", "debit": 0.0, "credit": 350.0, "fx_credit": 10.0},
            ],
            "fx": {"currency": "USD", "rate": 35.0},
        },
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=uuid.uuid4().hex,
        metadata_={},
    )
    await session.commit()

    entry = next(e for e in (await _journal(client, tok))["entries"] if e["je_id"] == je_id)
    assert all(l["fx_currency"] == "USD" and l["fx_rate"] == 35.0 for l in entry["lines"])
    assert next(l for l in entry["lines"] if l["debit"])["fx_debit"] == 10.0


async def test_cross_report_consistency_with_line_fx(client):
    """The journal, trial balance, and general ledger agree once an entry with
    foreign lines and an author-written difference is in the period."""
    tok = await _reg(client)
    await _thb(client, tok)
    await _post_manual_je(client, tok, _bal(100.0), ts="2026-01-10")
    r = await _post_manual_je(client, tok, [
        _fx({"account": "6100", "debit": 33.33, "credit": 0}, "USD", 3.0025),
        _fx({"account": "6200", "debit": 33.33, "credit": 0}, "USD", 3.0025),
        _fx({"account": "6300", "debit": 33.34, "credit": 0}, "USD", 3.0025),
        {"account": "6960", "debit": 0.01, "credit": 0},
        _fx({"account": "1111", "debit": 0, "credit": 100.00}, "USD", 3.0025),
    ], ts="2026-01-11")
    assert r.status_code == 200, r.text

    journal = await _journal(client, tok, date_from="2026-01-01", date_to="2026-12-31")
    tb = (await client.get("/accounting/trial-balance", headers=_h(tok),
                           params={"date_to": "2026-12-31"})).json()
    gl = (await client.get("/accounting/general-ledger", headers=_h(tok),
                           params={"date_to": "2026-12-31"})).json()

    assert abs(journal["total_debit"] - tb["total_debit"]) < 0.01
    assert abs(journal["total_credit"] - tb["total_credit"]) < 0.01
    assert gl["balanced"] is True

    tb_net = {l["code"]: l["net"] for l in tb["lines"]}
    assert abs(tb_net["6960"] - 0.01) < 0.001, "the difference line must reach the trial balance"
    for row in gl["rows"]:
        signed = row["closing"]
        if row["account_type"] not in ("asset", "expense", "cogs"):
            signed = -signed
        assert abs(signed - tb_net.get(row["code"], 0.0)) < 0.01, row["code"]

    fx_entry = next(e for e in journal["entries"] if e["ts"] == "2026-01-11")
    assert next(l for l in fx_entry["lines"] if l["account"] == "6100")["fx_debit"] == 33.33


async def test_creating_an_account_with_an_unknown_type_is_rejected(client):
    """An account type the reports cannot classify has no honest sign convention,
    so it is refused at the door rather than reported as something it is not."""
    tok = await _reg(client)
    r = await client.post("/accounting/accounts", json={
        "code": "1191", "name": "Odd", "account_type": "liabilty"}, headers=_h(tok))
    assert r.status_code == 422, r.text
    assert "asset" in r.json()["detail"] and "liability" in r.json()["detail"]


async def test_patching_an_account_to_an_unknown_type_is_rejected(client):
    tok = await _reg(client)
    r = await client.patch("/accounting/accounts/1111",
                           json={"account_type": "cash"}, headers=_h(tok))
    assert r.status_code == 422, r.text
    assert "cash" not in r.json()["detail"]


async def test_the_balance_sheet_rejects_an_unparsable_as_of(client):
    """as_of silently ignored means a balance sheet for the wrong date, reported
    with the same confidence as a right one."""
    tok = await _reg(client)
    r = await client.get("/accounting/balance-sheet?as_of=2026-W01", headers=_h(tok))
    assert r.status_code == 422, r.text
    assert "as_of" in r.json()["detail"]


def test_the_screen_offers_exactly_the_account_types_the_api_accepts():
    """The chart screen and the API run in separate processes, so the two copies
    of this list can drift silently. The API is the authoritative side."""
    from celerp_accounting.routes import ACCOUNT_TYPES
    from ui.routes.accounting_import import ACCOUNT_TYPES as OFFERED
    assert set(OFFERED) == set(ACCOUNT_TYPES)


# ---------------------------------------------------------------------------
# The statement as a subledger: party on a line, and the tie-out that proves it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_manual_entry_named_to_a_party_reaches_that_party_statement(client):
    """The point of the line-level contact. An adjustment posted straight to the
    receivable control account is real money the customer owes, and before the
    line could name a party it could only ever sit in the unattributed bucket
    while the customer's own statement said nothing about it."""
    tok = await _reg(client)
    contact = await _contact(client, tok, name="Adjusted Co")

    r = await _post_manual_je(client, tok, [
        {"account": "1120", "debit": 45.0, "credit": 0, "contact": contact},
        {"account": "4100", "debit": 0, "credit": 45.0},
    ], ts="2026-03-05", memo="Rebill of courier cost")
    assert r.status_code == 200, r.text
    je_id = r.json()["je_id"]

    soa = (await client.get(f"/accounting/soa/{contact}", headers=_h(tok))).json()
    assert abs(soa["closing_balance"] - 45.0) < 0.01
    row = [x for x in soa["rows"] if x["je_id"] == je_id]
    assert len(row) == 1, f"the entry is missing from the statement: {soa['rows']}"
    assert row[0]["kind"] == "journal"
    assert abs(row[0]["debit"] - 45.0) < 0.01

    # And the same line under the control account's own party filter.
    party = await _ledger(client, tok, "1120", contact_id=contact)
    assert abs(party["closing_balance"] - 45.0) < 0.01


@pytest.mark.asyncio
async def test_an_entry_with_no_party_stays_off_statements_and_in_the_unattributed_bucket(client):
    """Nothing is silently attributed. An entry nobody named belongs to no
    statement, and it stays visible on the control account so the difference
    between the statements and the balance sheet is always explainable."""
    tok = await _reg(client)
    contact = await _contact(client, tok, name="Named Co")

    assert (await _post_manual_je(client, tok, [
        {"account": "1120", "debit": 30.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 30.0},
    ], ts="2026-03-06")).status_code == 200

    soa = (await client.get(f"/accounting/soa/{contact}", headers=_h(tok))).json()
    assert soa["rows"] == []
    assert soa["closing_balance"] == 0

    unattributed = await _ledger(client, tok, "1120", contact_id="")
    assert abs(unattributed["closing_balance"] - 30.0) < 0.01


async def _issued_credit_note(client, tok, total, issue_date, contact=None) -> str:
    payload = {
        "doc_type": "credit_note", "issue_date": issue_date,
        "line_items": [{"name": "Returned goods", "quantity": 1,
                        "unit_price": total, "line_total": total}],
        "subtotal": total, "tax": 0.0, "total": total,
    }
    if contact:
        payload["contact_id"] = contact
    r = await client.post("/docs", headers=_h(tok), json=payload)
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    assert (await client.post(f"/docs/{cn}/finalize", headers=_h(tok))).status_code == 200
    return cn


@pytest.mark.asyncio
async def test_a_credit_note_is_attributed_to_its_own_party_not_the_invoiced_one(client):
    """A credit note never puts its rows on a party it does not belong to.

    Two doors keep that true, and both are checked here. Applying a credit note
    to another party's invoice is refused outright, so the mismatch cannot be
    created. And where the note names nobody, its application is attributed to
    nobody rather than inheriting the invoice's party, which is what would put a
    credit somebody else's on a reader's statement.

    The amounts are a transfer within receivables and net to nothing, so no
    balance moves either way. It is the rows that are wrong, and a reader
    querying a credit they never received is a conversation the books should
    never have started.
    """
    tok = await _reg(client)
    invoiced = await _contact(client, tok, name="Invoiced Co")
    other = await _contact(client, tok, name="Other Co")

    invoice = await _finalized_invoice(client, tok, invoiced, 100.0, "2026-01-05")

    mismatched = await _issued_credit_note(client, tok, 40.0, "2026-01-20", contact=other)
    r = await client.post(f"/docs/{mismatched}/apply-to-invoice", headers=_h(tok),
                          json={"target_doc_id": invoice, "amount": 40.0, "date": "2026-01-21"})
    assert r.status_code == 422, r.text
    assert "same contact" in r.json()["detail"]

    unnamed = await _issued_credit_note(client, tok, 40.0, "2026-01-20")
    r = await client.post(f"/docs/{unnamed}/apply-to-invoice", headers=_h(tok),
                          json={"target_doc_id": invoice, "amount": 40.0, "date": "2026-01-21"})
    assert r.status_code == 200, r.text

    soa = (await client.get(f"/accounting/soa/{invoiced}", headers=_h(tok))).json()
    assert all(unnamed not in str(row.get("doc_ref", "")) for row in soa["rows"]), (
        "a credit note naming nobody is not this party's")
    assert abs(soa["closing_balance"] - 100.0) < 0.01, (
        "and the transfer nets to nothing, so the balance is the invoice")

    unattributed = await _ledger(client, tok, "1120", contact_id="")
    assert abs(unattributed["closing_balance"]) < 0.01, "the two legs cancel"
    assert len(unattributed["lines"]) == 2, (
        "but both are on the control account, where they can be found")


@pytest.mark.asyncio
async def test_every_statement_sums_back_to_the_control_accounts(client):
    """The tie-out, and the reason the statement reads the ledger at all.

    A statement of account is a subledger: every party's statement plus the
    unattributed bucket has to add up to the receivable and payable control
    accounts, or the balance sheet says one thing and the statements say another
    with nothing on screen to explain the difference.

    The fixture is deliberately mixed: two customers with invoices, a payment,
    and an adjustment each side of the attribution line, plus a supplier on the
    payable account, so both control accounts and both signs are in the sum.

    This one is an invariant rather than a defect reproduction. It holds on the
    document-sourced statement too, because there both sides came from the same
    document totals. It is here to stay true afterwards, once the statement is a
    slice of the ledger and any posting at all can reach it."""
    tok = await _reg(client)
    alpha = await _contact(client, tok, name="Alpha")
    beta = await _contact(client, tok, name="Beta")
    gamma = await _contact(client, tok, name="Gamma", ctype="supplier")

    inv_a = await _invoice(client, tok, total=100.0, contact_id=alpha, issue_date="2026-01-05")
    assert (await client.post(f"/docs/{inv_a}/finalize", headers=_h(tok))).status_code == 200
    r = await client.post(f"/docs/{inv_a}/payment", headers=_h(tok), json={
        "payment_date": "2026-01-20", "amount": 40.0, "method": "transfer", "bank_account": "1111"})
    assert r.status_code == 200, r.text

    inv_b = await _invoice(client, tok, total=250.0, contact_id=beta, issue_date="2026-02-01")
    assert (await client.post(f"/docs/{inv_b}/finalize", headers=_h(tok))).status_code == 200

    po = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "purchase_order", "contact_id": gamma, "issue_date": "2026-02-10",
        "line_items": [{"name": "Steel", "quantity": 1, "unit_price": 500.0, "line_total": 500.0}],
        "subtotal": 500.0, "tax": 0.0, "total": 500.0})
    assert po.status_code == 200, po.text
    assert (await client.post(f"/docs/{po.json()['id']}/finalize", headers=_h(tok))).status_code == 200

    assert (await _post_manual_je(client, tok, [
        {"account": "1120", "debit": 15.0, "credit": 0, "contact": beta},
        {"account": "4100", "debit": 0, "credit": 15.0},
    ], ts="2026-02-15")).status_code == 200
    assert (await _post_manual_je(client, tok, [
        {"account": "1120", "debit": 7.0, "credit": 0},
        {"account": "4100", "debit": 0, "credit": 7.0},
    ], ts="2026-02-16")).status_code == 200

    statements = []
    for cid in (alpha, beta, gamma):
        soa = (await client.get(f"/accounting/soa/{cid}", headers=_h(tok))).json()
        statements.append(soa["closing_balance"])

    # Both reports net debits against credits; the ledger just reports each
    # account on its own normal side, so the payable figures flip to match.
    receivable = await _ledger(client, tok, "1120")
    payable = await _ledger(client, tok, "2110")
    unattributed = (
        (await _ledger(client, tok, "1120", contact_id=""))["closing_balance"]
        - (await _ledger(client, tok, "2110", contact_id=""))["closing_balance"]
    )
    control = receivable["closing_balance"] - payable["closing_balance"]

    assert abs(sum(statements) + unattributed - control) < 0.01, (
        f"statements {statements} plus unattributed {unattributed} "
        f"do not sum to the control accounts {control}")
    assert abs(receivable["closing_balance"] - 332.0) < 0.01, "the fixture itself moved"
    assert abs(payable["closing_balance"] - 500.0) < 0.01, "the fixture itself moved"


@pytest.mark.asyncio
async def test_a_statement_nets_what_a_party_owes_against_what_it_is_owed(client):
    """One party can be both customer and supplier. The statement is that party's
    net position on both control accounts, debits positive, so what the company
    owes them reads as a negative rather than stacking on the same side."""
    tok = await _reg(client)
    both = await _contact(client, tok, name="Both Ways")

    assert (await _post_manual_je(client, tok, [
        {"account": "1120", "debit": 100.0, "credit": 0, "contact": both},
        {"account": "4100", "debit": 0, "credit": 100.0},
    ], ts="2026-03-01")).status_code == 200
    assert (await _post_manual_je(client, tok, [
        {"account": "6200", "debit": 130.0, "credit": 0},
        {"account": "2110", "debit": 0, "credit": 130.0, "contact": both},
    ], ts="2026-03-02")).status_code == 200

    soa = (await client.get(f"/accounting/soa/{both}", headers=_h(tok))).json()
    assert len(soa["rows"]) == 2
    assert abs(soa["closing_balance"] + 30.0) < 0.01, (
        f"expected a net 30 owed to the party, got {soa['closing_balance']}")


@pytest.mark.asyncio
async def test_a_line_naming_a_contact_that_is_not_there_is_refused(client):
    """A party that resolves to nothing would post to a control account and then
    be missing from every statement, with nothing on screen to say why."""
    tok = await _reg(client)
    r = await _post_manual_je(client, tok, [
        {"account": "1120", "debit": 10.0, "credit": 0, "contact": "contact:nobody"},
        {"account": "4100", "debit": 0, "credit": 10.0},
    ])
    assert r.status_code == 422, r.text
    assert "contact:nobody" in r.json()["detail"]
    assert (await _journal(client, tok))["entries"] == []


@pytest.mark.asyncio
async def test_an_imported_line_naming_an_unknown_contact_is_refused(client):
    """Same rule at the import boundary, which writes entries without going
    through the manual entry endpoint."""
    tok = await _reg(client)
    r = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": f"je:{uuid.uuid4()}",
        "event_type": "acc.journal_entry.created",
        "data": {"status": "posted", "ts": "2026-01-15", "entries": [
            {"account": "1120", "debit": 10.0, "credit": 0, "contact": "contact:nobody"},
            {"account": "4100", "debit": 0, "credit": 10.0},
        ]},
        "source": "test", "idempotency_key": str(uuid.uuid4()),
    }]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0, "the entry was imported despite naming no real contact"
    assert any("contact:nobody" in e for e in body["errors"]), body["errors"]
    assert (await _journal(client, tok))["entries"] == []
