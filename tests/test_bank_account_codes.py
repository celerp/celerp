# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Bank accounts get their chart code automatically, with no limit on how many a
company holds. Every code is unique, stays inside the 1110 bank block (its leading
number is a bank code, so range tests still classify it as cash), and the codes sort
in the order the accounts were added."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.asyncio


async def _owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Many Banks Co", "email": "many-banks@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_more_than_a_hundred_bank_accounts_get_valid_sorted_codes(client):
    headers = await _owner(client)
    seeded = [a["code"] for a in (await client.get("/accounting/chart", headers=headers)).json()["items"]
              if "1110" < a["code"] < "1120"]
    codes = []
    for n in range(105):
        r = await client.post("/accounting/bank-accounts", json={
            "bank_name": f"Bank {n}", "account_number": f"{n:04d}",
            "bank_type": "checking", "currency": "USD"}, headers=headers)
        assert r.status_code == 200, (n, r.text)
        codes.append(r.json()["chart_account_code"])
    assert len(set(codes)) == len(codes)
    assert codes == sorted(codes)
    assert all(1110 <= int(re.match(r"\d+", c).group()) <= 1119 for c in codes)
    assert all(c < "1120" for c in codes)
    r = await client.get("/accounting/chart", headers=headers)
    assert r.status_code == 200, r.text
    chart = {a["code"]: a for a in r.json()["items"]}
    assert all(chart[c]["parent_code"] == "1110" for c in codes)
    in_block = [code for code in chart if "1110" < code < "1120"]
    assert in_block == seeded + codes
