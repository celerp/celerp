# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.asyncio


def _u() -> str:
    return str(uuid.uuid4())


async def _create_invoice(journey_api, *, total: float, amount_outstanding: float | None = None) -> str:
    payload = {
        "doc_type": "invoice",
        "customer_name": f"ITest Customer {_u()[:8]}",
        "issue_date": "2026-02-28",
        "due_date": "2026-03-14",
        "currency": "THB",
        "tax": 0.0,
        "shipping": 0.0,
        "discount": 0.0,
        "total": float(total),
        "amount_outstanding": float(amount_outstanding) if amount_outstanding is not None else float(total),
        "line_items": [
            {
                "sku": f"SKU-{_u()[:8]}",
                "name": "Integration Line",
                "quantity": 1,
                "unit_price": float(total),
            }
        ],
        "idempotency_key": _u(),
    }
    r = await journey_api.post("/docs", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _get_doc(journey_api, doc_id: str) -> dict:
    r = await journey_api.get(f"/docs/{doc_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def test_invoice_partial_then_full_payment_updates_balances(journey_api):
    inv_id = await _create_invoice(journey_api, total=1000.0)

    sent = await journey_api.post(f"/docs/{inv_id}/send", json={"message": "Please pay", "idempotency_key": _u()})
    assert sent.status_code == 200, sent.text

    pay1 = await journey_api.post(
        f"/docs/{inv_id}/payment",
        json={"amount": 400.0, "method": "cash", "reference": "p1", "idempotency_key": _u()},
    )
    assert pay1.status_code == 200, pay1.text

    inv = await _get_doc(journey_api, inv_id)
    assert inv["doc_type"] == "invoice"
    assert inv["amount_paid"] == pytest.approx(400.0)
    assert inv["amount_outstanding"] == pytest.approx(600.0)
    assert inv["status"] in {"partial", "awaiting_payment", "sent", "final"}

    pay2 = await journey_api.post(
        f"/docs/{inv_id}/payment",
        json={"amount": 600.0, "method": "bank", "reference": "p2", "idempotency_key": _u()},
    )
    assert pay2.status_code == 200, pay2.text

    inv2 = await _get_doc(journey_api, inv_id)
    assert inv2["amount_paid"] == pytest.approx(1000.0)
    assert inv2["amount_outstanding"] == pytest.approx(0.0)
    assert inv2["status"] in {"paid", "received"}


async def test_invoice_void_blocks_payment(journey_api):
    inv_id = await _create_invoice(journey_api, total=500.0)

    sent = await journey_api.post(f"/docs/{inv_id}/send", json={"message": "sent", "idempotency_key": _u()})
    assert sent.status_code == 200, sent.text

    void = await journey_api.post(
        f"/docs/{inv_id}/void",
        json={"reason": "Customer cancelled", "idempotency_key": _u()},
    )
    assert void.status_code == 200, void.text

    pay = await journey_api.post(
        f"/docs/{inv_id}/payment",
        json={"amount": 100.0, "method": "cash", "reference": "after-void", "idempotency_key": _u()},
    )
    assert pay.status_code == 409


async def test_credit_note_reduces_original_invoice_outstanding(journey_api):
    inv_id = await _create_invoice(journey_api, total=1200.0)

    sent = await journey_api.post(f"/docs/{inv_id}/send", json={"message": "sent", "idempotency_key": _u()})
    assert sent.status_code == 200, sent.text

    # Partially pay so invoice isn't a trivial full-outstanding case.
    pay = await journey_api.post(
        f"/docs/{inv_id}/payment",
        json={"amount": 200.0, "method": "cash", "reference": "pre-credit", "idempotency_key": _u()},
    )
    assert pay.status_code == 200, pay.text

    before = await _get_doc(journey_api, inv_id)
    assert before["amount_outstanding"] == pytest.approx(1000.0)

    cn = await journey_api.post(
        "/docs",
        json={
            "doc_type": "credit_note",
            "customer_name": before.get("customer_name") or before.get("customer") or "Integration Customer",
            "issue_date": "2026-02-28",
            "currency": "THB",
            "total": 300.0,
            "original_doc_id": inv_id,
            "line_items": [
                {"sku": "CN-1", "name": "Return", "quantity": 1, "unit_price": 300.0}
            ],
            "idempotency_key": _u(),
        },
    )
    assert cn.status_code == 200, cn.text

    after = await _get_doc(journey_api, inv_id)
    assert after["amount_outstanding"] == pytest.approx(700.0)
