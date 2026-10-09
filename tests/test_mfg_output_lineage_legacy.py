# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Output an older release received, then split, sold in part, transformed, used in another
run, wrote off or sold by converting a memo, before this release kept such lots whole until their run completed.

Its cost has left the lot along a path no re-cost can follow, so reconciling the run keeps
what that lot was given and shares the rest of what was issued over the run's other output;
the run then completes like any other, with the books settled. A lot an older release left
whole is held to today's rule from the upgrade on. When what such lots carry is more than
the value stated for the run, reconciling is refused with a message saying what to do.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from celerp.events.engine import emit_event
from celerp.modules import slots
from mfg_runs import PURCHASED, complete, issue, product, receive, refusal, role, run, snapshot
from test_mfg_output_lineage import refused_unchanged
from stock_books import assert_settled
from test_cost_restatement import _state
from test_mfg_reconcile import reconcile
from test_mfg_reconcile_legacy import _older_stock
from test_mfg_wip_upgrade import _facts, _older_issue, _older_receive, _upgrade, in_production_slot  # noqa: F401
from test_posting_roles_older_stock import _older_release

pytestmark = pytest.mark.asyncio


@contextmanager
def older_release_rules():
    """What the older release allowed: no hold on a lot whose run is still open."""
    saved = slots._slots.pop("item_lineage_guard", None)
    try:
        yield
    finally:
        if saved is not None:
            slots._slots["item_lineage_guard"] = saved


async def _cost(session, auth, item: str) -> float:
    return float((await _state(session, auth, item))["cost_total"])


async def _split(client, auth, lot):
    return await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})


async def _fulfil_one(client, session, auth, lot):
    state = await _state(session, auth, lot)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}", "total": 500.0,
        "line_items": [{"sku": state["sku"], "name": "Lot", "quantity": 1, "unit_price": 500.0, "entity_id": lot}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    return await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})


async def _transform(client, auth, lot):
    return await client.post(f"/items/{lot}/transform", headers=auth["headers"], json={
        "child_sku": f"TX-{uuid.uuid4().hex[:6]}", "child_category": "Processed", "child_sell_by": "piece",
        "child_quantity": 2})


async def _write_off_one(client, auth, lot):
    wo = (await client.post("/lists/writeoff", headers=auth["headers"], json={"entity_ids": [lot]})).json()["id"]
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=auth["headers"],
                          json={"item_id": lot, "qty_out": 1, "account": "6950"})
    assert r.status_code == 200, r.text
    return await client.post(f"/lists/{wo}/write-off", headers=auth["headers"])


async def older_run(client, session, auth) -> tuple[str, str, str]:
    """An older run of 4 needing twenty of a 200.00 component: it took ten (100.00) and
    received 2, into one lot given the whole 100.00 provisionally."""
    await _older_release(session, auth)
    raw = await _older_stock(session, auth, 200.0, 20)
    order = await run(client, auth, await product(client, auth, [(raw, 5)]), 4)
    await _older_issue(session, auth, order, raw, 10)
    lot = await _older_receive(session, auth, order, 2)
    assert await _cost(session, auth, lot) == 100.0
    return raw, order, lot


