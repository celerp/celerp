# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Runs an older release left behind, after this release's first start on them (``pre366``).

What those releases stored is taken as they stored it: a component listed on two lines is one
component needing both amounts; a line at zero or below is kept as written and holds the run
back from going ahead (Issue, Receive, Complete) while it can still be unwound (Return, Undo
Receipt, Cancel); and every component a settled run holds carries the value its own history
gives it, so a Return gives back exactly what left the shelf.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import pre366
from celerp.models.projections import Projection
from celerp.services.lot_origin import held_value
from stock_books import assert_books_carry_stock, assert_wip_carried

pytestmark = pytest.mark.asyncio

_FORWARD = (("issue", {}), ("receive", {}), ("complete", {}))


async def _row(session, old, entity_id: str) -> Projection:
    session.expire_all()
    return await session.get(Projection, {"company_id": old["company_id"], "entity_id": entity_id})


async def _state(session, old, run: str) -> dict:
    return (await _row(session, old, old["runs"][run])).state


async def _stock(session, old, item: str) -> tuple[float, Decimal]:
    row = await _row(session, old, old["items"][item])
    return float(row.state.get("quantity") or 0), held_value(row)


async def _post(client, old, run: str, action: str, body: dict | None = None):
    return await client.post(f"/manufacturing/{old['runs'][run]}/{action}", json=body or {}, headers=old["headers"])


async def _books(session, old) -> None:
    await assert_books_carry_stock(session, old["company_id"])
    await assert_wip_carried(session, old["company_id"])


async def test_a_component_listed_twice_is_one_component_issued_once(client, session):
    old = await pre366.upgraded(session)
    A = old["items"]["A"]
    assert [(i["item_id"], i["quantity"]) for i in (await _state(session, old, "dup"))["inputs"]] == [
        (A, 3.0), (old["items"]["B"], 1.0)]
    before = await _stock(session, old, "A")

    r = await _post(client, old, "dup", "issue")
    assert r.status_code == 200, r.text
    assert (await _stock(session, old, "A"))[0] == before[0] - 3
    again = await _post(client, old, "dup", "issue")
    assert again.status_code == 200 and again.json()["issued"] == [], again.text
    assert (await _stock(session, old, "A"))[0] == before[0] - 3
    await _books(session, old)


async def test_a_run_issued_on_two_lines_has_nothing_left_to_issue(client, session):
    old = await pre366.upgraded(session)
    state = await _state(session, old, "dup_issued")
    assert [(i["item_id"], i["issued_qty"]) for i in state["inputs"]] == [
        (old["items"]["A"], 3.0), (old["items"]["B"], 1.0)]
    before = await _stock(session, old, "A")

    r = await _post(client, old, "dup_issued", "issue")
    assert r.status_code == 200 and r.json()["issued"] == [], r.text
    assert await _stock(session, old, "A") == before
    # What was issued comes back once, at what it took.
    r = await _post(client, old, "dup_issued", "return")
    assert r.status_code == 200, r.text
    assert await _stock(session, old, "A") == (before[0] + 3, before[1] + Decimal("15"))
    await _books(session, old)


@pytest.mark.parametrize("run, line", [("zero", 0), ("negative", -1)])
async def test_a_run_with_a_component_at_zero_or_below_cannot_go_ahead(client, session, run, line):
    old = await pre366.upgraded(session)
    A = old["items"]["A"]
    state = await _state(session, old, run)
    assert [i["quantity"] for i in state["inputs"] if i["item_id"] == A] == [line]  # kept as written
    stock = {k: await _stock(session, old, k) for k in ("A", "B", "FG")}

    for action, body in _FORWARD:
        r = await _post(client, old, run, action, body)
        assert r.status_code == 409, (action, r.text)
        detail = r.json()["detail"]
        assert detail["message_key"] == "mfg.run_shape", detail
        assert "Return" in detail["message"] and "cancel" in detail["message"], detail
    assert {k: await _stock(session, old, k) for k in stock} == stock
    assert await _state(session, old, run) == state


@pytest.mark.parametrize("run", ["zero", "negative"])
async def test_a_run_with_a_component_at_zero_or_below_can_be_unwound(client, session, run):
    old = await pre366.upgraded(session)
    b_before = await _stock(session, old, "B")
    issued_b = sum(i["issued_qty"] for i in (await _state(session, old, run))["inputs"] if i["item_id"] == old["items"]["B"])

    r = await _post(client, old, run, "undo-receipt", {"lot_item_id": "item:none"})
    assert r.json()["detail"]["message_key"] == "mfg.not_a_receipt", r.text  # reached, not held back
    r = await _post(client, old, run, "return")
    assert r.status_code == 200, r.text
    assert await _stock(session, old, "B") == (b_before[0] + issued_b, b_before[1] + Decimal("3") * int(issued_b))
    r = await _post(client, old, run, "cancel", {"reason": "listed wrong"})
    assert r.status_code == 200, r.text
    assert (await _state(session, old, run))["status"] == "cancelled"
    await _books(session, old)


async def test_a_settled_run_returns_each_component_at_the_value_it_left_with(client, session):
    old = await pre366.upgraded(session)
    state = await _state(session, old, "settle")
    assert {i["item_id"]: Decimal(i["issued_value"]) for i in state["inputs"]} == {
        old["items"]["A"]: Decimal("10"), old["items"]["B"]: Decimal("3")}
    a, b = await _stock(session, old, "A"), await _stock(session, old, "B")

    r = await _post(client, old, "settle", "return")
    assert r.status_code == 200, r.text
    assert await _stock(session, old, "A") == (a[0] + 2, a[1] + Decimal("10"))
    assert await _stock(session, old, "B") == (b[0] + 1, b[1] + Decimal("3"))
    after = await _state(session, old, "settle")
    assert Decimal(after["wip_issued"]) == 0, after
    r = await _post(client, old, "settle", "cancel", {"reason": "not needed"})
    assert r.status_code == 200, r.text
    await _books(session, old)
