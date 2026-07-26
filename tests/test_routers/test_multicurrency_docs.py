# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Integration tests for multi-currency document support."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client) -> str:
    addr = f"fx-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post("/auth/register", json={"company_name": "FX Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


async def _make_invoice(client, h, currency="USD", conversion_rate=35.0):
    """A 100.00 invoice. The rate is sent whenever one is given, including 1:
    whether an explicit rate is legitimate is what several of these tests are
    about, so the helper must not decide it."""
    payload = {
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": currency,
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0,
        "tax": 0.0,
        "total": 100.0,
    }
    if conversion_rate is not None:
        payload["conversion_rate"] = conversion_rate
    r = await client.post("/docs", headers=h, json=payload)
    return r


async def _set_base_currency(client, h, currency: str) -> None:
    r = await client.patch("/companies/me", headers=h, json={"settings": {"currency": currency}})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_create_foreign_currency_invoice(client):
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")

    r = await _make_invoice(client, h, "USD", 35.0)
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    assert doc.get("currency") == "USD"
    assert float(doc.get("conversion_rate", 0)) == 35.0


# ---------------------------------------------------------------------------
# The conversion rate is validated at every door that can set it, because it
# multiplies every amount the document posts to the ledger.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -35.0])
async def test_create_refuses_a_conversion_rate_that_is_not_a_rate(client, bad):
    """A rate of zero would post the foreign amount as though it were base
    currency; a negative one would post the mirror image of the document. Refused
    where the value enters, so neither is ever a stored fact."""
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")

    r = await _make_invoice(client, h, "USD", bad)
    assert r.status_code == 422, r.text
    assert "greater than zero" in r.text


@pytest.mark.asyncio
async def test_create_holds_the_conversion_rate_to_the_exchange_rate_ceiling(client):
    """A document's rate is stored at the same ceiling a journal line's rate and
    a payment's rate are: the journal shows a rate at twelve places, so a
    document kept finer than that posts a base amount no reader could reproduce
    from the rate in front of them."""
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")

    r = await _make_invoice(client, h, "USD", 0.00001117318355)
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=h)).json()
    # Half up at the thirteenth place: ...18355 -> ...184, not truncated.
    assert doc["conversion_rate"] == 0.000011173184


@pytest.mark.asyncio
async def test_patch_refuses_a_conversion_rate_that_is_not_a_rate(client):
    """The edit door needs its own guard: fields_changed is a free-form envelope,
    so nothing about the request body constrains what arrives in it."""
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")
    doc_id = (await _make_invoice(client, h, "USD", 35.0)).json()["id"]

    r = await client.patch(f"/docs/{doc_id}", headers=h, json={
        "fields_changed": {"conversion_rate": {"old": 35.0, "new": -35.0}}})
    assert r.status_code == 422, r.text
    assert "greater than zero" in r.text

    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    assert doc["conversion_rate"] == 35.0, "the rejected edit must not have landed"


@pytest.mark.asyncio
async def test_patch_stores_the_conversion_rate_as_a_number_at_the_ceiling(client):
    """The inline rate field posts a form value, so the edit arrives as a string.

    One rate is one stored fact: it is stored as a number at the ceiling
    whichever door set it, not as a float from create and a string from an edit.
    """
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")
    doc_id = (await _make_invoice(client, h, "USD", 35.0)).json()["id"]

    r = await client.patch(f"/docs/{doc_id}", headers=h, json={
        "fields_changed": {"conversion_rate": {"old": 35.0, "new": "0.00001117318355"}}})
    assert r.status_code == 200, r.text

    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    assert doc["conversion_rate"] == 0.000011173184
    assert isinstance(doc["conversion_rate"], float)


@pytest.mark.asyncio
async def test_patch_clears_the_conversion_rate_with_an_empty_value(client):
    """Clearing has to stay open: it is the remedy for a rate that should not be
    on the document at all, which finalization refuses to post."""
    token = await _register(client)
    h = _auth(token)
    doc_id = (await _make_invoice(client, h, "USD", 35.0)).json()["id"]

    r = await client.patch(f"/docs/{doc_id}", headers=h, json={
        "fields_changed": {"conversion_rate": {"old": 35.0, "new": ""}}})
    assert r.status_code == 200, r.text

    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    assert doc.get("conversion_rate") is None
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_finalize_refuses_a_base_currency_doc_carrying_a_rate(client, session):
    """A document in the company's own currency converts at 1 by definition, so
    any other stored rate restates it: 100 USD booked as 3500 in USD books.

    Checked at finalization rather than on entry because that is the last point
    before the journal entry is minted, it is where the stored currency and the
    stored rate can be compared against each other, and the rate is immutable
    afterwards. The manual journal door already refuses the same mismatch.
    """
    token = await _register(client)
    h = _auth(token)  # base currency stays USD

    doc_id = (await _make_invoice(client, h, "USD", 35.0)).json()["id"]

    r = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "USD" in detail and "1" in detail

    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"))
    )).scalars().all()
    assert rows == [], "nothing may post when the rate is refused"


