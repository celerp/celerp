# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Imports reach a consignment's consignor the way the app does: an issued invoice
imported on consigned goods records their consignor at that first sale, and a batch
import that updates a consignment cannot change a consignor its sales already fixed."""
from __future__ import annotations

import uuid

import pytest

from test_consignment_in_sale import _consign, _sell, _settled, _state
from test_consignor_payable_per_consignor import CONSIGNOR_FIELD, _consignor, _owed_each

pytestmark = pytest.mark.asyncio


def _imported_invoice(lot: str, sku: str, qty: float) -> dict:
    return {"doc_type": "invoice", "status": "final", "doc_number": f"IMP-{uuid.uuid4().hex[:6]}",
            "issue_date": "2026-02-01", "contact_id": "customer:1",
            "line_items": [{"item_id": lot, "sku": sku, "name": "Lot", "quantity": qty,
                            "unit_price": 40.0, "line_total": 40.0 * qty}],
            "subtotal": 40.0 * qty, "total": 40.0 * qty, "amount_outstanding": 40.0 * qty}


def _record(data: dict, entity_id: str | None = None) -> dict:
    return {"entity_id": entity_id or f"doc:{uuid.uuid4()}", "event_type": "doc.created", "source": "test",
            "idempotency_key": uuid.uuid4().hex, "data": data}


@pytest.mark.parametrize("route", ["single", "batch"])
async def test_an_imported_sale_records_the_consignor_at_first_sale(client, session, auth, route):
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    rec = _record(_imported_invoice(lot, (await _state(session, auth, lot))["sku"], 2))
    if route == "single":
        r = await client.post("/docs/import", headers=auth["headers"], json=rec)
        assert r.status_code == 200, r.text
    else:
        r = await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [rec]})
        assert r.status_code == 200 and r.json()["created"] == 1, r.text
    session.expire_all()
    assert (await _state(session, auth, lot))[CONSIGNOR_FIELD] == a
    r = await client.patch(f"/docs/{con}", headers=auth["headers"],
                           json={"fields_changed": {"contact_id": {"new": b}}})
    assert r.status_code == 409, r.text
    assert await _owed_each(client, auth, a, b) == (8.0, 0.0, 0.0)
    await _settled(client, session, auth)


async def test_a_batch_update_cannot_change_a_fixed_consignor(client, session, auth):
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    await _sell(client, session, auth, lot, 1)
    snap = await _state(session, auth, con)
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={
        "upsert": True, "records": [_record({**snap, "contact_id": b}, con)]})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 0 and r.json()["errors"], r.text
    assert "consignor is fixed" in " ".join(r.json()["errors"])
    session.expire_all()
    assert (await _state(session, auth, con))["contact_id"] == a
    assert await _owed_each(client, auth, a, b) == (4.0, 0.0, 0.0)
    await _settled(client, session, auth)
