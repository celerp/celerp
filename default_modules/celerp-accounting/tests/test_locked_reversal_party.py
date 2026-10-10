# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A document voided out of a locked period keeps its party on the reversal.

The original entry stays in its locked period and its reversal posts on an open date.
The reversal belongs to the same document and the same customer or supplier as the
entry it reverses, so the party's ledger, statement and the journal read it that way:
the party nets to nothing for the document, nothing lands on the control account under
no party, and the locked period still shows the original amount on the party.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

ENTRY_DAY = "2026-01-15"
LOCK = "2026-03-31"
AR, AP = "1120", "2110"


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _reg(client) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "LockCo", "email": f"lrp-{uuid.uuid4().hex[:8]}@lrp.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _contact(client, tok, name: str, ctype: str) -> str:
    r = await client.post("/crm/contacts", headers=_h(tok), json={"name": name, "contact_type": ctype})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _finalized(client, tok, doc_type: str, contact: str, total: float, **extra) -> str:
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": doc_type, "contact_id": contact, "issue_date": ENTRY_DAY,
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}],
        "subtotal": total, "tax": 0.0, "total": total, **extra})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=_h(tok))
    assert r.status_code == 200, r.text
    return doc


async def _lock(client, tok) -> None:
    r = await client.post("/accounting/period-lock", headers=_h(tok), json={"lock_date": LOCK})
    assert r.status_code == 200, r.text


async def _void(client, tok, doc: str) -> None:
    r = await client.post(f"/docs/{doc}/void", headers=_h(tok), json={"reason": "Raised in error"})
    assert r.status_code == 200, r.text


