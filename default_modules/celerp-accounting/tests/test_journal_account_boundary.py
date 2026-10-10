# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every journal line passes one account boundary, whoever writes it.

The line's account must be in the posting company's own chart. A line chosen for
a posting role must use the role's current account (then active and of a type the
role allows) or an account that has served the role before. Each line keeps the
roles it was posted under, so later readers never reinterpret it from today's map.
Resolution for new recognition never falls back to a default code.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from celerp.events.engine import emit_event
from celerp.services.account_roles import PostingRoleError, resolve, resolve_many, set_role
from celerp.services.company_lock import locked_company


async def _reg(client, name="BoundaryCo") -> uuid.UUID:
    addr = f"je-{uuid.uuid4().hex[:8]}@boundary.test"
    r = await client.post("/auth/register", json={
        "company_name": name, "email": addr, "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.get("/companies/me", headers=h)
    return uuid.UUID(r.json()["id"])


async def _roles(session, company_id, roles: dict[str, str], scopes: dict[str, list[str]] | None = None):
    company = await locked_company(session, company_id)
    company.settings = {
        **(company.settings or {}),
        "posting_roles_schema": 1,
        "posting_roles": roles,
        "posting_role_scopes": scopes or {r: [c] for r, c in roles.items()},
    }
    await session.flush()


async def _post(session, company_id, entries):
    key = uuid.uuid4().hex
    return await emit_event(
        session, company_id=company_id, entity_id=f"je:test:{key}", entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={"ts": "2026-01-05", "memo": "Boundary", "entries": entries},
        actor_id=None, location_id=None, source="test", idempotency_key=key, metadata_={},
    )


async def _deactivate(session, company_id, code):
    from sqlalchemy import update

    from celerp_accounting.models import Account

    await session.execute(update(Account).where(
        Account.company_id == company_id, Account.code == code).values(is_active=False))


@pytest.mark.asyncio
async def test_line_on_an_account_missing_from_the_chart_is_refused(client, session):
    cid = await _reg(client)
    with pytest.raises(HTTPException) as exc:
        await _post(session, cid, [{"account": "9999", "debit": 5}, {"account": "1120", "credit": 5}])
    assert exc.value.status_code == 422
    assert "9999" in exc.value.detail


@pytest.mark.asyncio
async def test_line_on_another_companys_account_is_refused(client, session):
    from celerp_accounting.models import Account

    from celerp.models.company import Company

    cid_a, cid_b = await _reg(client, "A"), uuid.uuid4()
    session.add(Company(id=cid_b, name="B", slug=f"b-{cid_b.hex[:8]}", settings={}))
    await session.flush()
    session.add(Account(id=uuid.uuid4(), company_id=cid_b, code="1120", name="B AR", account_type="asset"))
    session.add(Account(id=uuid.uuid4(), company_id=cid_b, code="7777", name="B only", account_type="expense"))
    await session.flush()
    with pytest.raises(HTTPException) as exc:
        await _post(session, cid_a, [{"account": "7777", "debit": 5}, {"account": "1120", "credit": 5}])
    assert exc.value.status_code == 422
    await _post(session, cid_b, [{"account": "7777", "debit": 5}, {"account": "1120", "credit": 5}])


@pytest.mark.asyncio
async def test_lines_keep_the_roles_they_were_posted_under(client, session):
    cid = await _reg(client)
    await _roles(session, cid, {"receivable": "1120", "sales_revenue": "4100"})
    entry = await _post(session, cid, [
        {"account": "1120", "debit": 5}, {"account": "4100", "credit": 4}, {"account": "2120", "credit": 1},
    ])
    lines = entry.data["entries"]
    assert lines[0]["account_roles"] == ["receivable"]
    assert lines[1]["account_roles"] == ["sales_revenue"]
    assert lines[2]["account_roles"] == []


@pytest.mark.asyncio
async def test_explicit_empty_roles_stay_unclassified(client, session):
    cid = await _reg(client)
    await _roles(session, cid, {"receivable": "1120"})
    entry = await _post(session, cid, [
        {"account": "1120", "debit": 5, "account_roles": []}, {"account": "4100", "credit": 5},
    ])
    assert entry.data["entries"][0]["account_roles"] == []


@pytest.mark.asyncio
async def test_role_line_on_an_inactive_current_target_is_refused(client, session):
    cid = await _reg(client)
    await _roles(session, cid, {"receivable": "1120"})
    await _deactivate(session, cid, "1120")
    with pytest.raises(PostingRoleError) as exc:
        await _post(session, cid, [
            {"account": "1120", "debit": 5, "account_roles": ["receivable"]}, {"account": "4100", "credit": 5},
        ])
    assert "inactive" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_role_line_on_an_account_that_never_served_the_role_is_refused(client, session):
    cid = await _reg(client)
    await _roles(session, cid, {"receivable": "1120"})
    with pytest.raises(HTTPException) as exc:
        await _post(session, cid, [
            {"account": "1110", "debit": 5, "account_roles": ["receivable"]}, {"account": "4100", "credit": 5},
        ])
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_role_line_on_a_former_target_is_accepted(client, session):
    from celerp_accounting.models import Account

    cid = await _reg(client)
    session.add(Account(id=uuid.uuid4(), company_id=cid, code="1125", name="New AR",
                        account_type="asset", parent_code="1100"))
    await session.flush()
    await _roles(session, cid, {"receivable": "1125"}, {"receivable": ["1120", "1125"]})
    entry = await _post(session, cid, [
        {"account": "4100", "debit": 5}, {"account": "1120", "credit": 5, "account_roles": ["receivable"]},
    ])
    assert entry.data["entries"][1]["account_roles"] == ["receivable"]


@pytest.mark.asyncio
async def test_unknown_role_is_refused(client, session):
    cid = await _reg(client)
    with pytest.raises(HTTPException) as exc:
        await _post(session, cid, [
            {"account": "1120", "debit": 5, "account_roles": ["debtors"]}, {"account": "4100", "credit": 5},
        ])
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_resolution_never_falls_back_to_a_default_code(client, session):
    cid = await _reg(client)
    await _roles(session, cid, {"sales_revenue": "4100"})
    with pytest.raises(PostingRoleError) as exc:
        await resolve_many(session, cid, ["receivable", "sales_revenue"])
    assert "accounts receivable" in exc.value.detail["message"].lower()
    assert exc.value.headers["X-Celerp-Fix"] == "/settings/accounting?tab=posting-accounts"
    assert await resolve(session, cid, "sales_revenue") == "4100"


@pytest.mark.asyncio
async def test_resolution_refuses_a_target_of_the_wrong_type_or_missing(client, session):
    cid = await _reg(client)
    await _roles(session, cid, {"receivable": "4100", "payable": "2999"})
    with pytest.raises(PostingRoleError) as exc:
        await resolve(session, cid, "receivable")
    assert "must be of type asset" in exc.value.detail["message"]
    with pytest.raises(PostingRoleError) as exc:
        await resolve(session, cid, "payable")
    assert "not in the chart" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_default_deposit_must_be_a_concrete_account(client, session):
    cid = await _reg(client)
    await _roles(session, cid, {"default_deposit": "1110"})
    with pytest.raises(PostingRoleError) as exc:
        await resolve(session, cid, "default_deposit")
    assert "header" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_set_role_checks_the_target_and_grows_the_scope(client, session):
    cid = await _reg(client)
    await _roles(session, cid, {"receivable": "1120"})
    with pytest.raises(HTTPException) as exc:
        await set_role(session, cid, "receivable", "2110")
    assert exc.value.status_code == 422
    from celerp_accounting.models import Account

    session.add(Account(id=uuid.uuid4(), company_id=cid, code="1125", name="New AR",
                        account_type="asset", parent_code="1100"))
    await session.flush()
    settings = await set_role(session, cid, "receivable", "1125")
    assert settings["posting_roles"]["receivable"] == "1125"
    assert settings["posting_role_scopes"]["receivable"] == ["1120", "1125"]


@pytest.mark.asyncio
async def test_exchange_gain_and_loss_share_one_account_or_split_by_type(client, session):
    from celerp_accounting.models import Account

    cid = await _reg(client)
    await _roles(session, cid, {"fx_gain": "6960", "fx_loss": "6960"})
    assert await resolve_many(session, cid, ["fx_gain", "fx_loss"]) == {"fx_gain": "6960", "fx_loss": "6960"}
    session.add(Account(id=uuid.uuid4(), company_id=cid, code="4900", name="FX gain",
                        account_type="revenue", parent_code="4000"))
    await session.flush()
    # Split: gain on revenue, loss on expense.
    await set_role(session, cid, "fx_gain", "4900")
    # Shared again, now on a revenue account.
    await set_role(session, cid, "fx_loss", "4900")
    # Split with gain on an expense account and loss on revenue: refused.
    with pytest.raises(HTTPException):
        await set_role(session, cid, "fx_gain", "6960")


@pytest.mark.asyncio
async def test_without_the_accounting_module_only_roles_are_snapshotted(client, session, monkeypatch):
    from celerp.services import journal_accounts

    cid = await _reg(client)
    await _roles(session, cid, {"receivable": "1120"})
    monkeypatch.setattr(journal_accounts, "_chart", None)
    entry = await _post(session, cid, [{"account": "ZZZ", "debit": 5}, {"account": "1120", "credit": 5}])
    assert entry.data["entries"][1]["account_roles"] == ["receivable"]
