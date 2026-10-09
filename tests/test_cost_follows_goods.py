# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The cost of goods follows the goods.

A finalized invoice sets aside (costs) goods on hand it has not shipped yet. When another
invoice ships goods the first one set aside, the cost moves with them: the shipping
invoice takes the cost over from the one that set them aside (Dr Cost of goods sold for
the shipper, Cr Cost of goods sold for the other), inventory is not credited a second
time, and the invoice that lost its goods is costed when it ships, like a sale of goods
not yet in stock. The shipper is told, so the move is never silent. Restoring a voided
invoice sets aside only goods still on hand that no other open invoice holds.

At every step inventory is credited once per unit: cost of goods sold is the cost of the
goods shipped plus the goods set aside, the inventory account carries the rest of the
stock, and the books check finds nothing to report.
"""
from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from stock_books import assert_settled
from test_consignment_in_sale import _books as _codes
from test_consignment_in_sale import _consign, _customer_return
from test_consignor_payable_per_consignor import _consignor, _owed
from test_invoice_unshipped_books import _lot, _ok
from test_money_stock_and_contact_invariants import _account_net

pytestmark = pytest.mark.asyncio

COGS = "5100"
OPENING = "1130-OB"


async def _invoice(client, auth, lines: list[tuple[str, str, float]], *, finalize: bool = True) -> str:
    """An invoice of ``lines`` (lot, sku, qty), finalized unless told otherwise."""
    items = [{"entity_id": lot, "sku": sku, "name": "Lot", "quantity": qty, "unit_price": 40.0,
              "line_total": 40.0 * qty} for lot, sku, qty in lines]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}", "line_items": items,
        "total": sum(i["line_total"] for i in items)})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    if finalize:
        await _ok(client, auth, f"/docs/{doc}/finalize")
    return doc


async def _ship(client, auth, doc: str, *lots: str) -> dict:
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": list(lots)})
    assert r.status_code == 200, r.text
    return r.json()


async def _lots(client, auth, *costs: float) -> tuple[str, list[str]]:
    sku = f"CFG-{uuid.uuid4().hex[:6]}"
    return sku, [await _lot(client, auth, sku, 1, cost) for cost in costs]


async def _expect(client, session, auth, *, shipped: float, set_aside: float, received: float) -> None:
    """Cost of goods sold is what shipped plus what is set aside, the inventory account
    carries the rest of what was received, and the books check finds nothing."""
    cid = auth["company_id"]
    assert (await _account_net(session, cid, COGS), await _account_net(session, cid, OPENING)) == (
        round(shipped + set_aside, 2), round(received - shipped - set_aside, 2))
    await assert_settled(client, session, auth)


async def _doc_number(session, auth, doc: str) -> str:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc})
    return row.state.get("doc_number") or row.state.get("ref_id")


async def _cost_moves(session, auth) -> list[dict]:
    """Every posted entry that moves cost of goods sold from one invoice to another."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.like("je:auto:%:cost-move:%")))).scalars().all()
    return [r.state for r in rows if (r.state or {}).get("status") == "posted"]


@pytest.mark.parametrize("cost_k,cost_l,cost_m", [(10.0, 20.0, 30.0), (10.0, 10.0, 10.0), (10.0, 30.0, 10.0)])
@pytest.mark.parametrize("then", ["ship_A", "never", "void_A", "revert_A"])
async def test_goods_one_invoice_set_aside_and_another_shipped_move_their_cost(client, session, auth, cost_k, cost_l, cost_m, then):
    """A (bound to K, 2 units) sets aside K and L; B (bound to L) sets aside the free M.
    B ships L: L's cost moves from A to B, B's M is released, A keeps only K."""
    sku, (K, L, M) = await _lots(client, auth, cost_k, cost_l, cost_m)
    received = cost_k + cost_l + cost_m
    a = await _invoice(client, auth, [(K, sku, 2)])
    b = await _invoice(client, auth, [(L, sku, 1)])
    await _expect(client, session, auth, shipped=0, set_aside=cost_k + cost_l + cost_m, received=received)

    shipped = await _ship(client, auth, b, L)
    assert shipped["cost_moved"] == [{"sku": sku, "lot_id": L, "doc_id": a,
                                      "doc_number": await _doc_number(session, auth, a)}]
    [move] = await _cost_moves(session, auth)
    assert sorted((e["account"], e["debit"], e["credit"]) for e in move["entries"]) == [
        (COGS, 0.0, cost_l), (COGS, cost_l, 0.0)]
    await _expect(client, session, auth, shipped=cost_l, set_aside=cost_k, received=received)

    if then == "ship_A":
        await _ship(client, auth, a, K)
        await _expect(client, session, auth, shipped=received, set_aside=0, received=received)
    elif then == "void_A":
        await _ok(client, auth, f"/docs/{a}/void")
        await _expect(client, session, auth, shipped=cost_l, set_aside=0, received=received)
        await _ok(client, auth, f"/docs/{a}/unvoid")
        await _expect(client, session, auth, shipped=cost_l, set_aside=cost_k, received=received)
        await _ok(client, auth, f"/docs/{a}/void")
        await _ok(client, auth, f"/docs/{a}/unvoid")
        await _expect(client, session, auth, shipped=cost_l, set_aside=cost_k, received=received)
        await _ship(client, auth, a, K)
        await _expect(client, session, auth, shipped=received, set_aside=0, received=received)
    elif then == "revert_A":
        await _ok(client, auth, f"/docs/{a}/revert-to-draft")
        await _expect(client, session, auth, shipped=cost_l, set_aside=0, received=received)
        await _ok(client, auth, f"/docs/{a}/finalize")
        await _expect(client, session, auth, shipped=cost_l, set_aside=cost_k + cost_m, received=received)
        await _ship(client, auth, a, K)
        await _expect(client, session, auth, shipped=received, set_aside=0, received=received)
    assert len(await _cost_moves(session, auth)) == 1


