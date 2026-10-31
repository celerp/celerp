# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Item and category income accounts, and the revenue lines a sale posts from them.

Initial conditions per test: a fresh company with the seeded chart (4000 Revenue is a
header, 4100 Sales Revenue, 4200 Service Revenue and 4300 Other Income are revenue
accounts, 1120 is the receivable). Items are service items made through POST /items;
invoices and credit notes are made and finalized through /docs. An item's own income
account wins over its category's; an item with neither sells on the sales revenue
account, as every sale did before. A credit note line linked to an invoice line reverses
the account that invoice line posted, read from the invoice's entry, never from the
item as it is now.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_credit_note_settlement_owned import _post
from test_set_aside_older_paths import _net

pytestmark = pytest.mark.asyncio


async def _account(client, auth, code: str, account_type: str = "revenue", parent: str | None = "4000") -> None:
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": code, "name": f"Account {code}", "account_type": account_type, "parent_code": parent})
    assert r.status_code == 200, r.text


async def _category(client, auth, name: str) -> str:
    r = await client.post("/companies/me/categories", headers=auth["headers"], json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["key"]


async def _set_category_account(client, auth, key: str, code: str | None):
    return await client.put(f"/companies/me/categories/{key}/income-account", headers=auth["headers"],
                            json={"income_account": code})


async def _item(client, auth, name: str, **fields) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "name": name, "sell_by": "piece", "inventory_type": "service", "quantity": 0, "status": "available",
        **fields})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _patch_item(client, auth, item: str, **changes):
    return await client.patch(f"/items/{item}", headers=auth["headers"], json={
        "fields_changed": {k: {"new": v} for k, v in changes.items()}})


def _line(item: str | None, name: str, amount: float, **extra) -> dict:
    return {"item_id": item, "name": name, "quantity": 1, "unit_price": amount, "line_total": amount, **extra}


async def _draft(client, auth, doc_type: str, lines: list[dict], **extra) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "ref_id": f"{doc_type[:3].upper()}-{uuid.uuid4().hex[:6]}",
        "line_items": lines, "total": round(sum(li["line_total"] for li in lines), 2), **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _issued(client, auth, doc_type: str, lines: list[dict], **extra) -> str:
    doc = await _draft(client, auth, doc_type, lines, **extra)
    r = await _post(client, auth, f"/docs/{doc}/finalize")
    assert r.status_code == 200, r.text
    return doc


