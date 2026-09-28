# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from test_helpers import import_sent_po


async def _register(client):
    r = await client.post("/auth/register", json={"company_name": "Reports Co", "email": f"x-{uuid.uuid4().hex[:8]}@rep.test", "name": "Admin", "password": "validpass1"})
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_reports_all_branches(client):
    token = await _register(client)
    today = date.today()
    past_10 = (today - timedelta(days=10)).isoformat()
    past_40 = (today - timedelta(days=40)).isoformat()
    past_100 = (today - timedelta(days=100)).isoformat()

    # docs to feed reports
    async def _issued(body: dict) -> str:
        r = await client.post("/docs", headers=_h(token), json=body)
        doc_id = r.json()["id"]
        ri = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))
        assert ri.status_code == 200, ri.text
        return doc_id

    await _issued({"doc_type": "invoice", "contact_id": "c1", "contact_name": "C1", "line_items": [{"item_id": "i1", "name": "Item1", "quantity": 2, "unit_price": 25, "line_total": 50, "cost_total": 30}], "subtotal": 50, "tax": 0, "total": 50, "date": past_10, "due_date": past_10})
    partial = await _issued({"doc_type": "invoice", "contact_id": "c2", "contact_name": "C2", "line_items": [{"item_id": "i2", "name": "Item2", "quantity": 1, "unit_price": 80, "line_total": 80, "cost_total": 20}], "subtotal": 80, "tax": 0, "total": 80, "date": past_40, "due_date": past_40})
    rp = await client.post(f"/docs/{partial}/payment", headers=_h(token), json={"amount": 30.0, "payment_date": past_40, "bank_account": "1110"})
    assert rp.status_code == 200, rp.text
    await import_sent_po(
        client, _h(token), contact_id="s1", contact_name="S1", subtotal=100, total=100,
        date=past_100, expected_delivery=past_100,
        line_items=[{"item_id": "p1", "name": "P1", "quantity": 5, "unit_price": 20, "line_total": 100}],
    )

    # AR/AP aging
    ar = (await client.get("/reports/ar-aging", headers=_h(token))).json()
    ap = (await client.get("/reports/ap-aging", headers=_h(token))).json()
    assert len(ar["lines"]) >= 1
    assert len(ap["lines"]) >= 1

    # sales by customer/item/period
    s1 = (await client.get("/reports/sales?group_by=customer", headers=_h(token))).json()
    s2 = (await client.get("/reports/sales?group_by=item", headers=_h(token))).json()
    s3 = (await client.get("/reports/sales?group_by=period&period=weekly", headers=_h(token))).json()
    assert s1["group_by"] == "customer" and len(s1["lines"]) >= 1
    assert s2["group_by"] == "item" and len(s2["lines"]) >= 1
    assert s3["group_by"] == "period" and len(s3["lines"]) >= 1

    # purchases by supplier/item/period
    p1 = (await client.get("/reports/purchases?group_by=supplier", headers=_h(token))).json()
    p2 = (await client.get("/reports/purchases?group_by=item", headers=_h(token))).json()
    p3 = (await client.get("/reports/purchases?group_by=period&period=daily", headers=_h(token))).json()
    assert p1["group_by"] == "supplier" and len(p1["lines"]) >= 1
    assert p2["group_by"] == "item" and len(p2["lines"]) >= 1
    assert p3["group_by"] == "period" and len(p3["lines"]) >= 1

    exp = (await client.get("/reports/expiring?days=30", headers=_h(token))).json()
    assert "count" in exp
