# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shipping after an invoice is reissued.

An invoice is issued, some of its goods ship and come back, it is reverted to draft, a line
is removed and it is issued again. What it shipped before the revert belongs to the earlier
issue: the reissued invoice ships its remaining lines, costing exactly the goods it ships,
and nothing anywhere else in the company is blocked by the removed line. A shipment Celerp
really cannot place on a line is still refused, as a conflict that says what to do."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from test_cost_follows_goods import COGS, OPENING, _doc_number
from test_invoice_unshipped_books import _lot, _ok
from test_money_stock_and_contact_invariants import _account_net
from test_set_aside_goods_follow_the_lot import _per_doc

pytestmark = pytest.mark.asyncio

COSTS = (70.0, 28.0, 24.0, 31.0, 45.0, 52.0)


async def _stones(client, auth) -> list[tuple[str, str, float]]:
    """Six available stones, one lot each with its own SKU and cost."""
    out = []
    for cost in COSTS:
        sku = f"STN-{uuid.uuid4().hex[:6]}"
        out.append((await _lot(client, auth, sku, 1, cost), sku, cost))
    return out


async def _state(client, auth, doc: str) -> dict:
    r = await client.get(f"/docs/{doc}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _invoice(client, auth, stones) -> str:
    lines = [{"item_id": lot, "sku": sku, "description": sku, "quantity": 1, "unit_price": 100.0,
              "sell_by": "piece"} for lot, sku, _cost in stones]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": lines, "total": 100.0 * len(lines)})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    await _ok(client, auth, f"/docs/{doc}/finalize")
    return doc


async def _line_ids(client, auth, doc: str) -> list[str]:
    return [li["line_id"] for li in (await _state(client, auth, doc))["line_items"]]


async def _ship(client, auth, doc: str, line_ids: list[str]):
    return await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_ids": line_ids})


async def _ship_back_and_revert(client, auth, doc: str, n: int) -> None:
    """Ship the first ``n`` lines, set them available again and revert the invoice to draft."""
    ids = (await _line_ids(client, auth, doc))[:n]
    r = await _ship(client, auth, doc, ids)
    assert r.status_code == 200, r.text
    await _ok(client, auth, f"/docs/{doc}/set-available", {"line_ids": ids})
    await _ok(client, auth, f"/docs/{doc}/revert-to-draft")


async def _remove_first_line_and_reissue(client, auth, doc: str) -> None:
    st = await _state(client, auth, doc)
    r = await client.patch(f"/docs/{doc}", headers=auth["headers"], json={
        "fields_changed": {"line_items": {"old": st["line_items"], "new": st["line_items"][1:]}},
        "expected_version": st["version"]})
    assert r.status_code == 200, r.text
    await _ok(client, auth, f"/docs/{doc}/finalize")


async def _reissued(client, auth):
    """The customer's sequence up to the reissue: 6 stones issued, 3 shipped and set available,
    reverted, the first line (a stone that shipped and came back) removed, issued again."""
    stones = await _stones(client, auth)
    doc = await _invoice(client, auth, stones)
    await _ship_back_and_revert(client, auth, doc, 3)
    await _remove_first_line_and_reissue(client, auth, doc)
    return doc, stones


async def test_reissued_invoice_with_removed_line_ships_remaining_lines_with_correct_cogs(client, session, auth):
    doc, stones = await _reissued(client, auth)
    r = await _ship(client, auth, doc, await _line_ids(client, auth, doc))
    assert r.status_code == 200, r.text
    kept = sum(cost for _lot_id, _sku, cost in stones[1:])
    assert (await _per_doc(session, auth)).get(doc) == round(kept, 2)
    cid = auth["company_id"]
    assert await _account_net(session, cid, COGS) == round(kept, 2)
    assert await _account_net(session, cid, OPENING) == round(stones[0][2], 2)
    removed = (await client.get(f"/items/{stones[0][0]}", headers=auth["headers"])).json()
    assert removed["status"] == "available"


async def test_reissued_invoice_without_a_removed_line_ships_every_line(client, session, auth):
    """The control: the same sequence with no line removed ships all six at their full cost."""
    stones = await _stones(client, auth)
    doc = await _invoice(client, auth, stones)
    await _ship_back_and_revert(client, auth, doc, 3)
    await _ok(client, auth, f"/docs/{doc}/finalize")
    r = await _ship(client, auth, doc, await _line_ids(client, auth, doc))
    assert r.status_code == 200, r.text
    assert await _account_net(session, auth["company_id"], COGS) == round(sum(COSTS), 2)


async def test_finalizing_another_invoice_on_a_returned_lot_of_a_reissued_invoice(client, session, auth):
    doc, stones = await _reissued(client, auth)
    removed_lot, removed_sku, removed_cost = stones[0]
    other = await _invoice(client, auth, [stones[0]])
    r = await _ship(client, auth, other, await _line_ids(client, auth, other))
    assert r.status_code == 200, r.text
    assert (await _per_doc(session, auth)).get(other) == removed_cost


async def test_company_wide_readers_ignore_pre_revert_history(client, session, auth):
    from celerp.services import auto_je

    doc, stones = await _reissued(client, auth)
    cid = auth["company_id"]
    session.expire_all()
    claims = await auto_je.unshipped_claims(session, cid)
    assert {c.doc_id for c in claims} >= {doc}
    assert sum(c.qty for c in claims if c.doc_id == doc) == 5
    assert sum((await auto_je.recognized_unshipped(session, cid)).values()) == pytest.approx(sum(COSTS[1:]))
    row = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_id == doc))).scalar_one()
    assert await auto_je.held_short(session, cid, doc, row.state) == set()
    lots = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_id.in_([s[0] for s in stones])))).scalars().all()
    held = await auto_je.set_aside(session, cid, lots)
    assert stones[0][0] not in held and all(lot in held for lot, _sku, _cost in stones[1:])
    # Restoring any voided invoice checks the free goods of every open one.
    sku = f"STN-{uuid.uuid4().hex[:6]}"
    other = await _invoice(client, auth, [(await _lot(client, auth, sku, 1, 9.0), sku, 9.0)])
    await _ok(client, auth, f"/docs/{other}/void")
    await _ok(client, auth, f"/docs/{other}/unvoid")


