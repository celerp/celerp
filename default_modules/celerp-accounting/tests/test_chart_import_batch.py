# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""POST /accounting/accounts/import/batch: add accounts from a chart file.

The import only ever adds. An account code already in the chart is skipped and
listed, never changed, because retyping or deactivating an account that postings
use would break them; there is no upsert or replace mode. Every row is checked
for code, name and type by the same rules as creating one account, and parent
references resolve against the chart as it will be after the import, whatever
order the rows arrive in. A parent that is missing, or a chain of parents that
loops back on itself, makes the row invalid and it is reported, not written.

Registration seeds the default chart, so these tests use codes in the 8000s,
which it does not contain.
"""

from __future__ import annotations

import uuid

import pytest

from test_helpers import grant_permission, perm_setup

PATH = "/accounting/accounts/import/batch"


async def _reg(client) -> dict:
    addr = f"chart-{uuid.uuid4().hex[:8]}@import.test"
    r = await client.post("/auth/register", json={
        "company_name": "ChartCo", "email": addr, "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _row(code, name="Account", account_type="asset", parent_code=None, **extra) -> dict:
    return {"code": code, "name": name, "account_type": account_type, "parent_code": parent_code, **extra}


async def _chart(client, h) -> dict[str, dict]:
    r = await client.get("/accounting/chart", headers=h)
    assert r.status_code == 200, r.text
    return {a["code"]: a for a in r.json()["items"]}


async def _import(client, h, records, **body):
    return await client.post(PATH, headers=h, json={"records": records, **body})


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_requires_manage_accounting(client, session):
    s = await perm_setup(client, session)
    await grant_permission(client, s["admin_h"], "manage_accounting", "admin")
    r = await _import(client, s["manager_h"], [_row("8100", "Denied")])
    assert r.status_code == 403, r.text
    assert "8100" not in await _chart(client, s["admin_h"])


@pytest.mark.asyncio
async def test_chart_import_requires_import_export_data(client, session):
    s = await perm_setup(client, session)
    await grant_permission(client, s["admin_h"], "import_export_data", "admin")
    r = await _import(client, s["manager_h"], [_row("8100", "Denied")])
    assert r.status_code == 403, r.text
    assert "8100" not in await _chart(client, s["admin_h"])


@pytest.mark.asyncio
async def test_chart_import_refuses_operator(client, session):
    s = await perm_setup(client, session)
    r = await _import(client, s["operator_h"], [_row("8100", "Denied")])
    assert r.status_code == 403, r.text
    r = await _import(client, s["manager_h"], [_row("8101", "Allowed")])
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1


# ---------------------------------------------------------------------------
# Add-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_adds_new_accounts(client):
    h = await _reg(client)
    r = await _import(client, h, [
        _row("8000", "Holding", "equity"),
        _row("8010", "Holding Reserve", "equity", "8000", is_active="false"),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["created"], body["skipped"], body["errors"]) == (2, 0, [])
    chart = await _chart(client, h)
    assert chart["8000"]["name"] == "Holding" and chart["8000"]["account_type"] == "equity"
    assert chart["8000"]["parent_code"] is None and chart["8000"]["is_active"] is True
    assert chart["8010"]["parent_code"] == "8000" and chart["8010"]["is_active"] is False


@pytest.mark.asyncio
async def test_chart_import_existing_code_is_skipped_reported_and_unchanged(client):
    h = await _reg(client)
    before = (await _chart(client, h))["1110"]
    r = await _import(client, h, [
        _row("1110", "Renamed Cash", "expense", "6000", is_active="false"),
        _row("8200", "New One"),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["created"], body["skipped"], body["errors"]) == (1, 1, [])
    assert body["skipped_codes"] == ["1110"]
    assert (await _chart(client, h))["1110"] == before


@pytest.mark.asyncio
async def test_chart_import_rejects_upsert_mode(client):
    h = await _reg(client)
    before = (await _chart(client, h))["1110"]
    r = await _import(client, h, [_row("1110", "Renamed Cash"), _row("8300", "New")], upsert=True)
    assert r.status_code == 422, r.text
    assert "only adds" in r.text
    chart = await _chart(client, h)
    assert chart["1110"] == before and "8300" not in chart


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"mode": "replace"}, {"mode": "upsert"}, {"replace": True}])
async def test_chart_import_rejects_replace_mode(client, extra):
    h = await _reg(client)
    r = await _import(client, h, [_row("8300", "New")], **extra)
    assert r.status_code == 422, r.text
    assert "8300" not in await _chart(client, h)


@pytest.mark.asyncio
async def test_chart_import_repeat_same_file_creates_nothing(client):
    h = await _reg(client)
    rows = [_row("8400", "Parent"), _row("8410", "Child", parent_code="8400")]
    first = (await _import(client, h, rows)).json()
    chart = await _chart(client, h)
    second = await _import(client, h, rows)
    assert second.status_code == 200, second.text
    assert first["created"] == 2
    assert (second.json()["created"], second.json()["skipped"]) == (0, 2)
    assert second.json()["skipped_codes"] == ["8400", "8410"]
    assert await _chart(client, h) == chart


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("row, message", [
    (_row(""), "code is required"),
    (_row("   "), "code is required"),
    (_row("8" * 33), "32 characters"),
    (_row("8500", ""), "name is required"),
    (_row("8500", "  "), "name is required"),
    (_row("8500", "Bad Type", "Current Asset"), "Account type must be one of"),
    (_row("8500", "No Type", ""), "Account type must be one of"),
    ({"code": 8500, "name": "Numeric", "account_type": "asset"}, "code must be text"),
    ("not a row", "must be an object"),
])
async def test_chart_import_validates_code_name_type(client, row, message):
    h = await _reg(client)
    r = await _import(client, h, [row, _row("8501", "Good")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1
    assert len(body["errors"]) == 1 and message in body["errors"][0], body["errors"]
    chart = await _chart(client, h)
    assert "8501" in chart and "8500" not in chart


@pytest.mark.asyncio
@pytest.mark.parametrize("payload, message", [
    ({"code": "", "name": "Blank Code", "account_type": "asset"}, "code is required"),
    ({"code": "8" * 33, "name": "Long Code", "account_type": "asset"}, "32 characters"),
    ({"code": "8600", "name": " ", "account_type": "asset"}, "name is required"),
])
async def test_create_account_applies_the_same_code_and_name_rules(client, payload, message):
    h = await _reg(client)
    r = await client.post("/accounting/accounts", headers=h, json=payload)
    assert r.status_code == 422, r.text
    assert message in r.json()["detail"]


@pytest.mark.asyncio
async def test_account_parent_code_is_trimmed_and_length_checked(client):
    h = await _reg(client)
    r = await client.post("/accounting/accounts", headers=h, json={
        "code": "8610", "name": "Long Parent", "account_type": "asset", "parent_code": "8" * 40})
    assert r.status_code == 422, r.text
    assert "Parent code must be 32 characters" in r.json()["detail"]
    r = await client.post("/accounting/accounts", headers=h, json={
        "code": "8611", "name": "Blank Parent", "account_type": "asset", "parent_code": "  "})
    assert r.status_code == 200, r.text
    assert r.json()["parent_code"] is None
    r = await client.patch("/accounting/accounts/8611", headers=h, json={"parent_code": "8" * 40})
    assert r.status_code == 422, r.text
    assert "Parent code must be 32 characters" in r.json()["detail"]
    r = await client.patch("/accounting/accounts/8611", headers=h, json={"parent_code": " 1000 "})
    assert r.status_code == 200, r.text
    assert (await _chart(client, h))["8611"]["parent_code"] == "1000"


@pytest.mark.asyncio
async def test_bank_account_code_follows_the_account_code_rules(client):
    """A bank account adds a chart account, so its code is checked the same way and a
    chart import of the same code later finds it instead of adding a near copy."""
    h = await _reg(client)
    bank = {"bank_name": "Code Bank", "account_number": "4321", "bank_type": "checking",
            "currency": "USD", "opening_balance": 0}
    r = await client.post("/accounting/bank-accounts", headers=h, json={**bank, "account_code": "Q" * 33})
    assert r.status_code == 422, r.text
    assert "32 characters" in r.json()["detail"]
    r = await client.post("/accounting/bank-accounts", headers=h, json={**bank, "account_code": " 8620 "})
    assert r.status_code == 200, r.text
    assert "8620" in await _chart(client, h)
    r = await _import(client, h, [_row("8620", "Bank Again")])
    assert (r.json()["created"], r.json()["skipped_codes"]) == (0, ["8620"])


# ---------------------------------------------------------------------------
# File size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_takes_a_file_of_more_than_500_accounts(client):
    h = await _reg(client)
    rows = [_row(f"A{i:04d}", f"Account {i}") for i in range(1200)]
    r = await _import(client, h, rows)
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["errors"]) == (1200, [])


@pytest.mark.asyncio
async def test_chart_import_over_the_limit_says_so_plainly(client):
    h = await _reg(client)
    rows = [_row(f"A{i:04d}", f"Account {i}") for i in range(2001)]
    r = await _import(client, h, rows)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "A chart file can hold up to 2000 accounts; this one has 2001."
    assert "A0000" not in await _chart(client, h)


# ---------------------------------------------------------------------------
# Parents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_resolves_parent_later_in_file(client):
    h = await _reg(client)
    r = await _import(client, h, [
        _row("8720", "Grandchild", parent_code="8710"),
        _row("8710", "Child", parent_code="8700"),
        _row("8700", "Root"),
    ])
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["errors"]) == (3, [])
    chart = await _chart(client, h)
    assert chart["8720"]["parent_code"] == "8710" and chart["8710"]["parent_code"] == "8700"


@pytest.mark.asyncio
async def test_chart_import_resolves_parent_already_in_chart(client):
    h = await _reg(client)
    r = await _import(client, h, [_row("1160", "Deposits", parent_code="1100")])
    assert (r.json()["created"], r.json()["errors"]) == (1, [])
    assert (await _chart(client, h))["1160"]["parent_code"] == "1100"


@pytest.mark.asyncio
async def test_chart_import_missing_parent_is_invalid(client):
    h = await _reg(client)
    r = await _import(client, h, [_row("8800", "Orphan", parent_code="9999"), _row("8801", "Fine")])
    body = r.json()
    assert body["created"] == 1
    assert len(body["errors"]) == 1
    assert "8800" in body["errors"][0] and "9999" in body["errors"][0]
    assert "8800" not in await _chart(client, h)


@pytest.mark.asyncio
async def test_chart_import_cycle_is_invalid(client):
    h = await _reg(client)
    r = await _import(client, h, [
        _row("8900", "A", parent_code="8920"),
        _row("8910", "B", parent_code="8900"),
        _row("8920", "C", parent_code="8910"),
        _row("8930", "Self", parent_code="8930"),
        _row("8940", "Fine"),
    ])
    body = r.json()
    assert body["created"] == 1
    assert len(body["errors"]) == 4
    assert all("loop" in e for e in body["errors"]), body["errors"]
    chart = await _chart(client, h)
    assert not {"8900", "8910", "8920", "8930"} & set(chart) and "8940" in chart


@pytest.mark.asyncio
async def test_chart_import_cycle_through_existing_account_is_invalid(client):
    h = await _reg(client)
    # An account already in the chart whose parent code names nothing yet.
    r = await client.post("/accounting/accounts", headers=h, json={
        "code": "8950", "name": "Existing", "account_type": "asset", "parent_code": "8960"})
    assert r.status_code == 200, r.text
    r = await _import(client, h, [_row("8960", "Closes The Loop", parent_code="8950")])
    body = r.json()
    assert body["created"] == 0
    assert len(body["errors"]) == 1 and "loop" in body["errors"][0]
    assert "8960" not in await _chart(client, h)


@pytest.mark.asyncio
async def test_chart_import_child_of_invalid_row_is_invalid(client):
    h = await _reg(client)
    r = await _import(client, h, [
        _row("8971", "Child", parent_code="8970"),
        _row("8970", "Bad Parent", "not-a-type"),
    ])
    body = r.json()
    assert body["created"] == 0
    assert len(body["errors"]) == 2
    assert any("8971" in e and "8970" in e for e in body["errors"]), body["errors"]
    chart = await _chart(client, h)
    assert "8970" not in chart and "8971" not in chart


@pytest.mark.asyncio
async def test_chart_import_duplicate_code_in_file_is_invalid(client):
    h = await _reg(client)
    r = await _import(client, h, [
        _row("8980", "First"), _row("8980", "Second"), _row("8981", "Child", parent_code="8980"),
    ])
    body = r.json()
    assert body["created"] == 0
    assert any("more than once" in e for e in body["errors"]), body["errors"]
    chart = await _chart(client, h)
    assert "8980" not in chart and "8981" not in chart


# ---------------------------------------------------------------------------
# is_active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_parses_is_active_explicitly(client):
    h = await _reg(client)
    cases = {
        "false": False, "FALSE": False, " no ": False, "0": False, False: False,
        "true": True, "Yes": True, "1": True, True: True, "": True, None: True,
    }
    rows = [_row(f"86{i:02d}", f"Case {i}", is_active=v) for i, v in enumerate(cases)]
    rows.append(_row("8699", "Missing field"))
    r = await _import(client, h, rows)
    assert (r.json()["created"], r.json()["errors"]) == (len(rows), [])
    chart = await _chart(client, h)
    for i, expected in enumerate(cases.values()):
        assert chart[f"86{i:02d}"]["is_active"] is expected, (i, expected)
    assert chart["8699"]["is_active"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["maybe", "inactive", "2", 1, [True]])
async def test_chart_import_rejects_unrecognized_is_active(client, value):
    h = await _reg(client)
    r = await _import(client, h, [_row("8690", "Odd", is_active=value)])
    body = r.json()
    assert body["created"] == 0
    assert len(body["errors"]) == 1 and "is_active" in body["errors"][0]
    assert "8690" not in await _chart(client, h)
