# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The chart's own rules, enforced wherever an account is added or changed.

A parent must be an active account of the same company and of a type the child
can sit under; a move can never make an account its own ancestor. An account a
posting role currently points at cannot be deactivated or turned into a type the
role cannot use, and an account with journal history keeps its type. The same
rules hold for one account at a time and for a chart import.
"""

from __future__ import annotations

import uuid

import pytest

from celerp.services.company_lock import locked_company


async def _reg(client, name="RulesCo") -> dict:
    addr = f"rules-{uuid.uuid4().hex[:8]}@chart.test"
    r = await client.post("/auth/register", json={
        "company_name": name, "email": addr, "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _company_id(client, h) -> uuid.UUID:
    r = await client.get("/companies/me", headers=h)
    assert r.status_code == 200, r.text
    return uuid.UUID(r.json()["id"])


async def _chart(client, h) -> dict[str, dict]:
    r = await client.get("/accounting/chart", headers=h)
    assert r.status_code == 200, r.text
    return {a["code"]: a for a in r.json()["items"]}


async def _create(client, h, code, account_type="asset", parent_code=None):
    return await client.post("/accounting/accounts", headers=h, json={
        "code": code, "name": f"Account {code}", "account_type": account_type, "parent_code": parent_code})


async def _patch(client, h, account, **fields):
    return await client.patch(f"/accounting/accounts/{account}", headers=h, json=fields)


async def _other_company_account(session, code) -> uuid.UUID:
    from celerp.models.company import Company
    from celerp_accounting.models import Account

    other = uuid.uuid4()
    session.add(Company(id=other, name="Other", slug=f"other-{other.hex[:8]}", settings={}))
    await session.flush()
    session.add(Account(id=uuid.uuid4(), company_id=other, code=code, name="Other", account_type="asset"))
    await session.commit()
    return other


async def _point_role(session, company_id, role, code):
    """Set the role straight in settings, as the role panel will store it."""
    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    settings["posting_roles"] = {**(settings.get("posting_roles") or {}), role: code}
    settings["posting_role_scopes"] = {**(settings.get("posting_role_scopes") or {}), role: [code]}
    company.settings = settings
    await session.commit()


@pytest.mark.asyncio
async def test_parent_from_another_company_is_refused(client, session):
    h_a = await _reg(client, "A")
    await _other_company_account(session, "8800")
    r = await _create(client, h_a, "8801", "asset", parent_code="8800")
    assert r.status_code == 422, r.text
    assert "8800" in r.json()["detail"]
    assert "8801" not in await _chart(client, h_a)


@pytest.mark.asyncio
async def test_new_account_under_a_parent_of_another_type_is_refused(client, session):
    h = await _reg(client)
    r = await _create(client, h, "8802", "revenue", parent_code="1100")
    assert r.status_code == 422, r.text
    assert "8802" not in await _chart(client, h)


@pytest.mark.asyncio
async def test_cost_of_sales_may_sit_under_expenses(client, session):
    h = await _reg(client)
    r = await _create(client, h, "8803", "cogs", parent_code="6000")
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_new_account_under_an_inactive_parent_is_refused(client, session):
    h = await _reg(client)
    assert (await _create(client, h, "8810", "asset")).status_code == 200
    assert (await _patch(client, h, "8810", is_active=False)).status_code == 200
    r = await _create(client, h, "8811", "asset", parent_code="8810")
    assert r.status_code == 422, r.text
    assert "inactive" in r.json()["detail"]


@pytest.mark.asyncio
async def test_account_cannot_be_its_own_parent(client, session):
    h = await _reg(client)
    assert (await _create(client, h, "8820", "asset")).status_code == 200
    r = await _patch(client, h, "8820", parent_code="8820")
    assert r.status_code == 422, r.text
    assert (await _chart(client, h))["8820"]["parent_code"] is None


@pytest.mark.asyncio
async def test_move_that_closes_a_loop_is_refused(client, session):
    h = await _reg(client)
    assert (await _create(client, h, "8830", "asset")).status_code == 200
    assert (await _create(client, h, "8831", "asset", parent_code="8830")).status_code == 200
    assert (await _create(client, h, "8832", "asset", parent_code="8831")).status_code == 200
    r = await _patch(client, h, "8830", parent_code="8832")
    assert r.status_code == 422, r.text
    assert (await _chart(client, h))["8830"]["parent_code"] is None


@pytest.mark.asyncio
async def test_move_under_a_parent_of_another_type_is_refused(client, session):
    h = await _reg(client)
    assert (await _create(client, h, "8840", "asset")).status_code == 200
    r = await _patch(client, h, "8840", parent_code="4000")
    assert r.status_code == 422, r.text
    assert (await _chart(client, h))["8840"]["parent_code"] is None


@pytest.mark.asyncio
async def test_move_to_a_missing_parent_is_refused(client, session):
    h = await _reg(client)
    assert (await _create(client, h, "8845", "asset")).status_code == 200
    r = await _patch(client, h, "8845", parent_code="NOPE")
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_type_change_that_strands_a_child_is_refused(client, session):
    h = await _reg(client)
    assert (await _create(client, h, "8850", "asset")).status_code == 200
    assert (await _create(client, h, "8851", "asset", parent_code="8850")).status_code == 200
    r = await _patch(client, h, "8850", account_type="liability")
    assert r.status_code == 422, r.text
    assert (await _chart(client, h))["8850"]["account_type"] == "asset"


@pytest.mark.asyncio
async def test_deactivating_a_role_target_is_refused_with_the_role_named(client, session):
    h = await _reg(client)
    cid = await _company_id(client, h)
    await _point_role(session, cid, "receivable", "1120")
    r = await _patch(client, h, "1120", is_active=False)
    assert r.status_code == 409, r.text
    assert "Accounts receivable" in r.json()["detail"]
    assert (await _chart(client, h))["1120"]["is_active"] is True


@pytest.mark.asyncio
async def test_retyping_a_role_target_to_a_type_the_role_cannot_use_is_refused(client, session):
    h = await _reg(client)
    cid = await _company_id(client, h)
    assert (await _create(client, h, "8860", "revenue")).status_code == 200
    await _point_role(session, cid, "sales_revenue", "8860")
    r = await _patch(client, h, "8860", account_type="expense")
    assert r.status_code == 409, r.text
    assert "Sales revenue" in r.json()["detail"]
    assert (await _chart(client, h))["8860"]["account_type"] == "revenue"


@pytest.mark.asyncio
async def test_retyping_an_account_with_journal_history_is_refused(client, session):
    h = await _reg(client)
    assert (await _create(client, h, "8870", "expense")).status_code == 200
    assert (await _create(client, h, "8871", "liability")).status_code == 200
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-01-05", "memo": "History", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "8870", "debit": 10}, {"account": "8871", "credit": 10}]})
    assert r.status_code == 200, r.text
    r = await _patch(client, h, "8870", account_type="cogs")
    assert r.status_code == 409, r.text
    assert (await _chart(client, h))["8870"]["account_type"] == "expense"


@pytest.mark.asyncio
async def test_unused_account_can_still_be_retyped_and_renamed(client, session):
    h = await _reg(client)
    assert (await _create(client, h, "8880", "expense")).status_code == 200
    r = await _patch(client, h, "8880", account_type="cogs", name="Renamed")
    assert r.status_code == 200, r.text
    row = (await _chart(client, h))["8880"]
    assert (row["account_type"], row["name"]) == ("cogs", "Renamed")


@pytest.mark.asyncio
async def test_chart_import_refuses_a_parent_of_another_type(client, session):
    h = await _reg(client)
    r = await client.post("/accounting/accounts/import/batch", headers=h, json={"records": [
        {"code": "8890", "name": "Fine", "account_type": "asset", "parent_code": "1100"},
        {"code": "8891", "name": "Wrong side", "account_type": "revenue", "parent_code": "1100"},
        {"code": "8892", "name": "Header", "account_type": "expense"},
        {"code": "8893", "name": "Under file header", "account_type": "revenue", "parent_code": "8892"},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 2
    assert any("8891" in e for e in body["errors"])
    assert any("8893" in e for e in body["errors"])
    chart = await _chart(client, h)
    assert "8890" in chart and "8891" not in chart and "8893" not in chart


@pytest.mark.asyncio
async def test_chart_import_refuses_an_inactive_parent(client, session):
    h = await _reg(client)
    assert (await _create(client, h, "8895", "asset")).status_code == 200
    assert (await _patch(client, h, "8895", is_active=False)).status_code == 200
    r = await client.post("/accounting/accounts/import/batch", headers=h, json={"records": [
        {"code": "8896", "name": "Under inactive", "account_type": "asset", "parent_code": "8895"},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 0


@pytest.mark.asyncio
async def test_account_code_is_not_changeable(client, session):
    h = await _reg(client)
    assert (await _create(client, h, "8898", "asset")).status_code == 200
    r = await _patch(client, h, "8898", code="8899")
    chart = await _chart(client, h)
    assert "8898" in chart and "8899" not in chart, r.text
