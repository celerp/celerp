# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A copy keeps what the user entered on the source (record_copy): the commission agent and rate,
the From block and an audit List's location survive a duplicate and a convert, on a List and on a
quotation document, from the demo company."""
from __future__ import annotations

from test_tax_acceptance_matrix import _demo_company

CONTENT = {"commission_rate_pct": 5, "company_name": "Edited Seller", "company_address": "1 Edited Road"}


async def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


async def _company(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    cust = next(c["id"] for c in contacts if c.get("contact_type") in ("customer", "both"))
    agent = next(c["id"] for c in contacts if c.get("contact_type") in ("vendor", "both"))
    return auth["headers"], cust, {**CONTENT, "commission_contact_id": agent}


def _kept(copy: dict, content: dict):
    assert {k: copy.get(k) for k in content} == content


async def test_a_list_duplicate_and_its_invoice_keep_the_commission_and_from_block(client, session):
    h, cust, content = await _company(client, session)
    lid = (await _ok(await client.post("/lists", headers=h, json={
        "list_type": "quotation", "contact_id": cust, "line_items": [{"name": "A", "quantity": 1, "unit_price": 9}]})))["id"]
    await _ok(await client.patch(f"/lists/{lid}", headers=h, json={
        "fields_changed": {k: {"new": v} for k, v in content.items()}}))
    dup = (await _ok(await client.post(f"/lists/{lid}/duplicate", headers=h)))["id"]
    _kept(await _ok(await client.get(f"/lists/{dup}", headers=h)), content)
    await _ok(await client.post(f"/lists/{dup}/finalize", headers=h))
    inv = (await _ok(await client.post(f"/lists/{dup}/convert", headers=h, json={"target_type": "invoice"})))["target_doc_id"]
    _kept(await _ok(await client.get(f"/docs/{inv}", headers=h)), content)


async def test_a_quotation_document_converted_keeps_the_commission_and_from_block(client, session):
    h, cust, content = await _company(client, session)
    qid = (await _ok(await client.post("/docs", headers=h, json={
        "doc_type": "quotation", "contact_id": cust, "line_items": [{"name": "A", "quantity": 1, "unit_price": 9}]})))["id"]
    await _ok(await client.patch(f"/docs/{qid}", headers=h, json={
        "fields_changed": {k: {"new": v} for k, v in content.items()}}))
    await _ok(await client.post(f"/docs/{qid}/finalize", headers=h))
    inv = (await _ok(await client.post(f"/docs/{qid}/convert", headers=h)))["target_doc_id"]
    _kept(await _ok(await client.get(f"/docs/{inv}", headers=h)), content)


async def test_an_audit_list_duplicate_keeps_its_location(client, session):
    h, _, _ = await _company(client, session)
    loc = (await _ok(await client.post("/companies/me/locations", headers=h, json={"name": "Shop", "type": "warehouse"})))["id"]
    aid = (await _ok(await client.post("/lists/audit", headers=h, json={"location_id": loc})))["id"]
    dup = (await _ok(await client.post(f"/lists/{aid}/duplicate", headers=h)))["id"]
    assert (await _ok(await client.get(f"/lists/{dup}", headers=h)))["location_id"] == loc
