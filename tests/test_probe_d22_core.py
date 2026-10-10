# Supplier returns on restated bills keep the money and the stock in step. In every test: at every step 2110 == -outstanding (books
# currency), AP aging == outstanding, books carry stock (assert_settled), and stock gain +
# stock shrinkage nets to zero once the restated goods are all gone; no rtn-value entry is
# posted twice.
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _set_cost, _state
from test_lot_value_boundary import GAIN, SHRINKAGE, _role
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _receive, _return
from test_receive_selected_lines import _stamp_line_ids
from test_supplier_return_settles_bill import _aged, _restated_bill
from celerp.models.projections import Projection
from sqlalchemy import select


async def _post(client, auth, doc, action, body=None):
    return await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json=body or {})


async def _pl(session, auth) -> float:
    cid = auth["company_id"]
    return round(await _account_net(session, cid, await _role(session, auth, GAIN))
                 + await _account_net(session, cid, await _role(session, auth, SHRINKAGE)), 2)


async def _value_jes(session, auth, doc) -> dict[str, int]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    out: dict[str, int] = {}
    for r in rows:
        if r.entity_id.startswith(f"je:auto:{doc}:") and r.state.get("status") == "posted":
            fam = r.entity_id[len(f"je:auto:{doc}:"):].split(":")[0]
            out[fam] = out.get(fam, 0) + 1
    return out


async def _inv(client, session, auth, doc, rate: float = 1.0, step: str = "", settled: bool = True) -> dict:
    st = await _state(session, auth, doc)
    out = float(st.get("amount_outstanding") or 0) if st.get("status") != "draft" else 0.0
    ap = await _account_net(session, auth["company_id"], "2110")
    assert round(ap, 2) == round(-out * rate, 2), (step, "2110", ap, "outstanding", out, st.get("status"))
    assert round(await _aged(client, auth), 2) == round(out, 2), (step, "aging")
    if settled:
        await assert_settled(client, session, auth)
    return st


async def test_restated_partial_returns_then_void_unvoid_void(client, session, auth):
    doc, line_id, parcel = await _restated_bill(client, session, auth, from_order=False, cost=300.0)
    await _inv(client, session, auth, doc, step="restated")
    assert (await _return(client, auth, doc, parcel, 4)).status_code == 200
    await _inv(client, session, auth, doc, step="ret4")
    assert (await _return(client, auth, doc, parcel, 3)).status_code == 200
    await _inv(client, session, auth, doc, step="ret3")
    assert (await _return(client, auth, doc, parcel, 3)).status_code == 200
    st = await _inv(client, session, auth, doc, step="ret3b")
    assert st["status"] == "returned" and st["amount_outstanding"] == 0.0
    assert await _pl(session, auth) == 0.0
    before = await _value_jes(session, auth, doc)
    for action, status in (("void", "void"), ("unvoid", "returned"), ("void", "void"),
                           ("unvoid", "returned"), ("void", "void")):
        r = await _post(client, auth, doc, action)
        assert r.status_code == 200, (action, r.text)
        st = await _inv(client, session, auth, doc, step=action)
        assert st["status"] == status
        assert await _pl(session, auth) == 0.0, action
        assert (await _value_jes(session, auth, doc)).get("rtn-value") == before.get("rtn-value"), action


async def _multi_line_bill(client, session, auth, prices, qtys, from_order=False, currency=None, rate=None):
    extra = {}
    if currency:
        extra = {"currency": currency, "conversion_rate": rate}
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", [
        {"sku": f"ML{i}-{uuid.uuid4().hex[:6]}", "name": f"Part {i}", "quantity": q, "unit_price": p}
        for i, (p, q) in enumerate(zip(prices, qtys))], **extra)
    if not from_order:
        await _finalize(client, auth, doc)
    ids = await _stamp_line_ids(session, auth, doc)
    lines = (await _state(session, auth, doc))["line_items"]
    r = await _receive(client, auth, doc, *[
        {"po_line_index": i, "sku": li["sku"], "name": li["name"], "quantity_received": li["quantity"]}
        for i, li in enumerate(lines)])
    assert r.status_code == 200, r.text
    if from_order:
        await _finalize(client, auth, doc)
    parcels = (await _state(session, auth, doc))["received_item_ids"]
    return doc, ids, parcels


@pytest.mark.parametrize("from_order", [False, True], ids=["bill", "order"])
@pytest.mark.parametrize("by", ["lot", "line"])
async def test_multi_line_different_prices_one_restated(client, session, auth, from_order, by):
    doc, ids, parcels = await _multi_line_bill(client, session, auth, [3.33, 7.77, 11.11], [3, 7, 9],
                                               from_order=from_order)
    await _inv(client, session, auth, doc, step="recv")
    assert (await _set_cost(client, auth, parcels[1], 100.0)).status_code == 200
    assert (await _set_cost(client, auth, parcels[2], 1.0)).status_code == 200
    plan = [(0, 1), (1, 2), (2, 4), (1, 5), (0, 2), (2, 5)]
    for idx, q in plan:
        if by == "line":
            r = await client.post(f"/docs/{doc}/return-items", headers=auth["headers"],
                                  json={"lines": [{"line_id": ids[idx], "quantity_returned": q}]})
        else:
            r = await _return(client, auth, doc, parcels[idx], q)
        assert r.status_code == 200, r.text
        await _inv(client, session, auth, doc, step=f"ret {idx} {q}")
    st = await _state(session, auth, doc)
    assert st["status"] == "returned" and st["amount_outstanding"] == 0.0
    assert await _account_net(session, auth["company_id"], "2110") == 0.0
    assert await _pl(session, auth) == 0.0
    if not from_order:
        for action in ("void", "unvoid", "void"):
            assert (await _post(client, auth, doc, action)).status_code == 200, action
            await _inv(client, session, auth, doc, step=action)
            assert await _pl(session, auth) == 0.0


