# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A late correction to an item's cost follows the cost to wherever it went.

A merge folds its sources' cost into the result, and a sale moves a lot's cost
into cost of goods sold. Correcting a source's cost afterwards carries the same
difference into every merge result downstream and trues up the COGS of a sold
lot, in one transaction, or the correction is refused and nothing changes.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_helpers import merge_items

TZ = "Pacific/Kiritimati"


@pytest.fixture
def ids():
    return {"company_id": uuid.uuid4(), "user_id": uuid.uuid4()}


@pytest_asyncio.fixture
async def auth(session, ids):
    cid, uid = ids["company_id"], ids["user_id"]
    session.add(Company(id=cid, name="CostCo", slug=f"costco-{cid.hex[:8]}",
                        settings={"currency": "USD", "timezone": TZ}))
    session.add(User(id=uid, email=f"admin-{cid.hex[:8]}@test.co", name="Admin", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    from test_helpers import make_authed_token, provision_company_books
    await provision_company_books(session, cid)
    await session.commit()
    token = await make_authed_token(session, str(uid), str(cid), "admin")
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": cid, "user_id": uid}


async def _item(client, auth, cost_total: float | None, qty: float = 1, sku: str | None = None) -> str:
    data = {"sku": sku or f"CR-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": qty,
            "sell_by": "piece", "status": "available"}
    if cost_total is not None:
        data["cost_total"] = cost_total
    r = await client.post("/items", headers=auth["headers"], json=data)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _merge(client, auth, sources: list[str], **extra) -> str:
    r = await merge_items(client, headers=auth["headers"],
                          json={"source_entity_ids": sources, "target_sku_from": sources[0], **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _set_cost(client, auth, item_id: str, cost_total, headers=None, key: str | None = None):
    body = {"fields_changed": {"cost_total": {"old": None, "new": cost_total}}}
    if key:
        body["idempotency_key"] = key
    return await client.patch(f"/items/{item_id}", headers=headers or auth["headers"], json=body)


async def _state(session, auth, entity_id: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": entity_id})
    return row.state if row is not None else {}


async def _cost(session, auth, entity_id: str):
    return (await _state(session, auth, entity_id)).get("cost_total")


async def _invoice(client, session, auth, *item_ids: str) -> str:
    """Invoice each whole lot on its own line, in order, and finalize."""
    lines = []
    for item_id in item_ids:
        state = await _state(session, auth, item_id)
        lines.append({"sku": state["sku"], "name": "Lot", "quantity": state["quantity"],
                      "unit_price": 500.0, "entity_id": item_id})
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}",
        "line_items": lines, "total": 500.0 * len(lines),
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])).status_code == 200
    return doc_id


async def _fulfil(client, doc_id: str, auth, *item_ids: str) -> None:
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": list(item_ids)})
    assert r.status_code == 200, r.text


async def _sell(client, session, auth, *item_ids: str) -> str:
    """Invoice each whole lot on its own line, finalize, and fulfil them."""
    doc_id = await _invoice(client, session, auth, *item_ids)
    await _fulfil(client, doc_id, auth, *item_ids)
    return doc_id


async def _doc_cogs(session, auth, doc_id: str) -> float:
    """Cost of goods sold the document's posted entries recognize in total."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"],
        Projection.entity_type == "journal_entry",
        Projection.entity_id.like(f"je:auto:{doc_id}:%"),
    ))).scalars().all()
    return round(sum(_cogs(r.state) for r in rows if r.state.get("status") == "posted"), 2)


async def _cogs_adjustments(session, auth, doc_id: str) -> dict[str, dict]:
    """Live COGS adjustment JEs of a doc, keyed by JE id."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"],
        Projection.entity_type == "journal_entry",
        Projection.entity_id.like(f"je:auto:{doc_id}:cogs-adj:%"),
    ))).scalars().all()
    return {r.entity_id: r.state for r in rows}


def _cogs(je_state: dict) -> float:
    """Signed 5100 movement of one JE (debit positive)."""
    return round(sum(float(e.get("debit") or 0) - float(e.get("credit") or 0)
                     for e in je_state.get("entries", []) if e["account"] == "5100"), 2)


async def _count(session, auth, **where) -> int:
    q = select(func.count()).select_from(LedgerEntry).where(LedgerEntry.company_id == auth["company_id"])
    for k, v in where.items():
        q = q.where(getattr(LedgerEntry, k) == v)
    return (await session.execute(q)).scalar()


