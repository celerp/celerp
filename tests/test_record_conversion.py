# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A conversion charges what was agreed: a quotation (document or List) converted to an invoice or a
memo, and a consignment bought, keep the source's CONVERSION_KEYS (its header discount, shipping,
document taxes, header tax rate, currency and payment term) beside its parties and lines, so the new
document totals what was quoted and falls due by the term it kept. A duplicate keeps none of them.

The guard reads CONVERSION_KEYS itself: every conversion door is given a source holding each key and
must keep each one equal, so a key added to the constant is checked at every door."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from celerp_docs.doc_money import CONVERSION_KEYS
from test_consignment_in_sale import _consign, _state
from test_tax_acceptance_matrix import _demo_company, _tax

# A value of each key a source can hold, so every conversion door is handed all of CONVERSION_KEYS.
SOURCE = {"discount": 10, "discount_type": "percentage", "shipping": 5, "doc_taxes": [_tax("VAT 7%", 7)],
          "tax_rate": 3, "currency": "USD", "payment_terms": "Net 30"}


def test_every_conversion_key_has_a_source_value():
    assert set(SOURCE) == CONVERSION_KEYS


async def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


async def _company(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    cust = next(c["id"] for c in contacts if c.get("contact_type") in ("customer", "both"))
    item = (await _ok(await client.get("/items", headers=h, params={"limit": 5})))["items"][0]
    line = {"item_id": item["id"], "sku": item.get("sku"), "name": item.get("name"), "quantity": 2,
            "unit_price": 50, "taxes": []}
    return auth, h, cust, line


async def _quotation_doc(client, h, cust, line) -> dict:
    qid = (await _ok(await client.post("/docs", headers=h, json={
        "doc_type": "quotation", "contact_id": cust, "line_items": [line], **SOURCE})))["id"]
    return await _ok(await client.get(f"/docs/{qid}", headers=h))


async def _quotation_list(client, h, cust, line) -> dict:
    """A finalized quotation List with a header discount and a header tax rate (``tax``)."""
    lid = (await _ok(await client.post("/lists", headers=h, json={
        "list_type": "quotation", "contact_id": cust, "line_items": [line], "discount": 10,
        "discount_type": "percentage", "tax": 7})))["id"]
    await _ok(await client.post(f"/lists/{lid}/finalize", headers=h))
    return await _ok(await client.get(f"/lists/{lid}", headers=h))


def _kept(target: dict, source: dict, *, tax_rate_key: str = "tax_rate") -> None:
    """Each of CONVERSION_KEYS on ``target`` equals the source's (a List's header rate is its ``tax``)."""
    for key in CONVERSION_KEYS:
        assert target.get(key) == source.get(tax_rate_key if key == "tax_rate" else key), key


async def test_a_quotation_document_converts_to_an_invoice_of_the_quoted_total_and_term(client, session):
    auth, h, cust, line = await _company(client, session)
    quote = await _quotation_doc(client, h, cust, line)
    assert all(quote.get(k) for k in CONVERSION_KEYS), quote
    inv = await _ok(await client.get(f"/docs/{(await _ok(await client.post(f'/docs/{quote['id']}/convert', headers=h, json={})))['target_doc_id']}", headers=h))
    _kept(inv, quote)
    for key in ("subtotal", "discount_amount", "tax", "total"):
        assert inv[key] == pytest.approx(quote[key]), key
    assert date.fromisoformat(inv["due_date"]) - date.fromisoformat(inv["issue_date"]) == timedelta(days=30)


@pytest.mark.parametrize("target", ["invoice", "memo"])
async def test_a_quotation_list_converts_to_a_document_of_the_quoted_total(client, session, target):
    auth, h, cust, line = await _company(client, session)
    quote = await _quotation_list(client, h, cust, line)
    assert quote["discount_amount"] and quote["tax_amount"], quote
    doc = await _ok(await client.get(f"/docs/{(await _ok(await client.post(f'/lists/{quote['id']}/convert', headers=h, json={'target_type': target})))['target_doc_id']}", headers=h))
    _kept(doc, quote, tax_rate_key="tax")
    assert (doc["subtotal"], doc["discount_amount"], doc["tax"], doc["total"]) == (
        pytest.approx(quote["subtotal"]), pytest.approx(quote["discount_amount"]),
        pytest.approx(quote["tax_amount"]), pytest.approx(quote["total"]))


async def test_a_bought_consignment_keeps_its_money_and_term(client, session):
    auth, _ = await _demo_company(client, session, "USD")
    con, _ = await _consign(client, session, auth, qty=2, cost_price=4.0, **SOURCE)
    source = await _state(session, auth, con)
    assert all(source.get(k) for k in CONVERSION_KEYS), source
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    _kept(await _state(session, auth, (await _ok(r))["target_doc_id"]), source)


async def test_a_duplicate_keeps_none_of_the_conversion_keys_but_its_currency(client, session):
    auth, h, cust, line = await _company(client, session)
    quote = await _quotation_list(client, h, cust, line)
    dup = await _ok(await client.get(f"/lists/{(await _ok(await client.post(f'/lists/{quote['id']}/duplicate', headers=h)))['id']}", headers=h))
    blank = await _ok(await client.get(f"/lists/{(await _ok(await client.post('/lists', headers=h, json={'list_type': 'quotation'})))['id']}", headers=h))
    for key in CONVERSION_KEYS - {"currency"} | {"tax"}:
        assert dup.get(key) == blank.get(key), key
    assert dup.get("currency") == quote.get("currency")
    assert dup["contact_id"] == cust and [li["item_id"] for li in dup["line_items"]] == [line["item_id"]]
