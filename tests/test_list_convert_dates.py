# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A List converted to a document keeps its issue and due dates as a pair: the document is issued
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