@pytest.mark.asyncio
async def test_finalize_accepts_an_explicit_rate_of_one_on_a_base_currency_doc(client, session):
    """The guard must not over-block: 1 is the correct rate for a base-currency
    document, so an explicit 1 posts the document's own amounts unchanged."""
    token = await _register(client)
    h = _auth(token)

    doc_id = (await _make_invoice(client, h, "USD", 1.0)).json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200

    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"))
    )).scalars().all()
    created = next((r for r in rows if r.event_type == "acc.journal_entry.created"), None)
    assert created is not None
    ar = next((e for e in created.data.get("entries", [])
               if e.get("account") == "1120" and e.get("debit", 0) > 0), None)
    assert ar is not None and abs(float(ar["debit"]) - 100.0) < 0.01


@pytest.mark.asyncio
async def test_invalid_currency_code_rejected(client):
    token = await _register(client)
    h = _auth(token)

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "XYZ",
        "conversion_rate": 1.0,
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0}],
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_finalize_base_currency_doc_succeeds(client):
    """Regression: base-currency docs must finalize unchanged (no rate required)."""
    token = await _register(client)
    h = _auth(token)

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    finalize = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert finalize.status_code == 200


@pytest.mark.asyncio
async def test_finalize_foreign_currency_without_rate_fails(client):
    token = await _register(client)
    h = _auth(token)

    # Set base currency to THB so USD docs are foreign
    await client.patch("/companies/me", headers=h, json={"settings": {"currency": "THB"}})

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "USD",
        # No conversion_rate
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    finalize = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert finalize.status_code == 422
    assert "conversion rate" in finalize.json()["detail"].lower()


@pytest.mark.asyncio
async def test_finalize_with_rate_creates_base_currency_jes(client, session):
    """JE debit on AR account must equal total_usd * rate in base currency."""
    token = await _register(client)
    h = _auth(token)

    # Set base currency to THB
    await client.patch("/companies/me", headers=h, json={"settings": {"currency": "THB"}})

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "USD",
        "conversion_rate": 35.0,
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc = r.json()
    doc_id = doc.get("id")

    finalize = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert finalize.status_code == 200, finalize.text

    # Read the auto-JE ledger entries for this doc
    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"))
    )).scalars().all()

    created_row = next(
        (row for row in rows if row.event_type == "acc.journal_entry.created"),
        None,
    )
    assert created_row is not None, "JE created event not found"
    entries = created_row.data.get("entries", [])
    ar_entry = next((e for e in entries if e.get("account") == "1120" and e.get("debit", 0) > 0), None)
    assert ar_entry is not None, "AR debit entry not found"
    # 100 USD * 35.0 = 3500 THB
    assert abs(float(ar_entry["debit"]) - 3500.0) < 0.01, f"Expected 3500, got {ar_entry['debit']}"


@pytest.mark.asyncio
async def test_finalize_with_rate_revenue_entry_in_base(client, session):
    """Revenue credit entry must also be in base currency."""
    token = await _register(client)
    h = _auth(token)

    await client.patch("/companies/me", headers=h, json={"settings": {"currency": "THB"}})

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "USD",
        "conversion_rate": 40.0,
        "line_items": [{"name": "Item", "quantity": 2, "unit_price": 50.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    await client.post(f"/docs/{doc_id}/finalize", headers=h)

    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"))
    )).scalars().all()
    created_row = next((row for row in rows if row.event_type == "acc.journal_entry.created"), None)
    assert created_row is not None
    entries = created_row.data.get("entries", [])
    rev_entry = next((e for e in entries if e.get("account") == "4100" and e.get("credit", 0) > 0), None)
    assert rev_entry is not None
    # 100 USD * 40 = 4000 THB
    assert abs(float(rev_entry["credit"]) - 4000.0) < 0.01, f"Expected 4000, got {rev_entry['credit']}"