async def test_an_invoice_whose_goods_shipped_elsewhere_ships_its_other_line(client, session, auth):
    sku, (K, L, M) = await _lots(client, auth, 10.0, 20.0, 30.0)
    sku2, (X,) = await _lots(client, auth, 5.0)
    a = await _invoice(client, auth, [(K, sku, 2), (X, sku2, 1)])
    b = await _invoice(client, auth, [(L, sku, 1)])
    await _expect(client, session, auth, shipped=0, set_aside=65.0, received=65.0)
    await _ship(client, auth, b, L)
    await _expect(client, session, auth, shipped=20.0, set_aside=15.0, received=65.0)
    await _ship(client, auth, a, X)
    await _expect(client, session, auth, shipped=25.0, set_aside=10.0, received=65.0)
    await _ship(client, auth, a, K)
    await _expect(client, session, auth, shipped=65.0, set_aside=0, received=65.0)


async def test_an_invoice_left_short_is_costed_when_new_stock_ships(client, session, auth):
    """Only K and L exist; A sets both aside and B ships L: A cannot ship until stock
    arrives, and is costed at that stock's cost when it does."""
    sku, (K, L) = await _lots(client, auth, 10.0, 20.0)
    a = await _invoice(client, auth, [(K, sku, 2)])
    b = await _invoice(client, auth, [(L, sku, 1)])
    await _ship(client, auth, b, L)
    await _expect(client, session, auth, shipped=20.0, set_aside=10.0, received=30.0)
    r = await client.post(f"/docs/{a}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [K]})
    assert r.status_code == 409, r.text
    await _lot(client, auth, sku, 1, 40.0)
    await _expect(client, session, auth, shipped=20.0, set_aside=10.0, received=70.0)
    await _ship(client, auth, a, K)
    await _expect(client, session, auth, shipped=70.0, set_aside=0, received=70.0)


@pytest.mark.parametrize("cost_k,cost_l,cost_m", [(10.0, 20.0, 30.0), (10.0, 10.0, 10.0)])
async def test_restoring_a_voided_invoice_sets_aside_only_free_goods(client, session, auth, cost_k, cost_l, cost_m):
    """A sets aside K and L and is voided; B (bound to L) sets L aside. Restoring A sets
    aside K only: L is B's, and M was never A's."""
    sku, (K, L, M) = await _lots(client, auth, cost_k, cost_l, cost_m)
    received = cost_k + cost_l + cost_m
    a = await _invoice(client, auth, [(K, sku, 2)])
    await _ok(client, auth, f"/docs/{a}/void")
    b = await _invoice(client, auth, [(L, sku, 1)])
    await _expect(client, session, auth, shipped=0, set_aside=cost_l, received=received)
    await _ok(client, auth, f"/docs/{a}/unvoid")
    await _expect(client, session, auth, shipped=0, set_aside=cost_l + cost_k, received=received)
    await _ship(client, auth, b, L)
    await _expect(client, session, auth, shipped=cost_l, set_aside=cost_k, received=received)
    assert await _cost_moves(session, auth) == []
    await _ship(client, auth, a, K)
    await _expect(client, session, auth, shipped=received, set_aside=0, received=received)


async def test_restoring_a_voided_invoice_whose_goods_shipped_elsewhere(client, session, auth):
    """2 units at 10 costed on one invoice, voided, costed and shipped on a second; the
    first restored sets aside nothing."""
    sku = f"CFG-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 2, 20.0)
    one = await _invoice(client, auth, [(lot, sku, 2)])
    await _ok(client, auth, f"/docs/{one}/void")
    two = await _invoice(client, auth, [(lot, sku, 2)])
    await _ship(client, auth, two, lot)
    await _ok(client, auth, f"/docs/{one}/unvoid")
    await _expect(client, session, auth, shipped=20.0, set_aside=0, received=20.0)


async def test_refinalizing_after_a_revert_keeps_each_lot_set_aside_once(client, session, auth):
    sku, (K, L, M) = await _lots(client, auth, 10.0, 20.0, 30.0)
    a = await _invoice(client, auth, [(K, sku, 2)])
    b = await _invoice(client, auth, [(L, sku, 1)])
    await _ok(client, auth, f"/docs/{a}/revert-to-draft")
    await _expect(client, session, auth, shipped=0, set_aside=30.0, received=60.0)
    await _ok(client, auth, f"/docs/{a}/finalize")
    await _expect(client, session, auth, shipped=0, set_aside=60.0, received=60.0)
    await _ship(client, auth, b, L)
    await _expect(client, session, auth, shipped=20.0, set_aside=10.0, received=60.0)
    await _ship(client, auth, a, K)
    await _expect(client, session, auth, shipped=60.0, set_aside=0, received=60.0)


async def test_part_of_a_lot_set_aside_moves_only_what_shipped(client, session, auth):
    """Lot P holds 4 at 10. A sets aside 3; B (2 units) finds 1 free and sets it aside.
    B ships 2: one was B's, one moves from A. A ships once new stock arrives."""
    sku = f"CFG-{uuid.uuid4().hex[:6]}"
    unit = 10.0
    p = await _lot(client, auth, sku, 4, 4 * unit)
    a = await _invoice(client, auth, [(p, sku, 3)])
    b = await _invoice(client, auth, [(p, sku, 2)])
    await _expect(client, session, auth, shipped=0, set_aside=4 * unit, received=4 * unit)
    await _ship(client, auth, b, p)
    [move] = await _cost_moves(session, auth)
    assert sum(e["debit"] for e in move["entries"]) == 1 * unit
    await _expect(client, session, auth, shipped=2 * unit, set_aside=2 * unit, received=4 * unit)
    r = await client.post(f"/docs/{a}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [p]})
    assert r.status_code == 409, r.text
    q_cost = 15.0
    await _lot(client, auth, sku, 1, q_cost)
    received = 4 * unit + q_cost
    await _ship(client, auth, a, p)
    await _expect(client, session, auth, shipped=received, set_aside=0, received=received)


async def test_goods_two_invoices_ship_from_one_invoices_set_aside(client, session, auth):
    """A (3 units) sets aside K, L and M; B ships L and C ships M: each takes its lot's
    cost from A, and a customer return of L on B puts it back in stock."""
    sku, (K, L, M) = await _lots(client, auth, 10.0, 20.0, 30.0)
    await _invoice(client, auth, [(K, sku, 3)])
    b = await _invoice(client, auth, [(L, sku, 1)])
    c = await _invoice(client, auth, [(M, sku, 1)])
    await _expect(client, session, auth, shipped=0, set_aside=60.0, received=60.0)
    await _ship(client, auth, b, L)
    await _ship(client, auth, c, M)
    assert len(await _cost_moves(session, auth)) == 2
    await _expect(client, session, auth, shipped=50.0, set_aside=10.0, received=60.0)
    await _customer_return(client, session, auth, b, L, 1)
    await _expect(client, session, auth, shipped=30.0, set_aside=10.0, received=60.0)


async def test_voiding_the_invoice_that_took_the_goods_puts_them_back_in_stock(client, session, auth):
    """B ships L that A set aside, then takes it back and is voided. L goes back into stock
    at its cost, free for any invoice; A, already uncosted for it, is costed when it ships."""
    sku, (K, L, M) = await _lots(client, auth, 10.0, 20.0, 30.0)
    a = await _invoice(client, auth, [(K, sku, 2)])
    b = await _invoice(client, auth, [(L, sku, 1)])
    await _ship(client, auth, b, L)
    await _ok(client, auth, f"/docs/{b}/revert-lines", {"line_entity_ids": [L]})
    await _expect(client, session, auth, shipped=0, set_aside=10.0, received=60.0)
    await _ok(client, auth, f"/docs/{b}/void")
    await _expect(client, session, auth, shipped=0, set_aside=10.0, received=60.0)
    await _ok(client, auth, f"/docs/{b}/unvoid")
    await _expect(client, session, auth, shipped=0, set_aside=10.0, received=60.0)
    await _ok(client, auth, f"/docs/{b}/void")
    await _ship(client, auth, a, K)
    session.expire_all()
    shipped = 10.0
    for lot, cost in ((L, 20.0), (M, 30.0)):
        row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": lot})
        if row.state["status"] not in ("available", "reserved"):
            shipped += cost
    assert shipped in (30.0, 40.0)
    await _expect(client, session, auth, shipped=shipped, set_aside=0, received=60.0)


async def test_consigned_goods_set_aside_and_shipped_elsewhere_are_owed_once(client, session, auth):
    """2 consigned units at 4: A sets both aside (owed 8); B ships them. The consignor is
    owed 8, not 16, and converting the consignment settles it once."""
    consignor = await _consignor(client, auth, "Consignor")
    consignment, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, unit_price=5.0,
                                      contact_id=consignor)
    sku = (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": lot})).state["sku"]
    a = await _invoice(client, auth, [(lot, sku, 2)])
    assert await _owed(client, auth, consignor) == 8.0
    b = await _invoice(client, auth, [(lot, sku, 2)])
    assert await _codes(session, auth, "2115", COGS) == {"2115": -8.0, COGS: 8.0}
    shipped = await _ship(client, auth, b, lot)
    assert [m["doc_id"] for m in shipped["cost_moved"]] == [a]
    assert await _owed(client, auth, consignor) == 8.0
    assert await _codes(session, auth, "2115", COGS) == {"2115": -8.0, COGS: 8.0}
    await assert_settled(client, session, auth)
    await _ok(client, auth, f"/docs/{consignment}/convert")
    billed = 2 * 5.0
    assert await _codes(session, auth, "2115", COGS, "2110") == {"2115": 0.0, COGS: billed, "2110": -billed}
    await assert_settled(client, session, auth)


async def test_shipping_tells_the_user_whose_goods_it_took():
    """The shipping page shows a notice that stays until it is closed, naming the lot and
    the invoice that will be costed when it ships."""
    import json
    from unittest.mock import AsyncMock, patch

    from httpx import ASGITransport, AsyncClient

    from test_helpers import authed_cookies
    from ui.app import app as ui_app
    from ui.i18n import t

    moved = [{"sku": "LOT-1", "lot_id": "item:1", "doc_id": "doc:A", "doc_number": "INV-A"}]
    answer = {"fulfillment_status": "fulfilled", "fulfilled": ["item:1"], "cost_moved": moved}
    with patch("ui.api_client.fulfill_lines", new=AsyncMock(return_value=answer)):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
            r = await ui.post("/docs/doc:B/fulfill-lines", data={"selected": ["item:1"]},
                              cookies=authed_cookies())
    assert r.status_code == 204, r.text
    assert r.headers["HX-Redirect"] == "/docs/doc:B"
    toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]
    assert toast == {"message": t("documents.cost_moved_with_goods", sku="LOT-1", doc="INV-A"),
                     "type": "info", "persist": True}
    assert toast["message"] == "Lot LOT-1 was set aside for invoice INV-A. Invoice INV-A will be costed when it ships."


def test_the_notice_is_translated_in_every_language():
    import json
    from pathlib import Path

    locales = Path(__file__).resolve().parents[1] / "ui" / "locales"
    files = sorted(locales.glob("*.json"))
    assert len(files) == 12
    for path in files:
        text = json.loads(path.read_text())["documents.cost_moved_with_goods"]
        assert "{sku}" in text and "{doc}" in text, path.name


def test_a_notice_sent_with_a_redirect_is_shown_on_the_next_page():
    from ui.components.shell import _CLIENT_JS

    assert "HX-Redirect" in _CLIENT_JS and "sessionStorage" in _CLIENT_JS


@pytest.mark.timeout(900)
async def test_finalizing_stays_fast_with_many_open_invoices(client, session, auth):
    """Finalizing an invoice and running the books check read only what they need, so
    neither slows down as unshipped invoices pile up."""
    from celerp.services.lot_origin import stock_off_books

    sku = f"PERF-{uuid.uuid4().hex[:6]}"
    other = f"PERF-{uuid.uuid4().hex[:6]}"
    for _ in range(200):
        await _invoice(client, auth, [(await _lot(client, auth, other, 1, 10.0), other, 1)])
    lot = await _lot(client, auth, sku, 1, 10.0)
    doc = await _invoice(client, auth, [(lot, sku, 1)], finalize=False)
    started = time.perf_counter()
    await _ok(client, auth, f"/docs/{doc}/finalize")
    assert time.perf_counter() - started < 1.0
    session.expire_all()
    started = time.perf_counter()
    assert await stock_off_books(session, auth["company_id"]) == []
    assert time.perf_counter() - started < 2.0

