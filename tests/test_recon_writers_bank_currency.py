# CLASS money-in-the-wrong-currency (writer side; second finding of review-1 class 2).
"""The reconciliation's own writers post a foreign-currency bank's figures as base lines
with no currency, which the reconciliation then reads as having no amount: a foreign
currency bank with an opening balance, or one finished with a write-off or a statement
line booked from the workspace, can never be completed."""
from __future__ import annotations

import pytest

from test_reconciliation_history import admin, bank, fx_deposit, match, start

pytestmark = pytest.mark.asyncio


async def test_foreign_bank_with_an_opening_balance_completes(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="EUR", opening=1000, rate=1.08)
    sid = (await start(client, h, b["id"], "2026-09-30", 1000)).json()["id"]
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200, r.text


async def test_write_off_on_a_foreign_bank_lets_it_complete(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="JPY", opening=0)
    je = await fx_deposit(client, h, b, 1850, 0.0067, 12.40)
    sid = (await start(client, h, b["id"], "2026-09-30", 1851)).json()["id"]
    assert (await match(client, h, sid, [je])).status_code == 200
    r = await client.post(f"/accounting/reconciliation/{sid}/write-off", headers=h, json={"rate": 0.0067})
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200, r.text
