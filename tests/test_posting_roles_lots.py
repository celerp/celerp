# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every inventory lot keeps the inventory account its value was booked into.

A new lot takes the company's current inventory account; selling, fulfilling,
returning or splitting a lot moves its value on that same account whatever the
account is set to by then. A lot from before lots recorded their account uses
the account the company's history proves, and one with no provable account
refuses to move its cost rather than guess.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.account_roles import set_role
from celerp.services.company_lock import locked_company
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net

_FIELD = "inventory_account_code"


async def _new_inventory_account(client, auth, code: str = "1131") -> str:
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": code, "name": f"Stock {code}", "account_type": "asset", "parent_code": "1130"})
    assert r.status_code == 200, r.text
    return code


async def _remap(session, auth, code: str) -> None:
    await set_role(session, auth["company_id"], "inventory_purchased", code)
    await session.commit()


async def _lot(client, auth, cost: float, qty: float = 1, sku: str | None = None) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku or f"LOT-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": qty,
        "sell_by": "piece", "status": "available", "cost_total": cost})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _sell(client, auth, *lots: tuple[str, float]) -> str:
    lines = []
    for item_id, qty in lots:
        lines.append({"entity_id": item_id, "name": "Lot", "quantity": qty, "unit_price": 50.0,
                      "sell_by": "piece"})
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": lines, "total": 50.0 * sum(q for _, q in lots)})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc


async def _forget_origin(session, auth, item_id: str) -> None:
    """Make a lot look like one created before lots recorded their account."""
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": item_id},
                            populate_existing=True)
    row.state = {k: v for k, v in row.state.items() if k != _FIELD}
    await session.commit()


def _credits(je: dict) -> dict[str, float]:
    """The inventory credits of an entry, by account."""
    return {e["account"]: e["credit"] for e in je["entries"]
            if e.get("credit") and e["account"].startswith("113")}


@pytest.mark.asyncio
async def test_a_new_lot_records_the_inventory_account_it_is_booked_into(session, client, auth):
    first = await _lot(client, auth, 10.0)
    await _remap(session, auth, await _new_inventory_account(client, auth))
    second = await _lot(client, auth, 10.0)
    assert (await _state(session, auth, first))[_FIELD] == "1130-P"
    assert (await _state(session, auth, second))[_FIELD] == "1131"


@pytest.mark.asyncio
async def test_a_sale_relieves_each_lot_on_the_account_it_was_booked_into(session, client, auth):
    old = await _lot(client, auth, 30.0)
    await _remap(session, auth, await _new_inventory_account(client, auth))
    new = await _lot(client, auth, 12.0)
    inv = await _sell(client, auth, (old, 1), (new, 1))
    je = await _state(session, auth, f"je:auto:{inv}:fin")
    assert _credits(je)["1130-P"] == 30.0 and _credits(je)["1131"] == 12.0
    roles = {e["account"]: e.get("account_roles") for e in je["entries"]}
    assert roles["1130-P"] == ["inventory_purchased"] and roles["1131"] == ["inventory_purchased"]
    assert {e["account"]: e["debit"] for e in je["entries"] if e.get("debit")}["5100"] == 42.0


@pytest.mark.asyncio
async def test_fulfilling_after_a_remap_moves_nothing_onto_the_new_account(session, client, auth):
    lot = await _lot(client, auth, 30.0)
    inv = await _sell(client, auth, (lot, 1))
    await _remap(session, auth, await _new_inventory_account(client, auth))
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    cid = auth["company_id"]
    assert await _account_net(session, cid, "1131") == 0.0
    assert await _account_net(session, cid, "5100") == 30.0


@pytest.mark.asyncio
async def test_a_split_lot_keeps_its_account_in_every_part(session, client, auth):
    lot = await _lot(client, auth, 20.0, qty=2)
    await _remap(session, auth, await _new_inventory_account(client, auth))
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).scalars().all()
    parts = [p.state for p in rows if p.entity_id == lot or p.state.get("split_from") == lot
             or p.state.get("parent_id") == lot]
    assert len(parts) >= 2
    assert {s.get(_FIELD) for s in parts} == {"1130-P"}


@pytest.mark.asyncio
async def test_an_older_lot_relieves_the_account_the_company_history_proves(session, client, auth):
    lot = await _lot(client, auth, 30.0)
    await _forget_origin(session, auth, lot)
    await _remap(session, auth, await _new_inventory_account(client, auth))
    inv = await _sell(client, auth, (lot, 1))
    assert _credits(await _state(session, auth, f"je:auto:{inv}:fin")) == {"1130-P": 30.0}


@pytest.mark.asyncio
async def test_an_older_lot_with_no_provable_account_refuses_to_move_its_cost(session, client, auth):
    lot = await _lot(client, auth, 30.0)
    await _forget_origin(session, auth, lot)
    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "posting_legacy_lot_account"}
    await session.commit()
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "total": 50.0,
        "line_items": [{"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 50.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert "no recorded inventory account" in r.json()["detail"]
    assert r.headers["X-Celerp-Fix"] == "/settings/accounting?tab=posting-accounts"


@pytest.mark.asyncio
async def test_a_lot_account_is_not_editable(session, client, auth):
    lot = await _lot(client, auth, 30.0)
    r = await client.patch(f"/items/{lot}", headers=auth["headers"], json={
        "fields_changed": {_FIELD: {"old": "1130-P", "new": "1131"}}})
    assert r.status_code == 422, r.text
    assert (await _state(session, auth, lot))[_FIELD] == "1130-P"


@pytest.mark.asyncio
async def test_a_company_without_posting_accounts_records_no_lot_account(session, client, auth):
    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if not k.startswith("posting_")}
    await session.commit()
    lot = await _lot(client, auth, 0.0)
    assert _FIELD not in await _state(session, auth, lot)
