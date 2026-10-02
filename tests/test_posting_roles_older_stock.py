# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Stock from before lots recorded their inventory account.

Each older lot takes the account its own history proves: stock a receipt or a
production run brought in sits where that entry booked it, and stock entered by
hand sits in the account the opening inventory entry carries it on. Nothing is
assumed for the company as a whole. A lot whose history proves no single account
refuses to move its cost until someone picks its account, and the pick is checked
against what that account actually holds.

The companies here are built the way an older release wrote them: no posting
accounts in the settings, journal lines without roles, every receipt booked to
1130-P and pre-system stock carried on 1130-OB by the opening inventory entry.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.auto_je import _emit_auto_posted_je
from celerp.services.company_lock import locked_company
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net
from test_posting_roles_lot_origin import _books_match_lots, _open_books
from test_posting_roles_lots import _lot, _sell
from test_posting_roles_merge import _merge
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

_FIELD = "inventory_account_code"
_REPAIR = "Settings > Accounting > Posting accounts"


async def _older_release(session, auth) -> None:
    """The company as an older release left it: no posting accounts at all."""
    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if not k.startswith("posting_")}
    await session.commit()


async def _received(session, auth, cost: float, *, onto: str | None = None) -> str:
    """Goods received on a purchase order the way an older release booked them: a new
    lot (or more stock on the lot ``onto``) and a receipt entry debiting 1130-P."""
    cid, uid = auth["company_id"], auth["user_id"]
    po = f"doc:{uuid.uuid4()}"
    if onto is None:
        lot = f"item:{uuid.uuid4()}"
        await emit_event(session, company_id=cid, entity_id=lot, entity_type="item", event_type="item.created",
                         data={"sku": f"RCV-{uuid.uuid4().hex[:6]}", "name": "Received", "quantity": 1,
                               "sell_by": "piece", "status": "available", "cost_total": cost},
                         actor_id=uid, location_id=None, source="api",
                         idempotency_key=f"{po}:line:0", metadata_={"source_doc": po})
    else:
        lot = onto
        state = await _state(session, auth, onto)
        await emit_event(session, company_id=cid, entity_id=lot, entity_type="item",
                         event_type="item.quantity.adjusted",
                         data={"new_qty": float(state["quantity"]) + 1,
                               "cost_base": float(state["cost_total"]) + cost},
                         actor_id=uid, location_id=None, source="api",
                         idempotency_key=f"{po}:line:0", metadata_={"source_doc": po})
    await _emit_auto_posted_je(
        session, company_id=cid, user_id=uid, je_id=f"je:auto:{po}:rcv:1",
        idem_create=f"{po}:rcv:c", idem_posted=f"{po}:rcv:p", memo=f"Auto JE for {po} received",
        entries=[{"account": "1130-P", "debit": cost, "credit": 0.0},
                 {"account": "2110", "debit": 0.0, "credit": cost}],
        metadata_={"trigger": "doc.received", "doc_id": po})
    await session.commit()
    return lot


async def _opening_entry(session, auth, amount: float) -> None:
    """The opening inventory entry an older release posted for pre-system stock."""
    cid = auth["company_id"]
    await _emit_auto_posted_je(
        session, company_id=cid, user_id=auth["user_id"], je_id=f"je:auto:opening-inventory:{cid}",
        idem_create=f"opening-inv:{cid}:c:{amount}", idem_posted=f"opening-inv:{cid}:p:{amount}",
        memo="Opening inventory balance (pre-system stock)",
        entries=[{"account": "1130-OB", "debit": amount, "credit": 0.0},
                 {"account": "3200", "debit": 0.0, "credit": amount}],
        metadata_={"trigger": "opening_inventory.auto"})
    await session.commit()


async def _books(session, client, auth, purchased: float, opening: float) -> None:
    """1130-P and 1130-OB hold exactly the value of the lots on hand that record them."""
    await _open_books(client, auth)
    assert await _books_match_lots(session, auth, "1130-P", "1130-OB") == {"1130-P": purchased, "1130-OB": opening}


async def _sold(client, auth, *lots: tuple[str, float]) -> None:
    """Sell the lots and ship them, so the stock leaves the books."""
    inv = await _sell(client, auth, *lots)
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [lot for lot, _ in lots]})
    assert r.status_code == 200, r.text


async def _unrecorded(session, auth) -> set[str]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).scalars().all()
    return {r.entity_id for r in rows if not r.state.get(_FIELD)}


