# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An account's code is hidden only where Celerp made the code up. A code someone chose
or imported is always shown, however it is spelled, so two accounts with the same name
can still be told apart in every picker."""

from __future__ import annotations

import uuid

import pytest

from celerp.accounting_roles import account_label

pytestmark = pytest.mark.asyncio


async def _reg(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Provenance Co", "email": f"p-{uuid.uuid4().hex[:8]}@example.com",
        "name": "Owner", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _chart(client, h) -> dict[str, dict]:
    r = await client.get("/accounting/chart", headers=h)
    assert r.status_code == 200, r.text
    return {a["code"]: a for a in r.json()["items"]}


async def test_an_account_given_a_code_like_a_generated_one_shows_its_code(client):
    h = await _reg(client)
    r = await client.post("/accounting/accounts", headers=h, json={
        "code": "Mdeadbeef", "name": "Bank charges", "account_type": "expense", "parent_code": "6000"})
    assert r.status_code == 200, r.text
    assert r.json()["code_generated"] is False
    r = await client.post("/accounting/accounts/import/batch", headers=h, json={"records": [
        {"code": "Mdeadbee0", "name": "Bank charges", "account_type": "expense", "parent_code": "6000"}]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text

    chart = await _chart(client, h)

    assert [account_label(chart[c]) for c in ("Mdeadbeef", "Mdeadbee0")] == [
        "Mdeadbeef Bank charges", "Mdeadbee0 Bank charges"]
    assert {chart[c]["code_generated"] for c in chart} == {False}


async def test_the_label_follows_the_stored_flag_never_the_spelling():
    assert account_label({"code": "Mdeadbeef", "name": "Petty cash", "code_generated": False}) == "Mdeadbeef Petty cash"
    assert account_label({"code": "1110", "name": "Petty cash", "code_generated": True}) == "Petty cash"
    assert account_label({"code": "1110", "name": None, "code_generated": True}) == "1110"
    assert account_label({"code": None, "name": "Petty cash", "code_generated": False}) == "Petty cash"
