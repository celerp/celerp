# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A request key names one request.

Initial conditions per test: a fresh company with the seeded chart (1111 the default bank),
funds turned on, two unrestricted funds made through POST /accounting/funds and 500 of cash
in the first, posted by a manual entry. A request sent
again under its key with the same body answers what the first answered and posts nothing more;
the key sent with any field different is refused (409) and nothing is posted.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def _funds(client, auth) -> tuple[str, str]:
    r = await client.put("/accounting/funds/setting", headers=auth["headers"], json={"enabled": True})
    assert r.status_code == 200, r.text
    ids = []
    for name in ("Board reserve", "Building reserve"):
        r = await client.post("/accounting/funds", headers=auth["headers"], json={
            "request_key": uuid.uuid4().hex, "name": name})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-02-01", "memo": "Opening cash", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "1111", "debit": 500.0, "credit": 0.0, "fund_id": ids[0]},
                    {"account": "3200", "debit": 0.0, "credit": 500.0, "fund_id": ids[0]}]})
    assert r.status_code == 200, r.text
    return ids[0], ids[1]


async def test_a_transfer_key_sent_with_another_amount_is_refused(client, auth):
    """A transfer's request key sent again with a different amount is refused, not answered with
    the first transfer as if the second had posted."""
    a, b = await _funds(client, auth)
    body = {"request_key": uuid.uuid4().hex, "date": "2026-03-01", "reason": "Move", "from_fund": a,
            "to_fund": b, "amount": 10}
    first = await client.post("/accounting/funds/transfer", headers=auth["headers"], json=body)
    assert first.status_code == 200, first.text
    again = await client.post("/accounting/funds/transfer", headers=auth["headers"], json=body)
    assert again.status_code == 200 and again.json() == first.json(), again.text
    other = await client.post("/accounting/funds/transfer", headers=auth["headers"], json={**body, "amount": 25})
    assert other.status_code == 409, other.text