# -- Inventory: the delta follows the merge lineage -------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("new_a, expected_c", [(120.0, 170.0), (70.0, 120.0)])
async def test_source_correction_moves_merge_result_by_the_same_delta(client, session, auth, new_a, expected_c):
    a, b = await _item(client, auth, 100.0), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    assert await _cost(session, auth, c) == 150.0
    r = await _set_cost(client, auth, a, new_a)
    assert r.status_code == 200, r.text
    assert await _cost(session, auth, a) == new_a
    assert await _cost(session, auth, c) == expected_c


@pytest.mark.asyncio
async def test_correction_flows_through_two_generations_exactly_once(client, session, auth):
    a, b, d = (await _item(client, auth, 100.0), await _item(client, auth, 50.0),
               await _item(client, auth, 30.0))
    c = await _merge(client, auth, [a, b])
    e = await _merge(client, auth, [c, d])
    assert await _cost(session, auth, e) == 180.0
    assert (await _set_cost(client, auth, a, 110.0)).status_code == 200
    assert await _cost(session, auth, c) == 160.0
    assert await _cost(session, auth, e) == 190.0
    for eid in (c, e):
        assert await _count(session, auth, entity_id=eid, event_type="item.cost_adjusted") == 1


@pytest.mark.asyncio
async def test_independent_adjustment_of_the_result_is_preserved(client, session, auth):
    a, b = await _item(client, auth, 100.0), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    assert (await _set_cost(client, auth, c, 200.0)).status_code == 200
    assert (await _set_cost(client, auth, a, 120.0)).status_code == 200
    # 200 kept, plus the inherited +20; never rebuilt from source totals (170).
    assert await _cost(session, auth, c) == 220.0


@pytest.mark.asyncio
async def test_cost_price_endpoint_restates_through_lineage(client, session, auth):
    a, b = await _item(client, auth, 100.0, qty=2), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    r = await client.post(f"/items/{a}/price", headers=auth["headers"],
                          json={"price_type": "cost_price", "new_price": 60.0})
    assert r.status_code == 200, r.text
    assert await _cost(session, auth, a) == 120.0
    assert await _cost(session, auth, c) == 170.0


@pytest.mark.asyncio
async def test_the_same_restatement_sent_again_adds_nothing(client, session, auth):
    a, b = await _item(client, auth, 100.0), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    doc = await _sell(client, session, auth, c)
    key = f"restate-{uuid.uuid4().hex}"
    assert (await _set_cost(client, auth, a, 130.0, key=key)).status_code == 200
    before = await _count(session, auth)
    r = await _set_cost(client, auth, a, 130.0, key=key)
    assert r.status_code == 200, r.text
    assert await _count(session, auth) == before
    assert await _cost(session, auth, c) == 180.0
    assert [_cogs(s) for s in (await _cogs_adjustments(session, auth, doc)).values()] == [30.0]


# -- COGS: a sold lot's correction is trued up, dated today -----------------

@pytest.mark.asyncio
@pytest.mark.parametrize("new_a, delta", [(120.0, 20.0), (90.0, -10.0)])
async def test_sold_merge_result_posts_cogs_adjustment(client, session, auth, new_a, delta):
    a, b = await _item(client, auth, 100.0), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    doc = await _sell(client, session, auth, c)
    assert await _cogs_adjustments(session, auth, doc) == {}
    assert (await _set_cost(client, auth, a, new_a)).status_code == 200
    adjustments = await _cogs_adjustments(session, auth, doc)
    assert len(adjustments) == 1
    (je_id, je), = adjustments.items()
    assert re.fullmatch(rf"je:auto:{re.escape(doc)}:cogs-adj:restate-[0-9a-f]{{16}}", je_id)
    assert je["status"] == "posted"
    assert _cogs(je) == delta
    inventory = [x for x in je["entries"] if x["account"] != "5100"]
    assert round(sum(float(x.get("debit") or 0) - float(x.get("credit") or 0) for x in inventory), 2) == -delta
    assert await _cost(session, auth, c) == 150.0 + delta


@pytest.mark.asyncio
async def test_sold_item_correction_is_dated_today_in_business_time(client, session, auth):
    from celerp.services.business_time import business_date_at

    item = await _item(client, auth, 100.0)
    doc = await _sell(client, session, auth, item)
    fin_before = await _count(session, auth, entity_id=f"je:auto:{doc}:fin")
    today = business_date_at(datetime.now(timezone.utc), TZ)
    assert (await _set_cost(client, auth, item, 112.0)).status_code == 200
    (je_id, je), = (await _cogs_adjustments(session, auth, doc)).items()
    assert _cogs(je) == 12.0
    created = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == je_id,
        LedgerEntry.event_type == "acc.journal_entry.created"))).scalars().one()
    assert created.data["ts"] == today
    assert created.metadata_["trigger"] == "item.cost_restated"
    assert created.metadata_["item_id"] == item
    # The sale's own recognition is appended to, never rewritten.
    assert await _count(session, auth, entity_id=f"je:auto:{doc}:fin") == fin_before