async def _choose(client, auth, item_id: str, code: str):
    return await client.put(f"/accounting/posting-accounts/older-stock/{item_id}", headers=auth["headers"],
                            json={"code": code})


async def test_a_mixed_older_company_keeps_each_account_equal_to_its_lots(session, client, auth):
    await _older_release(session, auth)
    opening_1, opening_2 = await _lot(client, auth, 30.0), await _lot(client, auth, 40.0)
    bought_1, bought_2 = await _received(session, auth, 20.0), await _received(session, auth, 60.0)
    await _opening_entry(session, auth, 70.0)
    assert {opening_1, opening_2, bought_1, bought_2} <= await _unrecorded(session, auth)

    await _startup(session)
    assert [(await _state(session, auth, lot))[_FIELD] for lot in (opening_1, opening_2, bought_1, bought_2)] \
        == ["1130-OB", "1130-OB", "1130-P", "1130-P"]
    await _books(session, client, auth, purchased=80.0, opening=70.0)

    r = await _merge(client, auth, [opening_1, bought_1])
    assert r.status_code == 200, r.text
    merged = r.json()["id"]
    assert (await _state(session, auth, merged))[_FIELD] == "1130-OB"
    await _books(session, client, auth, purchased=60.0, opening=90.0)

    await _sold(client, auth, (opening_2, 1), (bought_2, 1))
    await _books(session, client, auth, purchased=0.0, opening=50.0)

    await _sold(client, auth, (merged, 2))
    await _books(session, client, auth, purchased=0.0, opening=0.0)


async def test_an_older_lot_whose_history_proves_no_single_account_refuses_to_move_its_cost(session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 10.0)
    await _opening_entry(session, auth, 10.0)
    await _received(session, auth, 15.0, onto=lot)  # half its value now sits on each account
    other = await _lot(client, auth, 5.0)
    await _startup(session)
    assert lot in await _unrecorded(session, auth)

    before = (await _account_net(session, auth["company_id"], "1130-P"),
              await _account_net(session, auth["company_id"], "1130-OB"))
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "total": 50.0,
        "line_items": [{"entity_id": lot, "name": "Lot", "quantity": 2, "unit_price": 25.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert "has no recorded inventory account" in r.json()["detail"] and _REPAIR in r.json()["detail"]
    assert r.headers["X-Celerp-Fix"] == "/settings/accounting?tab=posting-accounts"

    r = await _merge(client, auth, [other, lot])
    assert r.status_code == 409, r.text
    assert _REPAIR in r.json()["detail"]

    # Neither account holds the whole lot, so neither can be picked for it.
    for code in ("1130-P", "1130-OB"):
        r = await _choose(client, auth, lot, code)
        assert r.status_code == 422, r.text
        assert "does not hold" in r.json()["detail"]
    assert lot in await _unrecorded(session, auth)
    assert (await _account_net(session, auth["company_id"], "1130-P"),
            await _account_net(session, auth["company_id"], "1130-OB")) == before


async def test_an_older_lot_moves_its_cost_once_its_account_is_picked(session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 30.0)  # no opening entry carries it yet
    await _startup(session)
    assert lot in await _unrecorded(session, auth)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "total": 50.0,
        "line_items": [{"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 50.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"])).status_code == 409

    await _open_books(client, auth)  # the opening inventory entry now carries it on 1130-OB
    r = await _choose(client, auth, lot, "1130-P")
    assert r.status_code == 422, r.text
    assert "does not hold" in r.json()["detail"]
    r = await _choose(client, auth, lot, "1210")
    assert r.status_code == 422, r.text
    r = await _choose(client, auth, lot, "1130-OB")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, lot))[_FIELD] == "1130-OB"
    r = await _choose(client, auth, lot, "1130-OB")
    assert r.status_code == 409, r.text

    await _books(session, client, auth, purchased=0.0, opening=30.0)
    await _sold(client, auth, (lot, 1))
    await _books(session, client, auth, purchased=0.0, opening=0.0)


async def test_a_part_of_an_older_lot_takes_the_account_its_lot_proves(session, client, auth):
    await _older_release(session, auth)
    lot = await _lot(client, auth, 50.0, qty=10)
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 3}]})
    assert r.status_code == 200, r.text
    parts = await _unrecorded(session, auth)
    assert lot in parts and len(parts) == 2
    await _opening_entry(session, auth, 50.0)

    await _startup(session)
    assert [(await _state(session, auth, part))[_FIELD] for part in parts] == ["1130-OB", "1130-OB"]
    await _books(session, client, auth, purchased=0.0, opening=50.0)