async def _older_memo_convert(client, session, auth, lot):
    """Sent out whole on memo, then billed by converting the memo the way the older release
    did it: the lot marked sold to the memo, its sale on no invoice line, and the invoice
    finalized."""
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "memo", "line_items": [
        {"entity_id": lot, "sku": "OUT", "name": "Lot", "quantity": 2, "unit_price": 150.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    for path, body in ((f"/docs/{memo}/finalize", {}), (f"/docs/{memo}/fulfill-lines", {"line_entity_ids": [lot]}),
                       (f"/docs/{memo}/convert", {})):
        r = await client.post(path, headers=auth["headers"], json=body)
        assert r.status_code == 200, r.text
    invoice = r.json()["target_doc_id"]
    await emit_event(session, company_id=auth["company_id"], entity_id=lot, entity_type="item",
                     event_type="item.status.set", data={"new_status": "sold", "source_doc_id": memo},
                     actor_id=auth["user_id"], location_id=None, source="memo_convert",
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    r = await client.post(f"/docs/{invoice}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text


async def _older_consume(client, session, auth, lot):
    """Used as a component of another run, as the older release issued it."""
    await _older_issue(session, auth, await run(client, auth, await product(client, auth, [(lot, 1)]), 1), lot, 1)


async def _ok_(call) -> None:
    r = await call
    assert r.status_code == 200, r.text


# Each lineage, and whether it happens before the upgrade. Selling or writing off goes through
# this release's posting accounts, which the older company does not have yet: those happen
# after the upgrade, under the older release's rules, while the run still waits for reconciling.
_LINEAGES = {
    "split": (True, lambda c, s, a, lot: _ok_(_split(c, a, lot))),
    "fulfil": (False, lambda c, s, a, lot: _ok_(_fulfil_one(c, s, a, lot))),
    "transform": (True, lambda c, s, a, lot: _ok_(_transform(c, a, lot))),
    "consume": (True, _older_consume),
    "write_off": (False, lambda c, s, a, lot: _ok_(_write_off_one(c, a, lot))),
    "memo_convert": (False, _older_memo_convert),
}


@pytest.mark.parametrize("lineage", list(_LINEAGES))
async def test_legacy_lineage_reconciles_and_completes(client, session, auth, lineage):
    """The lot keeps its 100.00; the 100.00 issued since and the completion's waste are
    shared over the rest of the output, re-costing it, and the books settle."""
    raw, order, lot = await older_run(client, session, auth)
    before_upgrade, act = _LINEAGES[lineage]
    if before_upgrade:
        with older_release_rules():
            await act(client, session, auth, lot)
    await _upgrade(session)
    if not before_upgrade:
        with older_release_rules():
            await act(client, session, auth, lot)
    assert (await _facts(session, auth, order))["wip_unresolved"] == "received before tracking"

    r = await reconcile(client, auth, order, [(raw, 100.0)], await role(session, auth, PURCHASED))

    assert r.status_code == 200, r.text
    assert [(x["lot_item_id"], float(x["value"])) for x in r.json()["receipts"]] == [(lot, 100.0)]
    await assert_settled(client, session, auth)
    assert (await issue(client, auth, order, key="rest")).status_code == 200
    r = await receive(client, auth, order, 1, key="b")
    assert r.status_code == 200, r.text
    later = r.json()["lot_item_id"]
    assert await _cost(session, auth, later) == 50.0
    # Two of the twenty components wasted (20.00): 180.00 finished, 100.00 of it already in
    # the older lot's lineage, 80.00 shared by the two units received on this release.
    r = await complete(client, auth, order, key="done", waste_quantity=2, waste_reason="scrap")
    assert r.status_code == 200, r.text
    assert await _cost(session, auth, later) == 40.0
    assert (await _state(session, auth, order))["status"] == "completed"
    await assert_settled(client, session, auth)


async def test_legacy_open_output_is_guarded_after_upgrade(client, session, auth):
    """A lot the older release left whole is held to today's rule from the upgrade on: before
    and after the run is reconciled, until it completes."""
    raw, order, lot = await older_run(client, session, auth)
    await _upgrade(session)

    await refused_unchanged(session, auth, lambda: _split(client, auth, lot), order, lot)
    r = await reconcile(client, auth, order, [(raw, 100.0)], await role(session, auth, PURCHASED))
    assert r.status_code == 200, r.text
    assert await _cost(session, auth, lot) == 50.0
    await refused_unchanged(session, auth, lambda: _split(client, auth, lot), order, lot)

    assert (await issue(client, auth, order, key="rest")).status_code == 200
    r = await complete(client, auth, order, key="done", waste_quantity=2, waste_reason="scrap")
    assert r.status_code == 200, r.text
    assert await _cost(session, auth, lot) == 90.0
    await assert_settled(client, session, auth)
    assert (await _split(client, auth, lot)).status_code == 200


async def test_legacy_lots_carrying_more_than_issued_are_refused(client, session, auth):
    """The split lot carries 100.00 that cannot be taken back; a run stated at 60.00 would
    hold less than nothing. Refused, naming the floor; nothing changes."""
    raw, order, lot = await older_run(client, session, auth)
    with older_release_rules():
        assert (await _split(client, auth, lot)).status_code == 200
    await _upgrade(session)
    before = await snapshot(session, auth, raw, order, lot)

    detail = refusal(await reconcile(client, auth, order, [(raw, 60.0)], await role(session, auth, PURCHASED)),
                     422, "reconcile_kept")

    assert "100.00" in detail["message"], detail
    assert await snapshot(session, auth, raw, order, lot) == before