@pytest.mark.asyncio
async def test_reversal_keeps_the_corrected_cost_recognized(client, session, auth):
    a, b = await _item(client, auth, 100.0), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    doc = await _sell(client, session, auth, c)
    assert (await _set_cost(client, auth, a, 120.0)).status_code == 200
    assert await _doc_cogs(session, auth, doc) == 170.0
    r = await client.post(f"/docs/{doc}/revert-lines", headers=auth["headers"], json={"line_entity_ids": [c]})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, c))["status"] == "available"
    assert await _cost(session, auth, c) == 170.0
    # The invoice still stands, so it still recognizes the corrected cost of its goods.
    assert await _doc_cogs(session, auth, doc) == 170.0

    await _fulfil(client, doc, auth, c)
    assert await _doc_cogs(session, auth, doc) == 170.0


# -- COGS: a correction before fulfilment reaches the finalized invoice ------

@pytest.mark.asyncio
@pytest.mark.parametrize("fulfil_after", [False, True])
async def test_correction_after_finalize_adjusts_the_recognized_cogs(client, session, auth, fulfil_after):
    item = await _item(client, auth, 100.0)
    doc = await _invoice(client, session, auth, item)
    assert await _doc_cogs(session, auth, doc) == 100.0
    assert (await _set_cost(client, auth, item, 120.0)).status_code == 200
    assert await _doc_cogs(session, auth, doc) == 120.0
    if fulfil_after:
        await _fulfil(client, doc, auth, item)
        assert await _doc_cogs(session, auth, doc) == 120.0