async def _line_ids(client, auth, doc: str) -> list[str]:
    r = await client.get(f"/docs/{doc}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return [li["line_id"] for li in r.json()["line_items"]]


async def _revenue(session, auth) -> dict[str, float]:
    """Credit less debit on each revenue account over every posted entry."""
    return {code: -await _net(session, auth, code, prefix="je:") for code in ("4100", "4200", "4300", "4250")}


async def _finalize_entry(session, auth, doc: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": f"je:auto:{doc}:fin"})
    assert row is not None and row.state.get("status") == "posted"
    return row.state


async def test_income_account_validated_on_item_and_category_save(client, session, auth):
    await _account(client, auth, "1190", account_type="asset", parent=None)
    refused = {"9999": "not in the chart", "1190": "revenue", "4000": "header"}

    for code, why in refused.items():
        r = await client.post("/items", headers=auth["headers"], json={
            "name": "Workshop", "sell_by": "piece", "inventory_type": "service", "income_account": code})
        assert r.status_code == 422, (code, r.text)
        assert code in r.text and why in r.text.lower(), r.text
    item = await _item(client, auth, "Workshop", income_account="4200")
    session.expire_all()
    assert (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": item})).state["income_account"] == "4200"

    for code in refused:
        r = await _patch_item(client, auth, item, income_account=code)
        assert r.status_code == 422, (code, r.text)
        assert code in r.text
    assert (await _patch_item(client, auth, item, income_account="4300")).status_code == 200
    assert (await _patch_item(client, auth, item, income_account="")).status_code == 200
    session.expire_all()
    assert "income_account" not in (await session.get(
        Projection, {"company_id": auth["company_id"], "entity_id": item})).state

    key = await _category(client, auth, "Workshops")
    for code in refused:
        r = await _set_category_account(client, auth, key, code)
        assert r.status_code == 422, (code, r.text)
        assert code in r.text
    assert (await _set_category_account(client, auth, "no_such_category", "4200")).status_code == 404
    for _ in range(2):  # set-to-state: a repeat is a no-op
        r = await _set_category_account(client, auth, key, "4200")
        assert r.status_code == 200, r.text
    schemas = (await client.get("/companies/me/category-income-accounts", headers=auth["headers"])).json()
    assert schemas == {key: "4200"}
    assert (await _set_category_account(client, auth, key, None)).status_code == 200
    assert (await client.get("/companies/me/category-income-accounts", headers=auth["headers"])).json() == {}


async def test_finalize_refuses_missing_or_non_revenue_income_account(client, session, auth):
    await _account(client, auth, "4250")
    await _account(client, auth, "4260")
    key = await _category(client, auth, "Workshops")
    own = await _item(client, auth, "Strategy day", income_account="4250")
    by_category = await _item(client, auth, "Coaching hour", category=key)
    assert (await _set_category_account(client, auth, key, "4260")).status_code == 200

    # Both accounts change after the items were saved: one stops being revenue, one is archived.
    r = await client.patch("/accounting/accounts/4250", headers=auth["headers"], json={"account_type": "expense"})
    assert r.status_code == 200, r.text
    r = await client.patch("/accounting/accounts/4260", headers=auth["headers"], json={"is_active": False})
    assert r.status_code == 200, r.text

    for item, name, code in ((own, "Strategy day", "4250"), (by_category, "Coaching hour", "4260")):
        doc = await _draft(client, auth, "invoice", [_line(item, name, 100.0)])
        before = await _revenue(session, auth)
        r = await _post(client, auth, f"/docs/{doc}/finalize")
        assert r.status_code == 422, r.text
        assert name in r.text and code in r.text, r.text
        assert await _revenue(session, auth) == before
        session.expire_all()
        state = (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc})).state
        assert state["status"] == "draft"


async def test_account_delete_refused_while_items_reference_it(client, session, auth):
    from celerp_accounting.chart_rules import remove_account
    from fastapi import HTTPException

    await _account(client, auth, "4250")
    key = await _category(client, auth, "Workshops")
    await _item(client, auth, "Strategy day", income_account="4250")
    assert (await _set_category_account(client, auth, key, "4250")).status_code == 200

    with pytest.raises(HTTPException) as refused:
        await remove_account(session, auth["company_id"], "4250")
    assert refused.value.status_code == 409
    assert "Strategy day" in str(refused.value.detail) and "Workshops" in str(refused.value.detail)

    await session.rollback()
    await _account(client, auth, "4270")
    await remove_account(session, auth["company_id"], "4270")


async def _mixed_invoice(client, auth) -> tuple[str, str, str, list[str]]:
    """An invoice selling a 4200 item for 100.00, a 4300 item for 33.33 and a line with no item for 16.67."""
    a = await _item(client, auth, "Workshop", income_account="4200")
    b = await _item(client, auth, "Room hire", income_account="4300")
    doc = await _issued(client, auth, "invoice", [_line(a, "Workshop", 100.0), _line(b, "Room hire", 33.33),
                                                   _line(None, "Notes", 16.67)])
    return doc, a, b, await _line_ids(client, auth, doc)


async def test_mixed_income_accounts_split_exact(client, session, auth):
    doc, _, _, line_ids = await _mixed_invoice(client, auth)
    assert await _revenue(session, auth) == {"4100": 16.67, "4200": 100.0, "4300": 33.33, "4250": 0.0}
    entry = await _finalize_entry(session, auth, doc)
    credits = [e for e in entry["entries"] if e["account"] in ("4100", "4200", "4300")]
    assert len(credits) == 3 and round(sum(e["credit"] for e in credits), 2) == 150.0
    session.expire_all()
    created = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == f"je:auto:{doc}:fin",
        LedgerEntry.event_type == "acc.journal_entry.created"))).scalars().one()
    allocations = created.metadata_["line_allocations"]
    assert {a["line_id"]: a["account"] for a in allocations.values()} == dict(zip(line_ids, ("4200", "4300", "4100")))


async def test_credit_note_linked_line_reverses_posted_account(client, session, auth):
    doc, _, b, line_ids = await _mixed_invoice(client, auth)
    await _issued(client, auth, "credit_note", [_line(b, "Room hire", 33.33, source_line_id=line_ids[1])],
                  original_doc_id=doc)
    assert await _revenue(session, auth) == {"4100": 16.67, "4200": 100.0, "4300": 0.0, "4250": 0.0}


async def test_credit_note_unlinked_keeps_pro_rata(client, session, auth):
    doc, _, _, _ = await _mixed_invoice(client, auth)
    await _issued(client, auth, "credit_note", [_line(None, "Goodwill", 30.0)], original_doc_id=doc)
    # 30.00 over 100.00 : 33.33 : 16.67 of 150.00 = 20.00 : 6.67 : 3.33
    assert await _revenue(session, auth) == {"4100": 13.34, "4200": 80.0, "4300": 26.66, "4250": 0.0}


async def test_reversal_reads_posted_not_current_setting(client, session, auth):
    doc, a, _, line_ids = await _mixed_invoice(client, auth)
    assert (await _patch_item(client, auth, a, income_account="4300")).status_code == 200
    await _issued(client, auth, "credit_note", [_line(a, "Workshop", 40.0, source_line_id=line_ids[0])],
                  original_doc_id=doc)
    assert await _revenue(session, auth) == {"4100": 16.67, "4200": 60.0, "4300": 33.33, "4250": 0.0}