async def test_void_and_unvoid_reissued_invoice(client, session, auth):
    doc, stones = await _reissued(client, auth)
    await _ok(client, auth, f"/docs/{doc}/void")
    assert (await _per_doc(session, auth)).get(doc) is None
    assert await _account_net(session, auth["company_id"], COGS) == 0
    await _ok(client, auth, f"/docs/{doc}/unvoid")
    r = await _ship(client, auth, doc, await _line_ids(client, auth, doc))
    assert r.status_code == 200, r.text
    assert (await _per_doc(session, auth)).get(doc) == round(sum(COSTS[1:]), 2)


async def test_kept_line_returned_before_revert_holds_full_share_after_reissue(client, session, auth):
    """A line shipped and set available before the revert is issued again in full: once the
    reissued invoice ships its other line, it still sets aside the whole cost of the first."""
    stones = (await _stones(client, auth))[:2]
    doc = await _invoice(client, auth, stones)
    await _ship_back_and_revert(client, auth, doc, 1)
    await _ok(client, auth, f"/docs/{doc}/finalize")
    r = await _ship(client, auth, doc, (await _line_ids(client, auth, doc))[1:])
    assert r.status_code == 200, r.text
    assert await _account_net(session, auth["company_id"], COGS) == round(COSTS[0] + COSTS[1], 2)
    assert (await _per_doc(session, auth)).get(doc) == round(COSTS[0] + COSTS[1], 2)


async def _rewrite_state(session, auth, doc: str, change) -> None:
    """Store on the invoice's record the fields ``change`` returns for its state, outside its
    event history."""
    session.expire_all()
    row = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id == doc))).scalar_one()
    row.state = {**row.state, **change(row.state)}
    await session.commit()


async def _rewrite_lines(session, auth, doc: str, rewrite) -> None:
    """Store the invoice's lines as ``rewrite`` returns them, as a record whose lines no longer
    say where its goods went (a line edited outside its event history)."""
    await _rewrite_state(session, auth, doc, lambda st: {"line_items": rewrite(st["line_items"])})


async def test_unmatched_out_lot_still_refused_with_new_message(client, session, auth):
    stones = (await _stones(client, auth))[:2]
    doc = await _invoice(client, auth, stones)
    first, second = await _line_ids(client, auth, doc)
    r = await _ship(client, auth, doc, [first])
    assert r.status_code == 200, r.text
    await _rewrite_lines(session, auth, doc, lambda lines: lines[1:])
    r = await _ship(client, auth, doc, [second])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    number = await _doc_number(session, auth, doc)
    assert detail["message_key"] == "error.cogs_line_unknown"
    assert detail["params"] == {"doc": number, "items": stones[0][1]}
    assert detail["message"] == (
        f"Celerp can't tell which line of invoice {number} the goods {stones[0][1]} were shipped on, "
        "so their cost can't be booked and nothing was changed. "
        "Report it on GitHub Issues with this message so it can be put right.")


