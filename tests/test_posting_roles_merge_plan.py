# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One merge plan, shared by the preview and the merge itself.

The plan decides which items may merge, what the merged item holds, and which
inventory account its value sits in. A retry of a merge is recognised by what it
asks for, not only by its key. A merge never changes the value of the stock it
combines, and stock that records no inventory account, or stock that is not on hand,
is refused before anything is written.
"""
from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_cost_restatement import _state
from test_posting_roles_merge import _FIELD, _lot, _merge, _remap, _two_accounts

pytestmark = pytest.mark.asyncio

_PREVIEW_FIRST = "Preview this merge first, then confirm it with the plan_fingerprint the preview returned."


async def _ledger_count(session, auth) -> int:
    session.expire_all()
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


async def _items(session, auth) -> dict[str, dict]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).scalars().all()
    return {r.entity_id: dict(r.state) for r in rows}


async def _set_state(session, auth, item_id: str, **fields) -> None:
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": item_id},
                            populate_existing=True)
    row.state = {**row.state, **fields}
    await session.commit()


async def _forget_origin(session, auth, *item_ids: str) -> None:
    for item_id in item_ids:
        row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": item_id},
                                populate_existing=True)
        row.state = {k: v for k, v in row.state.items() if k != _FIELD}
    await session.commit()


# --- Retries are recognised by what they ask for ---------------------------------------


@pytest.mark.parametrize("change", [
    {"target_sku_from": "B"},
    {"resulting_quantity": 3},
    {"resulting_name": "Renamed"},
    {"resulting_sku": "OTHER-SKU"},
    {"resolved_attributes": {"grade": "AA"}},
])
async def test_a_merge_key_reused_for_a_different_merge_is_refused(session, client, auth, change):
    a, b = await _lot(client, auth, 600.0, qty=1), await _lot(client, auth, 400.0, qty=1)
    key = f"merge-{uuid.uuid4().hex}"
    first = await _merge(client, auth, [a, b], idempotency_key=key)
    assert first.status_code == 200, first.text
    body = {"source_entity_ids": [a, b], "target_sku_from": a, "idempotency_key": key, **change}
    if body["target_sku_from"] == "B":
        body["target_sku_from"] = b
    before = await _ledger_count(session, auth)
    again = await client.post("/items/merge", headers=auth["headers"], json=body)
    assert again.status_code == 409, again.text
    assert "already used" in again.json()["detail"]
    assert await _ledger_count(session, auth) == before


async def test_an_exact_retry_of_a_merge_returns_the_first_result(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    key = f"merge-{uuid.uuid4().hex}"
    asked = {"source_entity_ids": [a, b], "target_sku_from": a, "resulting_name": "Pair"}
    preview = await client.post("/items/merge/preview", headers=auth["headers"], json=asked)
    assert preview.status_code == 200, preview.text
    sent = {**asked, "idempotency_key": key, "plan_fingerprint": preview.json()["plan_fingerprint"]}
    first = await client.post("/items/merge", headers=auth["headers"], json=sent)
    assert first.status_code == 200, first.text
    before = await _ledger_count(session, auth)
    # A retry is not previewed again: the same request, its old fingerprint and all,
    # or without one, or with its items listed in another order, is the same merge.
    for again in (sent, {k: v for k, v in sent.items() if k != "plan_fingerprint"},
                  {**sent, "source_entity_ids": [b, a]}):
        r = await client.post("/items/merge", headers=auth["headers"], json=again)
        assert r.status_code == 200, r.text
        assert r.json() == first.json()
    assert await _ledger_count(session, auth) == before


async def _graded_lot(client, auth, cost: float, grade: str) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"LOT-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 1, "sell_by": "piece",
        "status": "available", "cost_total": cost, "attributes": {"grade": grade}})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.parametrize("change", [
    {"source_entity_ids": "ABC"},
    {"target_sku_from": "B"},
    {"resulting_quantity": 3},
    {"resulting_cost_total": 1000.0},
    {"resulting_name": "Renamed"},
    {"resulting_sku": "OTHER-SKU"},
    {"resolved_attributes": {"grade": "B"}},
])
async def test_a_merge_confirmed_with_another_requests_preview_is_refused(session, client, auth, change):
    a, b = await _graded_lot(client, auth, 600.0, "A"), await _graded_lot(client, auth, 400.0, "B")
    c = await _graded_lot(client, auth, 0.0, "A")
    reviewed = {"source_entity_ids": [a, b], "target_sku_from": a, "resolved_attributes": {"grade": "A"}}
    preview = await client.post("/items/merge/preview", headers=auth["headers"], json=reviewed)
    assert preview.status_code == 200, preview.text
    confirmed = {**reviewed, **change, "plan_fingerprint": preview.json()["plan_fingerprint"],
                 "idempotency_key": f"merge-{uuid.uuid4().hex}"}
    confirmed["source_entity_ids"] = [a, b, c] if change.get("source_entity_ids") == "ABC" else [a, b]
    if confirmed["target_sku_from"] == "B":
        confirmed["target_sku_from"] = b
    # The changed request is itself a valid merge: previewed on its own, it would go through.
    alone = await client.post("/items/merge/preview", headers=auth["headers"],
                              json={k: v for k, v in confirmed.items() if k != "plan_fingerprint"})
    assert alone.status_code == 200, alone.text
    items, before = await _items(session, auth), await _ledger_count(session, auth)
    r = await client.post("/items/merge", headers=auth["headers"], json=confirmed)
    assert r.status_code == 409, r.text
    assert "Review the merge again" in r.json()["detail"]
    await session.rollback()
    assert await _items(session, auth) == items
    assert await _ledger_count(session, auth) == before


@pytest.mark.parametrize("key", [None, "fresh"])
async def test_a_merge_confirmed_without_its_preview_is_refused(session, client, auth, key):
    a, b = await _two_accounts(session, client, auth)
    body = {"source_entity_ids": [a, b], "target_sku_from": a}
    if key:
        body["idempotency_key"] = f"merge-{uuid.uuid4().hex}"
    items, before = await _items(session, auth), await _ledger_count(session, auth)
    r = await client.post("/items/merge", headers=auth["headers"], json=body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == _PREVIEW_FIRST
    await session.rollback()
    assert await _items(session, auth) == items
    assert await _ledger_count(session, auth) == before


# --- Stock whose account is not recorded waits -----------------------------------------


async def test_older_stock_with_no_provable_account_is_refused_before_anything_changes(session, client, auth):
    a, b = await _lot(client, auth, 600.0, sku="OLD-A"), await _lot(client, auth, 400.0, sku="OLD-B")
    await _forget_origin(session, auth, a, b)
    items, before = await _items(session, auth), await _ledger_count(session, auth)

    r = await _merge(client, auth, [a, b])
    assert r.status_code == 409, r.text
    assert "Stock OLD-A has no recorded inventory account" in r.json()["detail"]["message"]
    assert r.headers["X-Celerp-Fix"] == "/settings/accounting?tab=posting-accounts"
    await session.rollback()
    assert await _items(session, auth) == items
    assert await _ledger_count(session, auth) == before
    preview = await client.post("/items/merge/preview", headers=auth["headers"],
                                json={"source_entity_ids": [a, b], "target_sku_from": a})
    assert preview.status_code == 409, preview.text


# --- Only stock on hand merges ---------------------------------------------------------


@pytest.mark.parametrize("status", ["sold", "archived", "expired", "disposed", "memo_out", "reserved"])
async def test_stock_that_is_not_on_hand_cannot_be_merged(session, client, auth, status):
    a, b = await _lot(client, auth, 600.0), await _lot(client, auth, 400.0)
    await _set_state(session, auth, b, status=status)
    items, before = await _items(session, auth), await _ledger_count(session, auth)
    r = await _merge(client, auth, [a, b])
    assert r.status_code == 409, r.text
    assert "not on hand" in r.json()["detail"]
    await session.rollback()
    assert await _items(session, auth) == items
    assert await _ledger_count(session, auth) == before


# --- A merge keeps the value of what it combines ---------------------------------------


async def test_a_merged_cost_different_from_the_items_cost_is_refused(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    items, before = await _items(session, auth), await _ledger_count(session, auth)
    r = await _merge(client, auth, [a, b], resulting_cost_total=1200.0)
    assert r.status_code == 422, r.text
    assert "cost correction" in r.json()["detail"]
    await session.rollback()
    assert await _items(session, auth) == items
    assert await _ledger_count(session, auth) == before
    r = await _merge(client, auth, [a, b], resulting_cost_total=1000.0)
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["id"]))["cost_total"] == 1000.0


# --- The preview binds the merge the user confirms -------------------------------------


async def test_a_merge_confirmed_after_its_items_changed_is_refused(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    preview = await client.post("/items/merge/preview", headers=auth["headers"],
                                json={"source_entity_ids": [a, b], "target_sku_from": a})
    assert preview.status_code == 200, preview.text
    fingerprint = preview.json()["plan_fingerprint"]
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)  # a keyed digest: says nothing about cost
    r = await client.patch(f"/items/{b}", headers=auth["headers"],
                           json={"fields_changed": {"cost_total": {"old": 400.0, "new": 450.0}}})
    assert r.status_code == 200, r.text
    items, before = await _items(session, auth), await _ledger_count(session, auth)
    r = await _merge(client, auth, [a, b], plan_fingerprint=fingerprint)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "The merge or its items changed since it was reviewed. Review the merge again."
    await session.rollback()
    assert await _items(session, auth) == items
    assert await _ledger_count(session, auth) == before

    again = await client.post("/items/merge/preview", headers=auth["headers"],
                              json={"source_entity_ids": [a, b], "target_sku_from": a})
    r = await _merge(client, auth, [a, b], plan_fingerprint=again.json()["plan_fingerprint"])
    assert r.status_code == 200, r.text
    assert r.json()["inventory_reclassification"]["moves"][0]["amount"] == 450.0
