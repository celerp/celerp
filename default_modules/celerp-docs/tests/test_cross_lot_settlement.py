# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Cross-lot memo settlement through revert and convert.

A memo line whose quantity exceeds its bound lot draws cross-lot siblings from
other lots of the same SKU. Fulfill stamps each drawn lot status_doc_id==this
memo (so close correctly counts it) but the sibling never appears in the memo's
line_items. The memo's own settlement workflows must be able to resolve that
sibling so the memo can be closed:

  * revert-lines accepts a sibling (an allocation-set member, not a line_items
    row) and returns it to stock; and
  * convert bills each memo line for the full still-out quantity of its SKU
    (bound lot plus every cross-lot sibling) at the line's unit price, and
    settles all backing lots.

These run over the HTTP client (real per-request commits within the test's
transaction), the same surface the cross-lot close/label tests already use.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@xlot.test"
    r = await client.post("/auth/register", json={"company_name": "XLot Co", "email": addr,
                                                   "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _lot(client, h, sku, qty=1) -> str:
    """An available, splittable lot for a SKU (several lots may share a SKU)."""
    r = await client.post("/items", headers=h, json={
        "status": "available", "sku": sku, "name": sku, "quantity": qty,
        "sell_by": "piece", "allow_splitting": True})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _cross_lot_memo(client, h, sku, unit_price=10):
    """A memo with one line bound to lot A, quantity 2, fulfilled across A + sibling B.

    Returns (memo_id, lot_a, lot_b). After fulfill both lots are memo_out; A is the
    line_items row, B the cross-lot sibling (stamped to this memo, absent from
    line_items)."""
    lot_a = await _lot(client, h, sku, qty=1)
    lot_b = await _lot(client, h, sku, qty=1)
    line_items = [{"entity_id": lot_a, "sku": sku, "name": sku, "quantity": 2,
                   "unit_price": unit_price, "sell_by": "piece"}]
    memo = (await client.post("/docs", headers=h,
                              json={"doc_type": "memo", "line_items": line_items})).json()["id"]
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h,
                              json={"line_entity_ids": [lot_a]})).status_code == 200
    assert (await client.get(f"/items/{lot_a}", headers=h)).json().get("status") == "memo_out"
    assert (await client.get(f"/items/{lot_b}", headers=h)).json().get("status") == "memo_out"
    return memo, lot_a, lot_b


@pytest.mark.asyncio
async def test_cross_lot_revert_sibling_then_close(client):
    """Fulfill across lots (A bound, B sibling), revert BOTH the bound line and the
    sibling, the allocation set empties, and Close then succeeds.

    At merge-base revert rejects the sibling B with a 422 (not in line_items), so B can
    never be settled through the memo and Close stays blocked forever. Post-fix the
    allocation-aware revert guard accepts B; reverting A and B empties the allocation set
    and Close returns 200."""
    token = await _register(client)
    h = _h(token)
    memo, lot_a, lot_b = await _cross_lot_memo(client, h, "XR-SPAN")

    # Reverting a foreign id (neither a line nor an allocation member) is still rejected.
    stranger = await _lot(client, h, "XR-OTHER")
    r_foreign = await client.post(f"/docs/{memo}/revert-lines", headers=h,
                                  json={"line_entity_ids": [stranger]})
    assert r_foreign.status_code == 422, r_foreign.text

    # Revert the bound lot A and the cross-lot sibling B together. B is an allocation-set
    # member, not a line_items row: the allocation-aware guard must accept it.
    r = await client.post(f"/docs/{memo}/revert-lines", headers=h,
                          json={"line_entity_ids": [lot_a, lot_b]})
    assert r.status_code == 200, f"revert of a cross-lot sibling must succeed; got {r.status_code}: {r.text}"

    # Both lots are back in stock; nothing is still out at the customer.
    assert (await client.get(f"/items/{lot_a}", headers=h)).json().get("status") != "memo_out"
    assert (await client.get(f"/items/{lot_b}", headers=h)).json().get("status") != "memo_out"

    # Close now succeeds: the allocation set is empty.
    rc = await client.post(f"/docs/{memo}/close", headers=h, json={})
    assert rc.status_code == 200, f"close must succeed once the sibling is settled; got {rc.text}"
    assert (await client.get(f"/docs/{memo}", headers=h)).json().get("status") == "closed"