async def _ledger(client, tok, code: str, **params) -> dict:
    r = await client.get(f"/accounting/ledger/{code}", headers=_h(tok), params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _soa(client, tok, contact: str, **params) -> dict:
    r = await client.get(f"/accounting/soa/{contact}", headers=_h(tok), params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _tb(client, tok) -> dict[str, float]:
    r = await client.get("/accounting/trial-balance", headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["balanced"], r.json()
    return {line["code"]: line["net"] for line in r.json()["lines"]}


async def _journal(client, tok) -> dict[str, dict]:
    r = await client.get("/accounting/journal", headers=_h(tok))
    assert r.status_code == 200, r.text
    return {e["je_id"]: e for e in r.json()["entries"]}


def _doc_lines(ledger: dict, doc: str) -> list[dict]:
    return [line for line in ledger["lines"] if line["je_id"].startswith(f"je:auto:{doc}:")]


async def _assert_party_reconciles(client, tok, code: str, doc: str, party: str, amount: float):
    """The document nets to nothing on the party, and nothing of it sits under no party."""
    whole = await _ledger(client, tok, code)
    party_ledger = await _ledger(client, tok, code, contact_id=party)
    blank = await _ledger(client, tok, code, contact_id="")
    blank_doc = sum(line["debit"] - line["credit"] for line in _doc_lines(blank, doc))
    assert abs(party_ledger["closing_balance"]) < 0.01 and not blank_doc, (
        f"{code} control {whole['closing_balance']}, {party} {party_ledger['closing_balance']}, "
        f"{doc} unattributed {blank_doc}")
    doc_lines = _doc_lines(whole, doc)
    reversals = [line for line in doc_lines if line["je_id"].endswith(":reversal")]
    assert reversals, f"the reversal of {doc} is on {code}: {doc_lines}"
    observed = {(line["je_id"], line["contact_id"], line["doc_id"], line["debit"], line["credit"])
                for line in doc_lines}
    assert all(line["contact_id"] == party for line in doc_lines), (
        f"every {code} line of {doc} belongs to {party}: {observed}")
    assert all(line["doc_id"] == doc for line in doc_lines), f"and names its document: {observed}"
    assert sum(line["debit"] - line["credit"] for line in doc_lines) == 0

    assert not _doc_lines(blank, doc), f"no {code} line of {doc} is unattributed: {_doc_lines(blank, doc)}"

    soa = await _soa(client, tok, party)
    assert abs(soa["closing_balance"]) < 0.01, f"{party} statement nets to nothing: {soa['rows']}"
    assert sum(1 for row in soa["rows"] if row["je_id"].endswith(":reversal")) == 1, soa["rows"]
    assert all(row["doc_id"] == doc for row in soa["rows"]), soa["rows"]

    # Party totals plus the unattributed bucket are the control account.
    parties = {line["contact_id"] for line in whole["lines"]}
    total = sum([(await _ledger(client, tok, code, contact_id=p))["closing_balance"] for p in parties])
    assert abs(total - whole["closing_balance"]) < 0.01

    # The locked period still shows the original amount on the party.
    before = await _ledger(client, tok, code, contact_id=party, date_to=LOCK)
    assert abs(abs(before["closing_balance"]) - amount) < 0.01, before
    soa_before = await _soa(client, tok, party, date_to=LOCK)
    assert abs(abs(soa_before["closing_balance"]) - amount) < 0.01, soa_before


async def test_a_voided_locked_invoice_reverses_on_its_customer(client):
    tok = await _reg(client)
    customer = await _contact(client, tok, "Buyer", "customer")
    doc = await _finalized(client, tok, "invoice", customer, 400.0)
    await _lock(client, tok)
    await _void(client, tok, doc)
    await _tb(client, tok)
    await _assert_party_reconciles(client, tok, AR, doc, customer, 400.0)
    reversal = (await _journal(client, tok))[f"je:auto:{doc}:fin:reversal"]
    assert reversal["source_doc"] and reversal["source_doc"]["doc_id"] == doc, reversal


async def test_a_voided_locked_bill_reverses_on_its_supplier(client):
    tok = await _reg(client)
    supplier = await _contact(client, tok, "Vendor", "vendor")
    doc = await _finalized(client, tok, "bill", supplier, 250.0)
    await _lock(client, tok)
    await _void(client, tok, doc)
    await _tb(client, tok)
    await _assert_party_reconciles(client, tok, AP, doc, supplier, 250.0)


async def test_a_voided_locked_credit_note_reverses_on_its_customer(client):
    tok = await _reg(client)
    customer = await _contact(client, tok, "Returner", "customer")
    doc = await _finalized(client, tok, "credit_note", customer, 60.0)
    await _lock(client, tok)
    await _void(client, tok, doc)
    await _tb(client, tok)
    await _assert_party_reconciles(client, tok, AR, doc, customer, 60.0)


async def test_a_voided_locked_foreign_invoice_keeps_its_currency(client):
    tok = await _reg(client)
    await client.patch("/companies/me/books", headers=_h(tok), json={"currency": "THB"})
    customer = await _contact(client, tok, "Abroad", "customer")
    doc = await _finalized(client, tok, "invoice", customer, 400.0, currency="USD", conversion_rate=35.0)
    await _lock(client, tok)
    await _void(client, tok, doc)
    await _tb(client, tok)
    journal = await _journal(client, tok)
    original, reversal = journal[f"je:auto:{doc}:fin"], journal[f"je:auto:{doc}:fin:reversal"]
    assert original["fx"] == {"currency": "USD", "rate": 35.0}, original
    assert reversal["fx"] == original["fx"], reversal
    assert {line["fx_currency"] for line in reversal["lines"]} == {"USD"}, reversal["lines"]
    whole = await _ledger(client, tok, AR)
    assert {line["contact_id"] for line in _doc_lines(whole, doc)} == {customer}, _doc_lines(whole, doc)


async def test_an_open_period_void_is_still_made_in_place(client):
    """Neighbour: with no lock the entry is voided in place and no reversal row exists."""
    tok = await _reg(client)
    customer = await _contact(client, tok, "Plain", "customer")
    doc = await _finalized(client, tok, "invoice", customer, 400.0)
    await _void(client, tok, doc)
    assert not _doc_lines(await _ledger(client, tok, AR), doc)
    assert abs((await _soa(client, tok, customer))["closing_balance"]) < 0.01
    assert f"je:auto:{doc}:fin:reversal" not in await _journal(client, tok)
