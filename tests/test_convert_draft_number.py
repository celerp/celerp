# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A converted document is numbered like any new draft of its type.

A draft invoice carries a proforma number and takes its invoice number when it is
finalized, so an invoice made by converting a list, a quotation or a memo uses exactly
one invoice number, the one finalize gives it. A memo made from a list takes the next
memo number, as a new memo does.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.models.company import Company

pytestmark = pytest.mark.asyncio


async def _next(session, auth, doc_type: str) -> int:
    session.expire_all()
    company = await session.get(Company, auth["company_id"])
    return int(((company.settings or {}).get("sequences") or {}).get(doc_type, {}).get("next", 1))


async def _ok(r) -> dict:
    assert r.status_code == 200, r.text
    return r.json()


def _line(sku: str, **extra) -> dict:
    return {"sku": sku, "name": sku, "quantity": 1, "unit_price": 10.0, **extra}


async def _issued_list(client, auth) -> str:
    sku = f"CV-{uuid.uuid4().hex[:6]}"
    eid = (await _ok(await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "line_items": [_line(sku)]})))["id"]
    await _ok(await client.post(f"/lists/{eid}/finalize", headers=auth["headers"]))
    return eid


async def _from_list(client, auth, target: str) -> str:
    eid = await _issued_list(client, auth)
    return (await _ok(await client.post(f"/lists/{eid}/convert", headers=auth["headers"],
                                        json={"target_type": target})))["target_doc_id"]


async def _from_quotation(client, auth) -> str:
    q = (await _ok(await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "quotation", "line_items": [_line(f"CV-{uuid.uuid4().hex[:6]}")], "total": 10.0})))["id"]
    return (await _ok(await client.post(f"/docs/{q}/convert", headers=auth["headers"])))["target_doc_id"]


async def _from_memo(client, auth) -> str:
    sku = f"CV-{uuid.uuid4().hex[:6]}"
    item = (await _ok(await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece", "status": "available"})))["id"]
    memo = (await _ok(await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "memo", "line_items": [_line(sku, entity_id=item)], "total": 10.0})))["id"]
    await _ok(await client.post(f"/docs/{memo}/finalize", headers=auth["headers"]))
    await _ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=auth["headers"],
                                json={"line_entity_ids": [item]}))
    return (await _ok(await client.post(f"/docs/{memo}/convert", headers=auth["headers"])))["target_doc_id"]


@pytest.mark.parametrize("convert", [
    lambda client, auth: _from_list(client, auth, "invoice"), _from_quotation, _from_memo,
], ids=["list", "quotation", "memo"])
async def test_a_converted_invoice_uses_one_invoice_number(client, session, auth, convert):
    before = await _next(session, auth, "invoice")
    draft = await convert(client, auth)

    doc = await _ok(await client.get(f"/docs/{draft}", headers=auth["headers"]))
    assert (doc["status"], doc["ref_id"][:3]) == ("draft", "PF-")
    assert await _next(session, auth, "invoice") == before

    await _ok(await client.post(f"/docs/{draft}/finalize", headers=auth["headers"]))
    doc = await _ok(await client.get(f"/docs/{draft}", headers=auth["headers"]))
    assert doc["ref_id"].startswith("INV-") and doc["source_proforma_ref"] == draft.removeprefix("doc:")
    assert await _next(session, auth, "invoice") == before + 1


async def test_a_memo_from_a_list_takes_the_next_memo_number(client, session, auth):
    before = await _next(session, auth, "memo")
    draft = await _from_list(client, auth, "memo")

    doc = await _ok(await client.get(f"/docs/{draft}", headers=auth["headers"]))
    assert (doc["doc_type"], doc["status"], doc["ref_id"][:5]) == ("memo", "draft", "MEMO-")
    assert await _next(session, auth, "memo") == before + 1
