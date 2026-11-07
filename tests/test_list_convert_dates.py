# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A List converted to a document, or duplicated, keeps its issue and due dates as a pair: the document is issued
on the company's business day and falls due by the List's payment term from that day, never on
a due date set against the List's own, earlier issue date."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from test_tax_acceptance_matrix import _demo_company

pytestmark = pytest.mark.asyncio


async def _converted(client, session, target, **dates):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    cust = next(c["id"] for c in contacts if c.get("contact_type") in ("customer", "both"))
    r = await client.post("/lists", headers=h, json={
        "list_type": "quotation", "contact_id": cust, "issue_date": "2026-10-01", "due_date": "2026-10-31",
        "line_items": [{"name": "A", "quantity": 2, "unit_price": 50}], **dates})
    assert r.status_code == 200, r.text
    lid = r.json()["id"]
    assert (await client.post(f"/lists/{lid}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/lists/{lid}/send", headers=h, json={"sent_via": "manual"})).status_code == 200
    r = await client.post(f"/lists/{lid}/convert", headers=h, json={"target_type": target})
    assert r.status_code == 200, r.text
    return (await client.get(f"/docs/{r.json()['target_doc_id']}", headers=h)).json()


@pytest.mark.parametrize("target", ["invoice", "memo"])
async def test_a_converted_list_falls_due_by_its_term_from_the_new_issue_date(client, session, target):
    doc = await _converted(client, session, target, payment_terms="Net 30")
    issued = date.fromisoformat(doc["issue_date"])
    assert issued > date(2026, 10, 1)
    assert doc["due_date"] == (issued + timedelta(days=30)).isoformat()


@pytest.mark.parametrize("target", ["invoice", "memo"])
async def test_a_converted_list_without_a_term_drops_the_lists_due_date(client, session, target):
    doc = await _converted(client, session, target)
    assert doc["issue_date"] > "2026-10-01"
    assert not doc.get("due_date"), doc["due_date"]


@pytest.mark.parametrize("terms", ["Net 30", None])
async def test_a_duplicated_list_keeps_its_issue_and_due_dates_as_a_pair(client, session, terms):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    cust = next(c["id"] for c in contacts if c.get("contact_type") in ("customer", "both"))
    r = await client.post("/lists", headers=h, json={
        "list_type": "quotation", "contact_id": cust, "issue_date": "2026-10-01", "due_date": "2026-10-31",
        "line_items": [{"name": "A", "quantity": 2, "unit_price": 50}],
        **({"payment_terms": terms} if terms else {})})
    assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{r.json()['id']}/duplicate", headers=h)
    assert r.status_code == 200, r.text
    copy = (await client.get(f"/lists/{r.json()['id']}", headers=h)).json()
    issued = date.fromisoformat(copy["issue_date"])
    assert issued > date(2026, 10, 1)
    if terms:
        assert copy["due_date"] == (issued + timedelta(days=30)).isoformat()
    else:
        assert not copy.get("due_date"), copy["due_date"]


async def _quotation(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    cust = next(c["id"] for c in contacts if c.get("contact_type") in ("customer", "both"))
    r = await client.post("/lists", headers=h, json={
        "list_type": "quotation", "contact_id": cust, "line_items": [{"name": "A", "quantity": 2, "unit_price": 50}]})
    assert r.status_code == 200, r.text
    return h, r.json()["id"]


async def test_every_convert_refusal_is_keyed_and_names_what_stops_it(client, session):
    """A List the user cannot convert is refused in the reader's language, saying why: the target is not a
    document a List becomes, the List is not finalized yet, or it already became a document."""
    h, lid = await _quotation(client, session)
    r = await client.post(f"/lists/{lid}/convert", headers=h, json={"target_type": "invoice"})
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "documents.err_list_convert_status", r.text
    assert (await client.post(f"/lists/{lid}/finalize", headers=h)).status_code == 200
    for target in ("bill", "", " ", "invoiceé", "x" * 5000):
        r = await client.post(f"/lists/{lid}/convert", headers=h, json={"target_type": target})
        assert r.status_code == 422 and r.json()["detail"]["message_key"] == "documents.err_convert_unsupported", r.text
    r = await client.post(f"/lists/{lid}/convert", headers=h, json={"target_type": "invoice"})
    assert r.status_code == 200, r.text
    number = r.json()["target_doc_id"].removeprefix("doc:")
    r = await client.post(f"/lists/{lid}/convert", headers=h, json={"target_type": "memo"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "documents.err_list_already_converted", r.text
    assert r.json()["detail"]["params"] == {"number": number}
    assert number in r.json()["detail"]["message"]