@pytest.mark.parametrize("from_order", [False, True], ids=["bill", "order"])
async def test_revert_refinalize_then_receive_return_pay(client, session, auth, from_order):
    doc, line_id, parcel = await _restated_bill(client, session, auth, from_order=from_order, cost=300.0)
    assert (await _return(client, auth, doc, parcel, 10)).status_code == 200
    await _inv(client, session, auth, doc, step="returned")
    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 200, r.text
    st = await _inv(client, session, auth, doc, step="reverted")
    assert await _pl(session, auth) == 0.0
    # Back to a bill again
    await _finalize(client, auth, doc)
    # a bill finalized with nothing received holds goods in transit; settled check skipped here
    # receive again (bill path) and return part, then pay the rest
    st = await _state(session, auth, doc)
    if from_order:
        # the order kept its receipts and its returns; the reconverted bill owes nothing for goods gone
        assert st["amount_outstanding"] == 0.0, st
        return
    assert st["amount_outstanding"] == 120.0
    sku = st["line_items"][0]["sku"]
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 10})
    assert r.status_code == 200, r.text
    await _inv(client, session, auth, doc, step="re-received")
    new = [p for p in (await _state(session, auth, doc))["received_item_ids"]]
    assert len(new) == 1 and new[0] != parcel, new
    assert (await _return(client, auth, doc, new[0], 4)).status_code == 200
    await _inv(client, session, auth, doc, step="re-returned")
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"],
                          json={"amount": 72.0, "payment_date": "2026-03-02", "bank_account": "1111"})
    assert r.status_code == 200, r.text
    st = await _inv(client, session, auth, doc, step="paid")
    assert st["status"] == "paid"


async def test_return_replay_same_key_posts_once(client, session, auth):
    doc, line_id, parcel = await _restated_bill(client, session, auth, from_order=False, cost=300.0)
    for _ in range(3):
        r = await _return(client, auth, doc, parcel, 4, key="k-replay-1")
        assert r.status_code == 200, r.text
    st = await _inv(client, session, auth, doc, step="replayed")
    assert st["amount_outstanding"] == 72.0
    assert (await _value_jes(session, auth, doc)).get("rtn-value") == 1
    assert (await _value_jes(session, auth, doc)).get("rtn") == 1


async def test_void_replay_same_key(client, session, auth):
    doc, line_id, parcel = await _restated_bill(client, session, auth, from_order=False, cost=300.0)
    assert (await _return(client, auth, doc, parcel, 10)).status_code == 200
    for _ in range(2):
        r = await _post(client, auth, doc, "void", {"idempotency_key": "v-1"})
        assert r.status_code == 200, r.text
    await _inv(client, session, auth, doc, step="void-replay")
    for _ in range(2):
        r = await _post(client, auth, doc, "unvoid", {"idempotency_key": "u-1"})
        assert r.status_code == 200, r.text
    await _inv(client, session, auth, doc, step="unvoid-replay")


@pytest.mark.parametrize("rate", [1.37, 0.0333, 36.123457])
async def test_fx_odd_rate_odd_quantities_clear(client, session, auth, rate):
    doc, ids, parcels = await _multi_line_bill(client, session, auth, [3.33, 7.77, 0.07], [3, 7, 9],
                                               currency="EUR", rate=rate)
    assert (await _set_cost(client, auth, parcels[0], 99.99)).status_code == 200
    for idx, q in [(0, 1), (1, 3), (2, 4), (0, 1), (1, 4), (2, 5), (0, 1)]:
        r = await _return(client, auth, doc, parcels[idx], q)
        assert r.status_code == 200, r.text
    st = await _state(session, auth, doc)
    assert st["status"] == "returned"
    assert st["amount_outstanding"] == 0.0
    assert await _account_net(session, auth["company_id"], "2110") == 0.0
    assert await _pl(session, auth) == 0.0
    await assert_settled(client, session, auth)


async def test_odd_quantity_thirds(client, session, auth):
    doc = await _doc(client, auth, "bill", [
        {"sku": f"TH-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 3, "unit_price": 0.01}])
    await _finalize(client, auth, doc)
    sku = (await _state(session, auth, doc))["line_items"][0]["sku"]
    assert (await _receive(client, auth, doc, {"po_line_index": 0, "sku": sku, "name": "Beads",
                                               "quantity_received": 3})).status_code == 200
    [parcel] = (await _state(session, auth, doc))["received_item_ids"]
    assert (await _set_cost(client, auth, parcel, 0.05)).status_code == 200
    for _ in range(3):
        assert (await _return(client, auth, doc, parcel, 1)).status_code == 200
        await _inv(client, session, auth, doc, step="third")
    assert await _account_net(session, auth["company_id"], "2110") == 0.0
    assert await _pl(session, auth) == 0.0


async def test_baseline_fresh_finalized_bill_settles(client, session, auth):
    """Control: a bill finalized before its goods come in books them on its own entry, so
    the purchased inventory account holds them with no lot yet; once they are received the
    books carry the stock."""
    sku = f"FR-{uuid.uuid4().hex[:6]}"
    doc = await _doc(client, auth, "bill", [{"sku": sku, "name": "Beads", "quantity": 10, "unit_price": 12.0}])
    await _finalize(client, auth, doc)
    await _inv(client, session, auth, doc, step="fresh", settled=False)
    assert await _account_net(session, auth["company_id"], "1130-P") == 120.0
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 10})
    assert r.status_code == 200, r.text
    await _inv(client, session, auth, doc, step="received")
