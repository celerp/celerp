# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Choosing a customer on a draft List applies the customer's currency, as on documents."""
from __future__ import annotations

import uuid

import pytest

from celerp_docs.doc_projections import apply_documents_event


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("currency", "line_total"), [("EUR", 100.01), ("JPY", 100.0)])
async def test_draft_list_selection_takes_customer_currency_and_reprices_in_it(client, currency, line_total):
    r = await client.post("/auth/register", json={
        "company_name": "Currency Co",
        "email": f"list-currency-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner",
        "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    h = _h(r.json()["access_token"])
    r = await client.post("/items", headers=h, json={
        "status": "available", "sku": "CUR-1", "name": "Cur", "quantity": 10, "sell_by": "piece",
        "retail_price": 50, "wholesale_price": 33.335,
    })
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    r = await client.post("/crm/contacts", headers=h, json={
        "name": "Euro Customer", "contact_type": "customer", "currency": currency,
    })
    assert r.status_code == 200, r.text
    contact_id = r.json()["id"]
    r = await client.patch(f"/crm/contacts/{contact_id}", headers=h, json={
        "fields_changed": {"price_list": {"old": None, "new": "Wholesale"}},
    })
    assert r.status_code == 200, r.text
    r = await client.post("/lists", headers=h, json={
        "list_type": "quotation", "price_list": "Retail", "currency": "USD",
        "line_items": [{"item_id": item_id, "sku": "CUR-1", "description": "Cur", "quantity": 3,
                        "unit_price": 50, "line_total": 150}],
    })
    assert r.status_code == 200, r.text
    list_id = r.json()["id"]

    # The List customer picker sends only the customer and the version the page shows.
    version = (await client.get(f"/lists/{list_id}", headers=h)).json()["version"]
    r = await client.patch(f"/lists/{list_id}", headers=h, json={
        "fields_changed": {"contact_id": {"old": None, "new": contact_id}},
        "expected_version": version,
    })
    assert r.status_code == 200, r.text
    state = (await client.get(f"/lists/{list_id}", headers=h)).json()
    assert state["contact_id"] == contact_id
    assert state["currency"] == currency
    assert state["price_list"] == "Wholesale"
    line = state["line_items"][0]
    # 3 x 33.335 = 100.005, rounded at the customer's currency precision.
    assert line["unit_price"] == 33.335
    assert line["line_total"] == line_total
    assert state["subtotal"] == line_total
    assert state["total"] == line_total


def test_list_currency_change_recalculates_totals_in_the_new_currency():
    state = {"status": "draft", "currency": "USD", "list_type": "quotation", "discount": 0,
             "tax": 0, "line_items": [{"quantity": 1, "unit_price": 10.4, "line_total": 10.4}]}
    after = apply_documents_event(state, "list.updated",
                                  {"fields_changed": {"currency": {"old": "USD", "new": "JPY"}}})
    assert after["currency"] == "JPY"
    assert after["subtotal"] == 10.0
    assert after["total"] == 10.0


@pytest.mark.parametrize("status", ["finalized", "closed", "void"])
def test_non_draft_list_currency_is_unchanged(status):
    state = {"status": status, "currency": "USD", "list_type": "quotation", "discount": 0,
             "tax": 0, "line_items": []}
    after = apply_documents_event(state, "list.updated",
                                  {"fields_changed": {"currency": {"old": "USD", "new": "EUR"}}})
    assert after["currency"] == "USD"
