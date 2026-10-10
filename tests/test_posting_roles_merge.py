# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Merging lots held in different inventory accounts moves their value with them.

The merged lot keeps the inventory account of the lot it is merged into. The
carrying value of every other lot held elsewhere is moved into that account by one
entry, posted with the merge: one credit per account the value leaves and one
debit to the surviving account. Lots that already share an account post nothing.
Undoing the merge reverses that exact entry and puts every lot back where it was.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.account_roles import set_role
from celerp.services.company_lock import locked_company
from test_helpers import invite_user, merge_items
from test_cost_restatement import _state
from test_posting_roles_landed import _freight_bill
from test_receipt_accounting import _finalize, _receive
from ui.i18n import t

_FIELD = "inventory_account_code"
_A_TO_B = {"destination": "1130-OB", "destination_name": "Inventory - Opening Balance", "currency": "USD",
           "moves": [{"account": "1131", "name": "Stock 1131", "amount": 400.0}]}


async def _account(client, auth, code: str) -> str:
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": code, "name": f"Stock {code}", "account_type": "asset", "parent_code": "1130"})
    assert r.status_code == 200, r.text
    return code


async def _remap(session, client, auth, code: str, *, create: bool = True, role: str = "inventory_opening") -> None:
    """Point the inventory account that new lots of the kind ``role`` record at ``code``."""
    if create:
        await _account(client, auth, code)
    await set_role(session, auth["company_id"], role, code)
    await session.commit()


async def _lot(client, auth, cost: float, qty: float = 1, sku: str | None = None) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku or f"LOT-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": qty,
        "sell_by": "piece", "status": "available", "cost_total": cost})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _lock_books(session, auth) -> None:
    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, "lock_date": "2999-12-31"}
    await session.commit()


async def _merge(client, auth, sources: list[str], **extra):
    return await merge_items(client, headers=auth["headers"],
                             json={"source_entity_ids": sources, "target_sku_from": sources[0], **extra})


async def _merged(client, auth, sources: list[str], **extra) -> dict:
    r = await _merge(client, auth, sources, **extra)
    assert r.status_code == 200, r.text
    return r.json()


async def _reclass(session, auth, merged_id: str) -> dict | None:
    return (await _state(session, auth, f"je:auto:{merged_id}:merge-reclass")) or None


def _lines(je: dict) -> dict[str, tuple[float, float]]:
    return {e["account"]: (e.get("debit") or 0, e.get("credit") or 0) for e in je["entries"]}


async def _entries(session, auth) -> dict[str, dict]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    return {r.entity_id: r.state for r in rows}


async def _sell(client, auth, lot: str, qty: float) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"entity_id": lot, "name": "Lot", "quantity": qty,
                                               "unit_price": 50.0, "sell_by": "piece"}],
        "total": 50.0 * qty})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    return doc


async def _two_accounts(session, client, auth, a_cost: float = 600.0, b_cost: float = 400.0):
    """Lot A booked to 1130-OB and lot B booked to 1131 (the worked example)."""
    a = await _lot(client, auth, a_cost, sku="A")
    await _remap(session, client, auth, "1131")
    b = await _lot(client, auth, b_cost, sku="B")
    return a, b


@pytest.mark.asyncio
async def test_lots_in_one_account_merge_with_no_reclassification(session, client, auth):
    a1, a2 = await _lot(client, auth, 600.0), await _lot(client, auth, 400.0)
    before = await _entries(session, auth)
    out = await _merged(client, auth, [a1, a2])
    assert out.get("inventory_reclassification") is None
    assert await _reclass(session, auth, out["id"]) is None
    assert set(await _entries(session, auth)) == set(before)
    merged = await _state(session, auth, out["id"])
    assert (merged[_FIELD], merged["cost_total"]) == ("1130-OB", 1000.0)


@pytest.mark.asyncio
async def test_lots_in_two_accounts_move_the_other_value_into_the_surviving_account(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    out = await _merged(client, auth, [a, b])
    merged = await _state(session, auth, out["id"])
    assert (merged[_FIELD], merged["cost_total"]) == ("1130-OB", 1000.0)
    je = await _reclass(session, auth, out["id"])
    assert je["status"] == "posted"
    assert _lines(je) == {"1130-OB": (400.0, 0), "1131": (0, 400.0)}
    assert out["inventory_reclassification"] == _A_TO_B
    # Traceable to the merge it belongs to.
    row = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"],
        Projection.entity_id == f"je:auto:{out['id']}:merge-reclass"))).scalar_one()
    assert row.state["memo"].startswith("Inventory reclassified on merge")