async def test_ambiguous_back_lot_still_refused(client, session, auth):
    """A lot that came back in this issue and now fits two lines of its SKU, binding neither,
    is still refused: Celerp cannot tell which line's share it lowers."""
    stones = (await _stones(client, auth))[:2]
    doc = await _invoice(client, auth, stones)
    first, second = await _line_ids(client, auth, doc)
    r = await _ship(client, auth, doc, [first])
    assert r.status_code == 200, r.text
    await _ok(client, auth, f"/docs/{doc}/set-available", {"line_ids": [first]})
    sku = stones[0][1]

    def two_of_its_sku(lines):
        twin = {k: v for k, v in lines[0].items() if k not in ("item_id", "entity_id")}
        return [lines[1], {**twin, "line_id": str(uuid.uuid4())}, {**twin, "line_id": str(uuid.uuid4())}]

    await _rewrite_lines(session, auth, doc, two_of_its_sku)
    before = await _per_doc(session, auth)
    r = await _ship(client, auth, doc, [second])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "error.cogs_line_unknown"
    assert r.json()["detail"]["params"]["items"] == sku
    assert await _per_doc(session, auth) == before


async def _ship_in_two_batches(client, auth, stones):
    """An invoice of two stones whose second line ships first and whose first line ships in a
    second batch, so the invoice's list of shipped goods names only the first stone."""
    doc = await _invoice(client, auth, stones)
    first, second = await _line_ids(client, auth, doc)
    for line in (second, first):
        r = await _ship(client, auth, doc, [line])
        assert r.status_code == 200, r.text
    return doc, first, second


async def test_line_of_a_lot_still_out_cannot_be_dropped(client, session, auth):
    """An issued invoice keeps the line of goods still out on it: removing that line is refused."""
    stones = (await _stones(client, auth))[:2]
    doc, _first, _second = await _ship_in_two_batches(client, auth, stones)
    st = await _state(client, auth, doc)
    r = await client.patch(f"/docs/{doc}", headers=auth["headers"], json={
        "fields_changed": {"line_items": {"old": st["line_items"], "new": st["line_items"][:1]}},
        "expected_version": st["version"]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "lines.shipped_elsewhere"
    assert len((await _state(client, auth, doc))["line_items"]) == 2


async def test_invoice_with_a_lot_still_out_cannot_be_reverted(client, session, auth):
    """Setting the later batch available leaves the invoice partly shipped while the earlier
    batch's goods are still out, so it cannot go back to draft."""
    stones = (await _stones(client, auth))[:2]
    doc, first, _second = await _ship_in_two_batches(client, auth, stones)
    r = await client.post(f"/docs/{doc}/set-available", headers=auth["headers"], json={"line_ids": [first]})
    assert r.status_code == 200, r.text
    assert r.json()["fulfillment_status"] == "partial"
    r = await client.post(f"/docs/{doc}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert (await _state(client, auth, doc))["status"] != "draft"
    assert (await client.get(f"/items/{stones[1][0]}", headers=auth["headers"])).json()["status"] == "sold"


async def test_lot_still_out_across_a_revert_is_still_refused(client, session, auth):
    """Goods still out when an invoice went back to draft (only a guard letting the revert
    through can do that) stay on the books of the invoice: once their line is gone, the next
    shipment is refused and names them, and nothing is booked."""
    stones = (await _stones(client, auth))[:2]
    doc, first, _second = await _ship_in_two_batches(client, auth, stones)
    await _ok(client, auth, f"/docs/{doc}/set-available", {"line_ids": [first]})
    # the invoice as a Set available that ignored the goods still out would leave it
    await _rewrite_state(session, auth, doc, lambda st: {"fulfilled_items": [], "fulfillment_status": "unfulfilled"})
    await _ok(client, auth, f"/docs/{doc}/revert-to-draft")
    await _rewrite_lines(session, auth, doc, lambda lines: lines[:1])
    await _ok(client, auth, f"/docs/{doc}/finalize")
    before = (await _per_doc(session, auth), await _account_net(session, auth["company_id"], COGS))
    r = await _ship(client, auth, doc, await _line_ids(client, auth, doc))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "error.cogs_line_unknown"
    assert detail["params"] == {"doc": await _doc_number(session, auth, doc), "items": stones[1][1]}
    assert (await _per_doc(session, auth), await _account_net(session, auth["company_id"], COGS)) == before
    assert (await client.get(f"/items/{stones[1][0]}", headers=auth["headers"])).json()["status"] == "sold"
    assert (await client.get(f"/items/{stones[0][0]}", headers=auth["headers"])).json()["status"] == "available"
