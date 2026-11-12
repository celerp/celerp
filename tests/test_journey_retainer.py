# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A consultant's retainer billed by hand without card payments, from the first bill to the
roll-forward, through the API the pages use.

Initial conditions: a fresh company with the seeded chart and no Celerp Cloud connection, so
automatic collection is not offered. The business day is frozen per step. The retainer bills
3100.00 a month in advance and recognizes each bill over its month: January's 31 days and
February's 28 days are each 3100.00. Changing to 2 seats from 15 February leaves 14 of
February's 28 days: the credit note gives back 1550.00 and the debit invoice bills 3100.00.
"""
from __future__ import annotations

import uuid

import pytest

from test_credit_note_settlement_owned import _post
from test_revenue_recognition import _freeze, _net, _post_month, _runs, _schedules

pytestmark = pytest.mark.asyncio


async def _generate(client, auth, eid: str, action: str = "generate-due") -> dict:
    r = await _post(client, auth, f"/subscriptions/{eid}/{action}", {"request_key": str(uuid.uuid4())})
    assert r.status_code == 200, r.text
    return r.json()


async def _retainer(client, auth) -> tuple[str, str]:
    h = auth["headers"]
    payer = await client.post("/crm/contacts", headers=h, json={"name": "Harbor Logistics", "email": "ap@harbor.test"})
    assert payer.status_code in (200, 201), payer.text
    r = await client.post("/docs", headers=h, json={
        "doc_type": "subscription_invoice", "kind": "retainer", "contact_id": payer.json()["id"],
        "frequency": "monthly", "billing_timing": "advance", "start_date": "2026-01-01",
        "line_items": [{"description": "Advisory retainer", "quantity": 1, "unit_price": 3100.0, "line_total": 3100.0}]})
    assert r.status_code in (200, 201), r.text
    eid = r.json()["id"]
    assert (await client.patch(f"/subscriptions/{eid}/schedule", headers=h,
                               json={"recognize_over_service_period": True})).status_code == 200
    assert (await _post(client, auth, f"/subscriptions/{eid}/activate")).status_code == 200
    line = (await client.get(f"/docs/{eid}", headers=h)).json()["line_items"][0]["line_id"]
    return eid, line


async def test_e2e2_retainer_without_card_payments(client, session, auth, monkeypatch):
    h = auth["headers"]
    _freeze(monkeypatch, "2026-01-05")
    eid, line = await _retainer(client, auth)
    assert (await client.get(f"/subscriptions/{eid}/collection", headers=h)).json()["offered"] is False
    assert [s["id"] for s in (await client.get("/subscriptions", headers=h, params={"kind": "retainer"})).json()["items"]] == [eid]

    january = (await _generate(client, auth, eid))["doc_id"]
    february = (await _generate(client, auth, eid, "bill-early"))["doc_id"]
    [jan_schedule] = await _schedules(session, auth, january)
    [feb_schedule] = await _schedules(session, auth, february)
    assert jan_schedule["segments"] == [{"start": "2026-01-01", "end": "2026-01-31", "amount": 3100.0}]
    assert feb_schedule["segments"] == [{"start": "2026-02-01", "end": "2026-02-28", "amount": 3100.0}]
    deferred = jan_schedule["deferred_account"]
    assert await _net(session, auth, deferred) == -6200.0

    _freeze(monkeypatch, "2026-02-03")
    assert {r["status"] for r in await _runs(client, auth) if r["month"] == "2026-01"} == {"outstanding"}
    assert (await _post_month(client, auth, "2026-01")).status_code == 200
    assert {r["status"] for r in await _runs(client, auth) if r["month"] == "2026-01"} == {"posted"}

    _freeze(monkeypatch, "2026-02-15")
    r = await _post(client, auth, f"/subscriptions/{eid}/amend",
                    {"effective": "2026-02-15", "quantities": {line: 2}, "request_key": str(uuid.uuid4())})
    assert r.status_code == 200, r.text
    credit = (await client.get(f"/docs/{r.json()['credit_note_id']}", headers=h)).json()
    debit = (await client.get(f"/docs/{r.json()['invoice_id']}", headers=h)).json()
    assert (credit["total"], debit["total"]) == (1550.0, 3100.0)
    [debit_schedule] = await _schedules(session, auth, r.json()["invoice_id"])
    assert debit_schedule["segments"] == [{"start": "2026-02-15", "end": "2026-02-28", "amount": 3100.0}]

    r = await client.patch(f"/subscriptions/{eid}/schedule", headers=h, json={"cancel_at_period_end": True})
    assert r.status_code == 200, r.text

    _freeze(monkeypatch, "2026-03-05")
    assert (await _generate(client, auth, eid))["period_index"] == 1  # nothing after February
    items = (await client.get("/subscriptions", headers=h, params={"kind": "retainer"})).json()["items"]
    assert items[0]["next_bill_date"] is None
    assert (await _post_month(client, auth, "2026-02")).status_code == 200

    roll = (await client.get("/accounting/recognition/roll-forward", headers=h,
                             params={"date_from": "2026-01-01", "date_to": "2026-02-28"})).json()
    assert (roll["opening"], roll["deferred"], roll["recognized"]) == (0, 9300.0, 7750.0), roll
    assert roll["adjusted"] == 1550.0 and roll["closing"] == roll["liability"] == 0, roll  # the credit note
    assert await _net(session, auth, deferred) == 0