@pytest.mark.asyncio
async def test_the_surviving_lot_keeps_its_own_account_not_the_current_one(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    await _remap(session, client, auth, "1132")
    out = await _merged(client, auth, [b, a])  # B survives
    assert (await _state(session, auth, out["id"]))[_FIELD] == "1131"
    assert _lines(await _reclass(session, auth, out["id"])) == {"1131": (600.0, 0), "1130-OB": (0, 600.0)}


@pytest.mark.asyncio
async def test_three_accounts_post_one_credit_per_account_for_the_exact_total(session, client, auth):
    a = await _lot(client, auth, 600.0, sku="A")
    await _remap(session, client, auth, "1131")
    b1, b2 = await _lot(client, auth, 150.25, sku="B1"), await _lot(client, auth, 99.75, sku="B2")
    await _remap(session, client, auth, "1132")
    c = await _lot(client, auth, 75.5, sku="C")
    out = await _merged(client, auth, [a, b1, c, b2])
    je = await _reclass(session, auth, out["id"])
    assert _lines(je) == {"1130-OB": (325.5, 0), "1131": (0, 250.0), "1132": (0, 75.5)}
    assert len(je["entries"]) == 3
    assert out["inventory_reclassification"]["moves"] == [
        {"account": "1131", "name": "Stock 1131", "amount": 250.0},
        {"account": "1132", "name": "Stock 1132", "amount": 75.5}]
    assert (await _state(session, auth, out["id"]))["cost_total"] == 925.5


@pytest.mark.asyncio
async def test_a_partly_sold_lot_moves_only_the_value_it_still_carries(session, client, auth):
    a = await _lot(client, auth, 600.0, sku="A")
    await _remap(session, client, auth, "1131")
    b = await _lot(client, auth, 400.0, qty=4, sku="B")
    await _sell(client, auth, b, 1)
    state = await _state(session, auth, b)
    assert (state["quantity"], state["cost_total"]) == (3, 300.0)
    out = await _merged(client, auth, [a, b])
    assert _lines(await _reclass(session, auth, out["id"])) == {"1130-OB": (300.0, 0), "1131": (0, 300.0)}


@pytest.mark.asyncio
async def test_a_lot_carrying_landed_cost_moves_its_full_carrying_value(session, client, auth):
    a = await _lot(client, auth, 600.0, sku="A")
    await _remap(session, client, auth, "1131", role="inventory_purchased")
    bill = await _freight_bill(client, auth)
    await _finalize(client, auth, bill)
    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": "GOODS", "name": "Goods",
                                            "quantity_received": 2})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    state = await _state(session, auth, parcel)
    assert (state[_FIELD], state["cost_total"]) == ("1131", 40.0)  # 30 goods + 10 freight
    out = await _merged(client, auth, [a, parcel], resolved_attributes={})
    assert _lines(await _reclass(session, auth, out["id"])) == {"1130-OB": (40.0, 0), "1131": (0, 40.0)}