@pytest.mark.asyncio
async def test_revert_sibling_leaves_doc_status_coherent(client):
    """Reverting a sibling that is not a line_items row leaves the doc's line_items-derived
    fulfillment_status correct (the recompute runs over line_items only).

    At merge-base this path is unreachable (revert 422s on the sibling first). Post-fix,
    reverting only the sibling B while the bound line A stays out must leave the doc
    reporting fulfilled from A's live status, not corrupted by the off-line revert."""
    token = await _register(client)
    h = _h(token)
    memo, lot_a, lot_b = await _cross_lot_memo(client, h, "XR-COH")

    # Revert only the sibling B; the bound lot A stays out at the customer.
    r = await client.post(f"/docs/{memo}/revert-lines", headers=h,
                          json={"line_entity_ids": [lot_b]})
    assert r.status_code == 200, f"reverting the sibling must succeed; got {r.status_code}: {r.text}"

    # A is still memo_out; B is back in stock.
    assert (await client.get(f"/items/{lot_a}", headers=h)).json().get("status") == "memo_out"
    assert (await client.get(f"/items/{lot_b}", headers=h)).json().get("status") != "memo_out"

    # The doc's fulfillment_status reflects the still-out bound line, not a corrupted value.
    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("fulfillment_status") in ("fulfilled", "partial"), (
        f"doc status must stay coherent after an off-line sibling revert; got "
        f"{doc.get('fulfillment_status')!r}")
    # A is still out, so Close must still refuse (the bound line remains an allocation).
    rc = await client.post(f"/docs/{memo}/close", headers=h, json={})
    assert rc.status_code == 409, f"close must still refuse while A is out; got {rc.text}"


@pytest.mark.asyncio
async def test_cross_lot_convert_bills_sku_rollup(client):
    """Converting a cross-lot memo bills the memo line for the FULL still-out quantity of
    its SKU (bound lot A + sibling B) at the line's unit price, settles both lots, and a
    later re-convert does not re-bill the settled SKU.

    At merge-base convert enumerates only line_items, so B's still-out quantity is never
    billed: the invoice carries only A's quantity (or 422s if A was already returned).
    Post-fix the invoice line carries the summed still-out quantity across the allocation
    set (2 units) at the line's unit price, and both lots leave memo_out."""
    token = await _register(client)
    h = _h(token)
    memo, lot_a, lot_b = await _cross_lot_memo(client, h, "XC-SPAN", unit_price=10)

    r = await client.post(f"/docs/{memo}/convert", headers=h)
    assert r.status_code == 200, f"convert of a cross-lot memo must succeed; got {r.status_code}: {r.text}"
    target_id = r.json()["target_doc_id"]

    # The invoice bills the full still-out quantity of the SKU: A (1) + B (1) = 2 units.
    inv = (await client.get(f"/docs/{target_id}", headers=h)).json()
    inv_lines = inv.get("line_items") or []
    total_qty = sum(float(li.get("quantity") or 0) for li in inv_lines
                    if (li.get("sku") == "XC-SPAN"))
    assert abs(total_qty - 2.0) < 1e-9, (
        f"invoice must bill the full SKU still-out quantity (2 units across A+B); "
        f"got {total_qty} from {inv_lines!r}")

    # Both lots are settled (no longer memo_out on this memo).
    assert (await client.get(f"/items/{lot_a}", headers=h)).json().get("status") != "memo_out"
    assert (await client.get(f"/items/{lot_b}", headers=h)).json().get("status") != "memo_out"

    # A re-convert finds nothing still memo_out and must refuse (no double-billing).
    r2 = await client.post(f"/docs/{memo}/convert", headers=h)
    assert r2.status_code == 422, (
        f"a second convert must refuse (nothing left On Memo); got {r2.status_code}: {r2.text}")