@pytest.mark.asyncio
@pytest.mark.parametrize("correct_while_void", [False, True])
async def test_voiding_and_restoring_the_invoice_keeps_the_corrected_cost(client, session, auth, correct_while_void):
    item = await _item(client, auth, 100.0)
    doc = await _invoice(client, session, auth, item)
    if not correct_while_void:
        assert (await _set_cost(client, auth, item, 120.0)).status_code == 200
    r = await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert await _doc_cogs(session, auth, doc) == 0.0
    if correct_while_void:
        assert (await _set_cost(client, auth, item, 120.0)).status_code == 200
        assert await _doc_cogs(session, auth, doc) == 0.0
    r = await client.post(f"/docs/{doc}/unvoid", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert await _doc_cogs(session, auth, doc) == 120.0


@pytest.mark.asyncio
async def test_correction_of_a_merge_source_reaches_the_invoice_of_the_result(client, session, auth):
    a, b = await _item(client, auth, 100.0), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    doc = await _invoice(client, session, auth, c)
    assert (await _set_cost(client, auth, a, 90.0)).status_code == 200
    assert await _doc_cogs(session, auth, doc) == 140.0
    await _fulfil(client, doc, auth, c)
    assert await _doc_cogs(session, auth, doc) == 140.0


@pytest.mark.asyncio
async def test_reverting_the_invoice_to_draft_ends_its_recognition(client, session, auth):
    item = await _item(client, auth, 100.0)
    doc = await _invoice(client, session, auth, item)
    assert (await _set_cost(client, auth, item, 120.0)).status_code == 200
    r = await client.post(f"/docs/{doc}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert await _doc_cogs(session, auth, doc) == 0.0
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    assert await _doc_cogs(session, auth, doc) == 120.0


@pytest.mark.asyncio
async def test_small_corrections_on_two_lines_round_once_for_the_invoice(client, session, auth):
    a, b = await _item(client, auth, 1.0), await _item(client, auth, 1.0)
    doc = await _sell(client, session, auth, a, b)
    assert await _doc_cogs(session, auth, doc) == 2.0
    assert (await _set_cost(client, auth, a, 1.004)).status_code == 200
    assert (await _set_cost(client, auth, b, 1.004)).status_code == 200
    # 2.008 recognized in total rounds to 2.01, not 2.00 + 0.00 + 0.00.
    assert await _doc_cogs(session, auth, doc) == 2.01


# -- Fail closed: nothing changes when the consequence is not exact ---------

async def _assert_refused(client, session, auth, root: str, watch: list[str], *, new_cost=120.0, fragment: str):
    before = {eid: await _cost(session, auth, eid) for eid in [root, *watch]}
    events = await _count(session, auth)
    r = await _set_cost(client, auth, root, new_cost)
    assert r.status_code == 409, r.text
    assert fragment in r.json()["detail"]
    assert {eid: await _cost(session, auth, eid) for eid in [root, *watch]} == before
    assert await _count(session, auth) == events


@pytest.mark.asyncio
async def test_split_root_refuses_correction(client, session, auth):
    item = await _item(client, auth, 100.0, qty=2)
    r = await client.post(f"/items/{item}/split", headers=auth["headers"],
                          json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text
    await _assert_refused(client, session, auth, item, [], fragment="cannot be carried")


@pytest.mark.asyncio
async def test_split_result_refuses_correction(client, session, auth):
    a, b = await _item(client, auth, 100.0, qty=2), await _item(client, auth, 50.0, qty=2)
    c = await _merge(client, auth, [a, b])
    r = await client.post(f"/items/{c}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text
    await _assert_refused(client, session, auth, a, [c], fragment="cannot be carried")


@pytest.mark.asyncio
async def test_partly_consumed_result_refuses_correction(client, session, auth):
    a, b = await _item(client, auth, 100.0), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    from celerp.events.engine import emit_event
    await emit_event(session, company_id=auth["company_id"], entity_id=c, entity_type="item",
                     event_type="item.consumed", data={"quantity_consumed": 0.5},
                     actor_id=auth["user_id"], location_id=None, source="test",
                     idempotency_key=f"consume-{uuid.uuid4().hex}", metadata_={})
    await session.commit()
    await _assert_refused(client, session, auth, a, [c], fragment="cannot be carried")


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["missing", "cycle"])
async def test_broken_lineage_refuses_correction_atomically(client, session, auth, broken):
    a, b = await _item(client, auth, 100.0), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    d = await _item(client, auth, 10.0)
    e = await _merge(client, auth, [c, d])
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": e})
    state = dict(row.state)
    if broken == "missing":
        state["merged_into"], state["status"] = "item:gone", "merged"
    else:
        state["merged_into"], state["status"] = c, "merged"
    row.state = state
    await session.commit()
    await _assert_refused(client, session, auth, a, [c, e], fragment="lineage")


@pytest.mark.asyncio
async def test_sale_without_an_exact_invoice_line_refuses_correction(client, session, auth):
    # Sold by converting a memo to an invoice: no invoice line ever fulfilled it.
    item = await _item(client, auth, 100.0)
    sku = (await _state(session, auth, item))["sku"]
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "memo", "line_items": [
        {"entity_id": item, "sku": sku, "name": sku, "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    for path, body in ((f"/docs/{memo}/finalize", {}), (f"/docs/{memo}/fulfill-lines", {"line_entity_ids": [item]}),
                       (f"/docs/{memo}/convert", {})):
        r = await client.post(path, headers=auth["headers"], json=body)
        assert r.status_code == 200, r.text
    assert (await _state(session, auth, item))["status"] == "sold"
    await _assert_refused(client, session, auth, item, [], fragment="invoice line")


# -- Every cost writer goes through the same operation ----------------------

@pytest.mark.asyncio
async def test_cost_permission_is_unchanged(client, session):
    from test_helpers import perm_setup

    ctx = await perm_setup(client, session)
    admin, operator = ctx["admin_h"], ctx["operator_h"]
    ids = []
    for cost in (100.0, 50.0):
        r = await client.post("/items", headers=admin, json={
            "sku": f"P-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 1, "sell_by": "piece",
            "status": "available", "cost_total": cost})
        ids.append(r.json()["id"])
    r = await merge_items(client, headers=admin,
                          json={"source_entity_ids": ids, "target_sku_from": ids[0]})
    c = r.json()["id"]
    body = {"fields_changed": {"cost_total": {"old": 100.0, "new": 120.0}}}
    assert (await client.patch(f"/items/{ids[0]}", headers=operator, json=body)).status_code == 403
    r = await client.post(f"/items/{ids[0]}/price", headers=operator,
                          json={"price_type": "cost_total", "new_price": 120.0})
    assert r.status_code == 403
    assert (await client.get(f"/items/{c}", headers=admin)).json()["cost_total"] == 150.0
    assert (await client.patch(f"/items/{ids[0]}", headers=admin, json=body)).status_code == 200
    assert (await client.get(f"/items/{c}", headers=admin)).json()["cost_total"] == 170.0


@pytest.mark.asyncio
async def test_csv_cost_upsert_restates_through_lineage(client, session, auth):
    a, b = await _item(client, auth, 100.0), await _item(client, auth, 50.0)
    c = await _merge(client, auth, [a, b])
    record = {"entity_id": a, "event_type": "item.patched", "source": "csv_import",
              "data": {"name": "Lot", "cost_total": 130.0},
              "idempotency_key": f"csv:item:{a}:patch:{uuid.uuid4().hex}"}
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [record]})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1, r.json()
    assert await _cost(session, auth, a) == 130.0
    assert await _cost(session, auth, c) == 180.0
    # The same import sent again adds nothing.
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [record]})
    assert r.json()["skipped"] == 1
    assert await _cost(session, auth, c) == 180.0