@pytest.mark.asyncio
async def test_a_merged_lot_sold_relieves_its_whole_cost_from_the_surviving_account(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    out = await _merged(client, auth, [a, b])
    doc = await _sell(client, auth, out["id"], 2)
    relief: dict[str, float] = {}
    for eid, je in (await _entries(session, auth)).items():
        if eid.startswith(f"je:auto:{doc}:") and je["status"] == "posted":
            for e in je["entries"]:
                if e["account"].startswith("113"):
                    relief[e["account"]] = relief.get(e["account"], 0) + (e.get("credit") or 0) - (e.get("debit") or 0)
    assert relief == {"1130-OB": 1000.0}


@pytest.mark.asyncio
async def test_undoing_a_merge_reverses_the_exact_entry_and_restores_every_lot(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    out = await _merged(client, auth, [a, b])
    await _remap(session, client, auth, "1132")  # today's account must not matter
    r = await client.post(f"/items/{out['id']}/undo-merge", headers=auth["headers"])
    assert r.status_code == 200, r.text
    je = await _reclass(session, auth, out["id"])
    assert je["status"] == "void"
    assert _lines(je) == {"1130-OB": (400.0, 0), "1131": (0, 400.0)}
    for lot, account, cost in ((a, "1130-OB", 600.0), (b, "1131", 400.0)):
        state = await _state(session, auth, lot)
        assert (state["status"], state.get("merged_into"), state[_FIELD], state["cost_total"]) == (
            "available", None, account, cost)
    merged = await _state(session, auth, out["id"])
    assert (merged["status"], merged["quantity"]) == ("archived", 0)
    # The restored lot sells from the account it came in on.
    doc = await _sell(client, auth, b, 1)
    credits = {e["account"]: e.get("credit") for eid, je in (await _entries(session, auth)).items()
               if eid.startswith(f"je:auto:{doc}:") for e in je["entries"] if e["account"].startswith("113")}
    assert credits == {"1131": 400.0}
    # A second undo changes nothing.
    r = await client.post(f"/items/{out['id']}/undo-merge", headers=auth["headers"])
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_undoing_a_merge_is_refused_once_the_merged_lot_has_moved_on(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    out = await _merged(client, auth, [a, b])
    await _sell(client, auth, out["id"], 1)
    r = await client.post(f"/items/{out['id']}/undo-merge", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert (await _reclass(session, auth, out["id"]))["status"] == "posted"
    assert (await _state(session, auth, b))["status"] == "merged"


@pytest.mark.asyncio
async def test_retrying_a_merge_returns_the_first_result_and_moves_the_value_once(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    key = f"merge-{uuid.uuid4().hex}"
    first = await _merged(client, auth, [a, b], idempotency_key=key)
    again = await _merged(client, auth, [a, b], idempotency_key=key)
    assert again == first
    entries = await _entries(session, auth)
    assert [eid for eid in entries if eid.endswith(":merge-reclass")] == [f"je:auto:{first['id']}:merge-reclass"]
    items = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item",
        Projection.entity_id.not_in([a, b])))).scalars().all()
    assert [p.entity_id for p in items] == [first["id"]]


@pytest.mark.asyncio
async def test_a_locked_period_refuses_the_merge_and_changes_nothing(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    await _lock_books(session, auth)
    before = await _entries(session, auth)
    r = await _merge(client, auth, [a, b])
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("error.period_locked", "en", date="2999-12-31")
    assert await _entries(session, auth) == before
    for lot in (a, b):
        state = await _state(session, auth, lot)
        assert (state["status"], state.get("merged_into")) == ("available", None)


@pytest.mark.asyncio
async def test_undoing_a_merge_in_a_locked_period_is_refused_and_changes_nothing(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    out = await _merged(client, auth, [a, b])
    await _lock_books(session, auth)
    r = await client.post(f"/items/{out['id']}/undo-merge", headers=auth["headers"])
    assert r.status_code == 422, r.text
    assert "locked through" in r.text
    assert (await _reclass(session, auth, out["id"]))["status"] == "posted"
    assert (await _state(session, auth, b))["status"] == "merged"
    assert (await _state(session, auth, out["id"]))["status"] == "available"


@pytest.mark.asyncio
async def test_the_merge_preview_names_the_value_that_will_move(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    r = await client.post("/items/merge/preview", headers=auth["headers"],
                          json={"source_entity_ids": [a, b], "target_sku_from": a})
    assert r.status_code == 200, r.text
    assert r.json()["inventory_reclassification"] == _A_TO_B
    a2 = await _lot(client, auth, 5.0)
    a3 = await _lot(client, auth, 5.0)
    r = await client.post("/items/merge/preview", headers=auth["headers"],
                          json={"source_entity_ids": [a2, a3], "target_sku_from": a2})
    assert r.status_code == 200, r.text
    assert r.json()["inventory_reclassification"] is None
    assert await _reclass(session, auth, "anything") is None


@pytest.mark.asyncio
async def test_a_role_that_cannot_see_cost_is_told_the_accounts_but_not_the_amount(session, client, auth):
    a, b = await _two_accounts(session, client, auth)
    op = {"Authorization": f"Bearer {await invite_user(client, session, auth['headers'], 'operator@example.com', 'operator')}"}
    r = await client.post("/items/merge/preview", headers=op, json={"source_entity_ids": [a, b], "target_sku_from": a})
    assert r.status_code == 200, r.text
    hidden = {**_A_TO_B, "moves": [{"account": "1131", "name": "Stock 1131", "amount": None}]}
    assert r.json()["inventory_reclassification"] == hidden
    r = await merge_items(client, headers=op, json={"source_entity_ids": [a, b], "target_sku_from": a})
    assert r.status_code == 200, r.text
    assert r.json()["inventory_reclassification"] == hidden
    assert _lines(await _reclass(session, auth, r.json()["id"])) == {"1130-OB": (400.0, 0), "1131": (0, 400.0)}


async def _costless_lot(client, auth) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"FREE-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 1, "sell_by": "piece",
        "status": "available"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_account", [False, True])
async def test_merging_a_costless_lot_with_a_costed_one_keeps_the_booked_value(session, client, auth, cross_account):
    """Red before: a source with no cost made the merged lot costless, so the books kept
    600 against a lot holding nothing."""
    from stock_books import assert_settled
    a = await _lot(client, auth, 600.0)
    if cross_account:
        await _remap(session, client, auth, "1131")
    b = await _costless_lot(client, auth)
    c = await _lot(client, auth, 400.0)
    out = await _merged(client, auth, [a, b, c])
    merged = await _state(session, auth, out["id"])
    assert (merged[_FIELD], merged["cost_total"]) == ("1130-OB", 1000.0)
    await assert_settled(client, session, auth)
