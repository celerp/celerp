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


# Runs an older release made without naming a product (company ``generic``): nothing tells
# which item their output is, so they go ahead only once the user names it, and a receipt
# that made no lot is discarded, never turned into stock.

async def _items(session, old) -> dict[str, dict]:
    session.expire_all()
    from sqlalchemy import select

    rows = (await session.execute(select(Projection).where(
        Projection.company_id == old["company_id"], Projection.entity_type == "item"))).scalars()
    return {r.entity_id: dict(r.state) for r in rows}


async def _ledger(session, old) -> int:
    from sqlalchemy import func, select

    from celerp.models.ledger import LedgerEntry

    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == old["company_id"]))).scalar_one()


async def _new_item(client, old, sku: str, **extra) -> str:
    r = await client.post("/items", headers=old["headers"], json={
        "sku": sku, "name": sku, "quantity": 0, "sell_by": "piece", "status": "available", "cost_total": 0.0,
        **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_a_run_naming_no_product_cannot_go_ahead(client, session):
    old = await pre366.upgraded(session, "generic")
    for run in ("generic_open", "generic_received"):
        state, count = await _state(session, old, run), await _ledger(session, old)
        for action, body in _FORWARD:
            r = await _post(client, old, run, action, body)
            assert r.status_code == 409 and r.json()["detail"]["message_key"] == "mfg.no_output", (run, action, r.text)
        assert await _state(session, old, run) == state and await _ledger(session, old) == count


async def test_a_receipt_that_made_no_lot_is_discarded_and_the_run_unwinds(client, session):
    old = await pre366.upgraded(session, "generic")
    assert (await _state(session, old, "generic_received"))["wip_unresolved"] == "received before tracking"
    r = await _post(client, old, "generic_received", "return")
    assert r.json()["detail"]["message_key"] == "mfg.reconciliation_required", r.text
    items = await _items(session, old)
    c = await _stock(session, old, "C")

    r = await _post(client, old, "generic_received", "repair-output", {})
    assert r.status_code == 200 and r.json()["discarded"] == 1.0, r.text
    state = await _state(session, old, "generic_received")
    assert state["received_qty"] == 0 and not state.get("received_lots"), state
    assert Decimal(state["wip_issued"]) == Decimal("4") and not state.get("wip_untracked") \
        and not state.get("wip_unresolved"), state
    assert {i["item_id"]: Decimal(i["issued_value"]) for i in state["inputs"]} == {old["items"]["C"]: Decimal("4")}
    assert await _items(session, old) == items  # no lot made from what the older release recorded
    await _books(session, old)

    r = await _post(client, old, "generic_received", "return")
    assert r.status_code == 200, r.text
    assert await _stock(session, old, "C") == (c[0] + 2, c[1] + Decimal("4"))
    r = await _post(client, old, "generic_received", "cancel", {"reason": "made nothing"})
    assert r.status_code == 200, r.text
    assert Decimal((await _state(session, old, "generic_received"))["wip_issued"]) == 0
    await _books(session, old)


async def test_a_run_held_back_by_another_runs_books_settles_once_that_run_is_repaired(client, session):
    old = await pre366.upgraded(session, "generic")
    # The other run's value is still on the inventory account, so the books cannot say which is which.
    assert (await _state(session, old, "generic_open"))["wip_unresolved"] == "books disagree"

    r = await _post(client, old, "generic_received", "repair-output", {})
    assert r.status_code == 200, r.text

    state = await _state(session, old, "generic_open")
    assert Decimal(state["wip_issued"]) == Decimal("4") and not state.get("wip_untracked") \
        and not state.get("wip_unresolved"), state
    await _books(session, old)


async def test_a_run_still_held_back_is_not_flagged_again_on_the_next_start(client, session):
    from sqlalchemy import func, select

    from celerp.models.notification import Notification

    async def told() -> int:
        return await session.scalar(select(func.count()).select_from(Notification).where(
            Notification.company_id == old["company_id"]))

    old = await pre366.upgraded(session, "generic")
    before = await _ledger(session, old), await told()

    await pre366.start()

    assert (await _ledger(session, old), await told()) == before
    assert (await _state(session, old, "generic_open"))["wip_unresolved"] == "books disagree"


async def test_a_run_given_its_product_goes_on_to_make_it(client, session):
    old = await pre366.upgraded(session, "generic")
    product = await _new_item(client, old, "GEN-1")

    r = await _post(client, old, "generic_received", "repair-output", {"output_item_id": product})
    assert r.status_code == 200, r.text
    state = await _state(session, old, "generic_received")
    assert state["output_item_id"] == product and state["expected_outputs"][0]["quantity"] == 2.0, state

    r = await _post(client, old, "generic_received", "receive")
    assert r.status_code == 200, r.text
    lot = (await _row(session, old, r.json()["lot_item_id"])).state
    assert (lot["parent_item_id"], lot["quantity"], Decimal(str(lot["cost_total"]))) == (product, 2.0, Decimal("4"))
    assert (await _state(session, old, "generic_received"))["status"] == "completed"
    await _books(session, old)


async def test_a_product_that_cannot_be_made_is_refused_with_nothing_changed(client, session):
    old = await pre366.upgraded(session, "generic")
    elsewhere = await pre366.load(session, "main")
    service = await _new_item(client, old, "SVC", inventory_type="service")
    state, count = await _state(session, old, "generic_received"), await _ledger(session, old)

    for output, key in ((elsewhere["items"]["FG"], "mfg.no_product"), (service, "mfg.not_stock"),
                        ("item:missing", "mfg.no_product")):
        r = await _post(client, old, "generic_received", "repair-output", {"output_item_id": output})
        assert r.status_code == 422 and r.json()["detail"]["message_key"] == key, (output, r.text)
        assert await _state(session, old, "generic_received") == state and await _ledger(session, old) == count


async def test_repairing_needs_manufacturing_permission(client, session):
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User
    from test_helpers import make_authed_token

    import uuid

    old = await pre366.upgraded(session, "generic")
    uid = uuid.uuid4()
    session.add(User(id=uid, email=f"v-{uid.hex[:8]}@test.co", name="Viewer", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=old["company_id"], role="viewer", is_active=True))
    await session.commit()
    viewer = {"Authorization": f"Bearer {await make_authed_token(session, str(uid), str(old['company_id']), 'viewer')}"}
    state, count = await _state(session, old, "generic_received"), await _ledger(session, old)

    r = await client.post(f"/manufacturing/{old['runs']['generic_received']}/repair-output", json={}, headers=viewer)
    assert r.status_code == 403, r.text
    assert await _state(session, old, "generic_received") == state and await _ledger(session, old) == count