@pytest.mark.asyncio
async def test_csv_cost_upsert_that_cannot_reconcile_changes_nothing(client, session, auth):
    a, b = await _item(client, auth, 100.0, qty=2), await _item(client, auth, 50.0, qty=2)
    c = await _merge(client, auth, [a, b])
    r = await client.post(f"/items/{c}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text
    record = {"entity_id": a, "event_type": "item.patched", "source": "csv_import",
              "data": {"name": "Renamed", "cost_total": 130.0},
              "idempotency_key": f"csv:item:{a}:patch:{uuid.uuid4().hex}"}
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [record]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 0 and any("cannot be carried" in e for e in body["errors"]), body
    state = await _state(session, auth, a)
    assert state["cost_total"] == 100.0 and state["name"] == "Lot"


# -- Zero-quantity unit cost: one normalization for price, edit and CSV -------

async def _set_unit_cost_at_zero(client, auth, item_id: str, path: str) -> None:
    if path == "price":
        r = await client.post(f"/items/{item_id}/price", headers=auth["headers"],
                              json={"price_type": "cost_price", "new_price": 12.5})
    elif path == "patch":
        r = await client.patch(f"/items/{item_id}", headers=auth["headers"],
                               json={"fields_changed": {"cost_price": {"old": None, "new": 12.5}}})
    else:
        record = {"entity_id": item_id, "event_type": "item.patched", "source": "csv_import",
                  "data": {"name": "Lot", "cost_price": 12.5},
                  "idempotency_key": f"csv:item:{item_id}:patch:{uuid.uuid4().hex}"}
        r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [record]})
        assert r.json()["updated"] == 1, r.json()
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["price", "patch", "csv"])
async def test_zero_quantity_unit_cost_survives_new_stock_and_sale(client, session, auth, path):
    # A lot that once had a cost basis, now empty: the new unit cost replaces it.
    item_id = await _item(client, auth, 40.0, qty=0)
    await _set_unit_cost_at_zero(client, auth, item_id, path)
    state = await _state(session, auth, item_id)
    assert state.get("cost_price") == 12.5
    assert state.get("cost_total") is None and state.get("cost_base") is None

    r = await client.post(f"/items/{item_id}/adjust", headers=auth["headers"], json={"new_qty": 4})
    assert r.status_code == 200, r.text
    item = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert item["cost_price"] == 12.5
    assert item["cost_total"] == 50.0

    doc_id = await _sell(client, session, auth, item_id)
    session.expire_all()
    fin = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": f"je:auto:{doc_id}:fin"})
    assert _cogs(fin.state) == 50.0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["price", "patch", "csv"])
async def test_unit_cost_with_stock_sets_the_basis(client, session, auth, path):
    item_id = await _item(client, auth, None, qty=4)
    await _set_unit_cost_at_zero(client, auth, item_id, path)
    state = await _state(session, auth, item_id)
    assert state["cost_base"] == 50.0 and state["cost_total"] == 50.0
    assert "cost_price" not in state


@pytest.mark.asyncio
async def test_correction_reaches_every_invoice_that_recognized_a_sold_lot(client, session, auth):
    item = await _item(client, auth, 100.0)
    shipped = await _invoice(client, session, auth, item)
    waiting = await _invoice(client, session, auth, item)
    assert await _doc_cogs(session, auth, waiting) == 100.0
    await _fulfil(client, shipped, auth, item)
    assert (await _set_cost(client, auth, item, 120.0)).status_code == 200
    assert await _doc_cogs(session, auth, shipped) == 120.0
    assert await _doc_cogs(session, auth, waiting) == 120.0
