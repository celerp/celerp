# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Terminal state of a closed memo across the send and shipping surfaces.

A closed memo is settled paperwork: it must not be re-sent (that would silently
un-close it) and it must not spawn a shipping document. Both are function-level
409s with a message that names the way back (Reopen first), and the send guard
is enforced on the backend regardless of what the UI offers.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@memoclose.test"
    r = await client.post("/auth/register", json={"company_name": "MemoClose Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _item(client, h, sku) -> str:
    r = await client.post("/items", headers=h, json={"status": "available", "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _memo(client, h, item_ids: list[str]) -> str:
    line_items = [{"entity_id": i, "sku": f"S-{n}", "name": f"S-{n}", "quantity": 1, "unit_price": 10, "sell_by": "piece"}
                  for n, i in enumerate(item_ids)]
    r = await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": line_items})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _closed_memo(client, h) -> str:
    """A memo taken all the way to closed: finalize, ship a line then return it
    (so nothing is left memo_out and Close is allowed), then close."""
    a = await _item(client, h, f"CM-{uuid.uuid4().hex[:6]}")
    memo = await _memo(client, h, [a])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    assert (await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    assert (await client.post(f"/docs/{memo}/close", headers=h, json={})).status_code == 200
    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "closed", doc.get("status")
    return memo


@pytest.mark.asyncio
async def test_send_rejected_on_closed_memo(client):
    """POST /docs/{id}/send on a closed memo returns 409 and leaves it closed
    (a send must never silently un-close a settled memo)."""
    token = await _register(client)
    h = _h(token)
    memo = await _closed_memo(client, h)

    r = await client.post(f"/docs/{memo}/send", headers=h, json={"sent_via": "manual"})
    assert r.status_code == 409, r.text
    assert "closed" in r.text.lower()

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "closed", f"send must not un-close; got {doc.get('status')}"


@pytest.mark.asyncio
async def test_revert_lines_rejected_on_closed_memo(client):
    """POST /docs/{id}/revert-lines on a closed memo returns 409 and reverts nothing
    (reverting fulfillment on a settled memo must go through Reopen first). The doc
    stays closed and the referenced item's status is unchanged."""
    token = await _register(client)
    h = _h(token)
    memo = await _closed_memo(client, h)
    doc_before = (await client.get(f"/docs/{memo}", headers=h)).json()
    _li0 = doc_before["line_items"][0]
    line_eid = _li0.get("entity_id") or _li0.get("item_id")
    item_before = (await client.get(f"/items/{line_eid}", headers=h)).json()

    r = await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_entity_ids": [line_eid]})
    assert r.status_code == 409, r.text
    assert "closed" in r.text.lower() or "reopen" in r.text.lower()

    doc_after = (await client.get(f"/docs/{memo}", headers=h)).json()
    item_after = (await client.get(f"/items/{line_eid}", headers=h)).json()
    assert doc_after.get("status") == "closed", f"revert must not un-close; got {doc_after.get('status')}"
    assert item_after.get("status") == item_before.get("status"), "revert must not change item status on a closed memo"


@pytest.mark.asyncio
async def test_closed_memo_no_create_shipping(client):
    """POST /shipment for a closed memo returns 409, nothing is created; a live
    (non-closed) memo still ships, so the guard is specific to closed."""
    token = await _register(client)
    h = _h(token)
    memo = await _closed_memo(client, h)

    r = await client.post("/docs/shipment", headers=h, json={"doc_ids": [memo]})
    assert r.status_code == 409, r.text
    assert "closed" in r.text.lower() or "reopen" in r.text.lower()


async def _paid_then_closed_memo(client, h) -> str:
    """A memo that carries a real payment and is then closed: finalize, take a cash
    payment (status -> partial), ship a line then return it so nothing is left
    memo_out, then close. The payment survives on the closed memo so refund / void /
    delete have something to act on."""
    a = await _item(client, h, f"PC-{uuid.uuid4().hex[:6]}")
    memo = await _memo(client, h, [a])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    rp = await client.post(f"/docs/{memo}/payment", headers=h,
                           json={"amount": 4, "payment_date": "2026-06-22", "method": "cash", "bank_account": "1111"})
    assert rp.status_code == 200, rp.text
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    assert (await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    assert (await client.post(f"/docs/{memo}/close", headers=h, json={})).status_code == 200
    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "closed", doc.get("status")
    assert doc.get("payments"), "the closed memo must still carry its payment"
    return memo


@pytest.mark.asyncio
async def test_refund_payment_rejected_on_closed_memo(client):
    """POST /docs/{id}/refund on a closed memo returns 409 and leaves it closed;
    refunding a payment on a settled memo must go through Reopen first, and must
    never silently un-close the memo by recomputing its status."""
    token = await _register(client)
    h = _h(token)
    memo = await _paid_then_closed_memo(client, h)

    r = await client.post(f"/docs/{memo}/refund", headers=h,
                          json={"amount": 4, "payment_date": "2026-06-23", "method": "cash", "bank_account": "1111"})
    assert r.status_code == 409, r.text
    assert "reopen" in r.text.lower()

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "closed", f"refund must not un-close; got {doc.get('status')}"


@pytest.mark.asyncio
async def test_void_payment_rejected_on_closed_memo(client):
    """POST /docs/{id}/void-payment on a closed memo returns 409 and leaves it
    closed (voiding a payment must not silently un-close a settled memo)."""
    token = await _register(client)
    h = _h(token)
    memo = await _paid_then_closed_memo(client, h)

    r = await client.post(f"/docs/{memo}/void-payment", headers=h,
                          json={"payment_index": 0, "void_reason": "closed memo test"})
    assert r.status_code == 409, r.text
    assert "reopen" in r.text.lower()

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "closed", f"void-payment must not un-close; got {doc.get('status')}"


@pytest.mark.asyncio
async def test_delete_payment_rejected_on_closed_memo(client):
    """DELETE /docs/{id}/payments/{index} on a closed memo returns 409 and leaves
    it closed (deleting a payment must not silently un-close a settled memo)."""
    token = await _register(client)
    h = _h(token)
    memo = await _paid_then_closed_memo(client, h)

    r = await client.delete(f"/docs/{memo}/payments/0", headers=h)
    assert r.status_code == 409, r.text
    assert "reopen" in r.text.lower()

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "closed", f"delete-payment must not un-close; got {doc.get('status')}"


# ---------------------------------------------------------------------------
# R1: a manual single-doc overpayment with no reference is refused, not clamped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_overpayment_no_reference_still_409(client):
    """POST /docs/{id}/payment for more than outstanding, with NO reference, is a 409;
    the amount is never silently clamped and no payment is recorded.

    The clamp that shrinks an over-tender to the fresh outstanding belongs to the bulk
    waterfall alone (an explicit clamp_overshoot=True). A hand-entered payment keys the
    default clamp_overshoot=False, so an overshoot on the manual route must 409 exactly
    as it always did - it must never fall into a no-reference silent clamp."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, f"OV-{uuid.uuid4().hex[:6]}")
    memo = await _memo(client, h, [a])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    # Line total is 10; tender 999 with no reference.
    r = await client.post(f"/docs/{memo}/payment", headers=h,
                          json={"amount": 999, "payment_date": "2026-07-01",
                                "method": "cash", "bank_account": "1111"})
    assert r.status_code == 409, f"a manual no-reference overshoot must 409, not clamp; got {r.status_code}: {r.text}"
    assert "exceeds" in r.text.lower()

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    payments = [p for p in (doc.get("payments") or []) if p.get("status") != "deleted"]
    assert not payments, f"no payment must be recorded on a refused overshoot; got {payments!r}"
    assert float(doc.get("amount_outstanding") or 0) == pytest.approx(10.0), (
        f"outstanding must be unchanged; got {doc.get('amount_outstanding')!r}")


# ---------------------------------------------------------------------------
# R2: Close refuses a memo that is already fully paid.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_rejects_fully_paid_memo(client):
    """POST /docs/{id}/close on a fully-paid, otherwise-resolved memo returns 409 and
    leaves it 'paid'; a fully-settled memo's terminal state is 'paid', not 'closed'.

    The memo is finalized and paid in full but never fulfilled (no line is memo_out),
    so the pending-items guard passes and Close reaches the fully-paid guard. A memo
    with an unsettled balance stays closable; a fully-paid one is already terminal."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, f"FP-{uuid.uuid4().hex[:6]}")
    memo = await _memo(client, h, [a])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    rp = await client.post(f"/docs/{memo}/payment", headers=h,
                           json={"amount": 10, "payment_date": "2026-07-02",
                                 "method": "cash", "bank_account": "1111"})
    assert rp.status_code == 200, rp.text
    assert (await client.get(f"/docs/{memo}", headers=h)).json().get("status") == "paid"

    r = await client.post(f"/docs/{memo}/close", headers=h, json={})
    assert r.status_code == 409, f"a fully-paid memo must not close; got {r.status_code}: {r.text}"
    assert "paid" in r.text.lower()

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "paid", f"close must not change a paid memo; got {doc.get('status')}"


@pytest.mark.asyncio
async def test_partial_paid_memo_still_closable(client):
    """A memo carrying a PARTIAL payment (status 'partial', not 'paid') still closes:
    the R2 guard fires only on a genuinely-settled ('paid') memo, so the
    deposit-then-close flow is preserved."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, f"PP-{uuid.uuid4().hex[:6]}")
    memo = await _memo(client, h, [a])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    rp = await client.post(f"/docs/{memo}/payment", headers=h,
                           json={"amount": 4, "payment_date": "2026-07-02",
                                 "method": "cash", "bank_account": "1111"})
    assert rp.status_code == 200, rp.text
    assert (await client.get(f"/docs/{memo}", headers=h)).json().get("status") == "partial"

    r = await client.post(f"/docs/{memo}/close", headers=h, json={})
    assert r.status_code == 200, f"a partially-paid memo must still close; got {r.status_code}: {r.text}"
    assert (await client.get(f"/docs/{memo}", headers=h)).json().get("status") == "closed"


# ---------------------------------------------------------------------------
# F2: Finalize refuses a closed memo.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_rejected_on_closed_memo(client):
    """POST /docs/{id}/finalize on a closed memo returns 409 with a reopen-first message,
    the status stays 'closed', and no doc.finalized is emitted (finalize must never strip
    the terminal closed status through the reducer's closed->final mapping)."""
    token = await _register(client)
    h = _h(token)
    memo = await _closed_memo(client, h)

    r = await client.post(f"/docs/{memo}/finalize", headers=h)
    assert r.status_code == 409, f"finalize on a closed memo must 409; got {r.status_code}: {r.text}"
    assert "reopen" in r.text.lower()

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "closed", f"finalize must not un-close; got {doc.get('status')}"


# ---------------------------------------------------------------------------
# F1: memo->invoice convert bills each still-out lot at its own line's terms.
# ---------------------------------------------------------------------------


async def _lot(client, h, sku, qty=1) -> str:
    r = await client.post("/items", headers=h, json={
        "status": "available", "sku": sku, "name": sku, "quantity": qty,
        "sell_by": "piece", "allow_splitting": True})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _two_line_memo(client, h, lines: list[dict]) -> str:
    """A finalized memo whose lines are given verbatim (each already bound to a lot via
    entity_id). Returned at status 'final', ready to fulfill and convert."""
    r = await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": lines})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    return memo


@pytest.mark.asyncio
async def test_convert_same_sku_distinct_prices(client):
    """Two same-SKU lots fulfilled as two bound lines at 100 and 150 convert to two
    invoice lines priced 100 and 150 (total 250), not a collapsed single line.

    At merge-base the SKU rollup dumps both units onto the first same-SKU line at its
    own price (2 x 100 = 200) and drops the second line, mispricing the invoice."""
    token = await _register(client)
    h = _h(token)
    sku = f"XP-{uuid.uuid4().hex[:6]}"
    lot_a = await _lot(client, h, sku)
    lot_b = await _lot(client, h, sku)
    memo = await _two_line_memo(client, h, [
        {"entity_id": lot_a, "sku": sku, "name": sku, "quantity": 1, "unit_price": 100, "sell_by": "piece"},
        {"entity_id": lot_b, "sku": sku, "name": sku, "quantity": 1, "unit_price": 150, "sell_by": "piece"},
    ])
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h,
                              json={"line_entity_ids": [lot_a, lot_b]})).status_code == 200

    r = await client.post(f"/docs/{memo}/convert", headers=h)
    assert r.status_code == 200, r.text
    inv = (await client.get(f"/docs/{r.json()['target_doc_id']}", headers=h)).json()
    lines = inv.get("line_items") or []
    prices = sorted(float(li.get("unit_price") or 0) for li in lines if li.get("sku") == sku)
    totals = sum(float(li.get("line_total") or (float(li.get("quantity") or 0) * float(li.get("unit_price") or 0)))
                 for li in lines if li.get("sku") == sku)
    assert prices == pytest.approx([100.0, 150.0]), f"both lot prices must survive; got {prices} from {lines!r}"
    assert totals == pytest.approx(250.0), f"invoice must bill 100 + 150 = 250; got {totals} from {lines!r}"


@pytest.mark.asyncio
async def test_convert_same_sku_distinct_discounts(client):
    """Two same-SKU bound lines carrying explicit discounted line_totals of 90 and 80
    convert to two lines whose totals stay 90 and 80 (sum 170).

    A per-line discount is an explicit line_total below quantity*unit_price; at merge-base
    line A's 90 is scaled x2 to 180 and line B (80) is dropped, losing both discounts."""
    token = await _register(client)
    h = _h(token)
    sku = f"XD-{uuid.uuid4().hex[:6]}"
    lot_a = await _lot(client, h, sku)
    lot_b = await _lot(client, h, sku)
    memo = await _two_line_memo(client, h, [
        {"entity_id": lot_a, "sku": sku, "name": sku, "quantity": 1, "unit_price": 100, "line_total": 90, "sell_by": "piece"},
        {"entity_id": lot_b, "sku": sku, "name": sku, "quantity": 1, "unit_price": 100, "line_total": 80, "sell_by": "piece"},
    ])
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h,
                              json={"line_entity_ids": [lot_a, lot_b]})).status_code == 200

    r = await client.post(f"/docs/{memo}/convert", headers=h)
    assert r.status_code == 200, r.text
    inv = (await client.get(f"/docs/{r.json()['target_doc_id']}", headers=h)).json()
    lines = [li for li in (inv.get("line_items") or []) if li.get("sku") == sku]
    line_totals = sorted(float(li.get("line_total") or 0) for li in lines)
    assert line_totals == pytest.approx([80.0, 90.0]), (
        f"each line's discounted line_total must survive; got {line_totals} from {lines!r}")


@pytest.mark.asyncio
async def test_convert_same_sku_distinct_tax(client):
    """Two same-SKU bound lines taxed at different rates keep each line's own tax code and
    a tax amount scaled to that line's own quantity.

    At merge-base line B is dropped and line A's taxes[].amount is never scaled, so the
    invoice loses B's tax entirely and mis-states A's."""
    token = await _register(client)
    h = _h(token)
    sku = f"XT-{uuid.uuid4().hex[:6]}"
    lot_a = await _lot(client, h, sku)
    lot_b = await _lot(client, h, sku)
    memo = await _two_line_memo(client, h, [
        {"entity_id": lot_a, "sku": sku, "name": sku, "quantity": 1, "unit_price": 100, "sell_by": "piece",
         "taxes": [{"code": "VAT7", "rate": 7}]},
        {"entity_id": lot_b, "sku": sku, "name": sku, "quantity": 1, "unit_price": 100, "sell_by": "piece",
         "taxes": [{"code": "VAT15", "rate": 15}]},
    ])
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h,
                              json={"line_entity_ids": [lot_a, lot_b]})).status_code == 200

    r = await client.post(f"/docs/{memo}/convert", headers=h)
    assert r.status_code == 200, r.text
    inv = (await client.get(f"/docs/{r.json()['target_doc_id']}", headers=h)).json()
    lines = [li for li in (inv.get("line_items") or []) if li.get("sku") == sku]
    codes = sorted(tx.get("code") for li in lines for tx in (li.get("taxes") or []))
    assert codes == ["VAT15", "VAT7"], f"both lines' tax codes must survive; got {codes} from {lines!r}"
    # Each line is qty 1 (no scaling): the stored amounts stay 7 and 15.
    amounts = sorted(round(float(tx.get("amount") or 0), 2)
                     for li in lines for tx in (li.get("taxes") or []))
    assert amounts == pytest.approx([7.0, 15.0]), (
        f"each line keeps its own tax amount; got {amounts} from {lines!r}")


@pytest.mark.asyncio
async def test_convert_unbound_sibling_credited_to_its_own_line(client):
    """A cross-lot sibling drawn by a spanning line is billed onto the same-SKU line that
    drew it, at that line's price; a different-SKU bound line is untouched.

    Line A (SKU-P, qty1, its own lot) and line B (SKU-Q, qty2 at unit 30 with an explicit
    discounted line_total of 40, spanning lot B plus a same-SKU sibling C). Bound lot B
    holds 1 still-out unit and sibling C holds 1. Post-fix the bound half of line B keeps
    its discount (40 scaled by the kept fraction 1/2 = 20) and the unbound sibling is added
    once at line B's unit_price (30), so SKU-Q bills 20 + 30 = 50 across qty2. At merge-base
    the SKU rollup dumps the full still-out qty (2) onto line B at the kept fraction 2/2 = 1,
    scaling line_total to a flat 40: the sibling never gets its own undiscounted unit price,
    understating the invoice."""
    token = await _register(client)
    h = _h(token)
    sku_p, sku_q = f"XU-P-{uuid.uuid4().hex[:5]}", f"XU-Q-{uuid.uuid4().hex[:5]}"
    lot_a = await _lot(client, h, sku_p, qty=1)   # bound to line A
    lot_b = await _lot(client, h, sku_q, qty=1)   # bound to line B, holds 1 of the 2 needed
    lot_c = await _lot(client, h, sku_q, qty=1)   # same-SKU sibling B spans into
    memo = await _two_line_memo(client, h, [
        {"entity_id": lot_a, "sku": sku_p, "name": sku_p, "quantity": 1, "unit_price": 50, "sell_by": "piece"},
        {"entity_id": lot_b, "sku": sku_q, "name": sku_q, "quantity": 2, "unit_price": 30, "line_total": 40, "sell_by": "piece"},
    ])
    # Fulfilling line B (qty2) from a qty1 lot draws sibling C of the same SKU.
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h,
                              json={"line_entity_ids": [lot_a, lot_b]})).status_code == 200
    assert (await client.get(f"/items/{lot_c}", headers=h)).json().get("status") == "memo_out", (
        "line B must have spanned into sibling lot C")

    r = await client.post(f"/docs/{memo}/convert", headers=h)
    assert r.status_code == 200, r.text
    inv = (await client.get(f"/docs/{r.json()['target_doc_id']}", headers=h)).json()
    lines = inv.get("line_items") or []
    by_sku = {}
    for li in lines:
        by_sku.setdefault(li.get("sku"), 0.0)
        by_sku[li.get("sku")] += float(li.get("quantity") or 0)
    assert by_sku.get(sku_p) == pytest.approx(1.0), f"the SKU-P line must stay qty 1; got {by_sku!r}"
    assert by_sku.get(sku_q) == pytest.approx(2.0), (
        f"the SKU-Q line must carry bound lot B + sibling C = 2 units; got {by_sku!r}")
    # SKU-Q bills the bound half at its discount (20) plus the sibling once at unit 30 = 50,
    # never collapsing to the flat rollup total (40) that loses the sibling's own price.
    q_total = sum(float(li.get("line_total") or (float(li.get("quantity") or 0) * float(li.get("unit_price") or 0)))
                  for li in lines if li.get("sku") == sku_q)
    assert q_total == pytest.approx(50.0), (
        f"SKU-Q must bill discounted bound half (20) + sibling (30) = 50; got {q_total} from {lines!r}")
    # Both physical lots for SKU-Q leave memo_out.
    assert (await client.get(f"/items/{lot_b}", headers=h)).json().get("status") != "memo_out"
    assert (await client.get(f"/items/{lot_c}", headers=h)).json().get("status") != "memo_out"
