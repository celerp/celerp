# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Automatic accounting posts new recognition to the company's posting roles.

Every automatic line names the role it was posted for, a role the company has not
mapped stops the operation with a pointer to the fix instead of falling back to a
default code, and a balance keeps living on the account it was recognized on after
the role moves.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from celerp.accounting_roles import POSTING_ACCOUNTS_PATH, ROLES_KEY, SEEDED_TARGETS
from celerp.models.company import Company
from celerp.services import auto_je
from celerp.services.account_roles import PostingRoleError, set_role
from celerp.services.company_lock import locked_company
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net


async def _account(client, auth, code: str, account_type: str, parent_code: str | None = None) -> None:
    body = {"code": code, "name": f"Account {code}", "account_type": account_type}
    if parent_code:
        body["parent_code"] = parent_code
    r = await client.post("/accounting/accounts", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text


async def _remap(session, auth, role: str, code: str) -> None:
    await set_role(session, auth["company_id"], role, code)
    await session.commit()


async def _unmap(session, auth, role: str) -> None:
    company = await locked_company(session, auth["company_id"])
    roles = dict(company.settings[ROLES_KEY])
    roles.pop(role)
    company.settings = {**company.settings, ROLES_KEY: roles}
    await session.commit()


async def _invoice(client, auth, total: float, finalize: bool = True, **extra):
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"name": "Service", "quantity": 1, "unit_price": total}],
        "total": total, **extra,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    if not finalize:
        return doc_id
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc_id


async def _pay(client, auth, doc_id: str, amount: float, **extra):
    return await client.post(f"/docs/{doc_id}/payment", headers=auth["headers"], json={
        "amount": amount, "payment_date": "2026-03-02", "bank_account": "1111", **extra})


@pytest.mark.asyncio
async def test_a_new_company_starts_with_every_role_mapped_to_its_seeded_account(session, auth):
    settings = (await session.get(Company, auth["company_id"], populate_existing=True)).settings
    assert settings[ROLES_KEY] == {r.value: c for r, c in SEEDED_TARGETS.items()}


@pytest.mark.asyncio
async def test_reconcile_never_creates_an_account_and_changes_nothing_when_run_again(session, auth):
    from celerp_accounting.models import Account

    from celerp.services.account_roles import reconcile_company

    cid = auth["company_id"]
    await session.execute(Account.__table__.delete().where(Account.company_id == cid, Account.code == "1130-FRT"))
    company = await locked_company(session, cid)
    company.settings = {k: v for k, v in company.settings.items() if not k.startswith("posting_")}
    await session.flush()
    count = (await session.execute(select(func.count()).select_from(Account).where(Account.company_id == cid))).scalar()

    unmapped = await reconcile_company(session, cid)
    first = dict((await session.get(Company, cid)).settings)
    # Work in progress takes its seeded account only when Celerp has just created it.
    assert set(unmapped) == {"landed_freight", "work_in_progress"}
    assert "landed_freight" not in first[ROLES_KEY]
    assert await reconcile_company(session, cid) == unmapped
    assert dict((await session.get(Company, cid)).settings) == first
    assert (await session.execute(select(func.count()).select_from(Account).where(Account.company_id == cid))).scalar() == count


@pytest.mark.asyncio
async def test_finalized_invoice_lines_record_the_role_each_was_posted_for(session, client, auth):
    inv = await _invoice(client, auth, 100.0)
    je = await _state(session, auth, f"je:auto:{inv}:fin")
    assert {(e["account"], tuple(e["account_roles"])) for e in je["entries"]} == {
        ("1120", ("receivable",)), ("4100", ("sales_revenue",))}


@pytest.mark.asyncio
async def test_an_unmapped_role_stops_finalize_and_points_at_the_fix(session, client, auth):
    await _unmap(session, auth, "receivable")
    inv = await _invoice(client, auth, 100.0, finalize=False)
    r = await client.post(f"/docs/{inv}/finalize", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert "accounts receivable" in r.json()["detail"]
    assert r.headers["X-Celerp-Fix"] == POSTING_ACCOUNTS_PATH
    assert await _state(session, auth, f"je:auto:{inv}:fin") == {}
    assert await _account_net(session, auth["company_id"], "1120") == 0.0


@pytest.mark.asyncio
async def test_a_payment_after_the_receivable_moves_settles_where_the_invoice_was_recognized(session, client, auth):
    await _account(client, auth, "1121", "asset")
    old = await _invoice(client, auth, 100.0)
    await _remap(session, auth, "receivable", "1121")
    new = await _invoice(client, auth, 40.0)

    assert (await _pay(client, auth, old, 100.0)).status_code == 200
    assert (await _pay(client, auth, new, 40.0)).status_code == 200
    cid = auth["company_id"]
    assert (await _account_net(session, cid, "1120"), await _account_net(session, cid, "1121")) == (0.0, 0.0)
    pay = await _state(session, auth, f"je:auto:{old}:pay:0")
    assert {e["account"]: e.get("account_roles") for e in pay["entries"]}["1120"] == ["receivable"]


@pytest.mark.asyncio
async def test_voiding_a_payment_reverses_the_accounts_it_moved_after_a_remap(session, client, auth):
    await _account(client, auth, "1121", "asset")
    inv = await _invoice(client, auth, 100.0)
    assert (await _pay(client, auth, inv, 100.0)).status_code == 200
    await _remap(session, auth, "receivable", "1121")
    r = await client.post(f"/docs/{inv}/void-payment", headers=auth["headers"], json={"payment_index": 0})
    assert r.status_code == 200, r.text
    cid = auth["company_id"]
    assert await _account_net(session, cid, "1120") == 100.0
    assert await _account_net(session, cid, "1121") == 0.0
    assert await _account_net(session, cid, "1111") == 0.0


@pytest.mark.asyncio
async def test_exchange_differences_post_to_the_gain_and_loss_roles(session, client, auth):
    await _account(client, auth, "4910", "revenue")
    await _account(client, auth, "6961", "expense")
    await _remap(session, auth, "fx_gain", "4910")
    await _remap(session, auth, "fx_loss", "6961")
    gain = await _invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    loss = await _invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    assert (await _pay(client, auth, gain, 100.0, conversion_rate=1.2)).status_code == 200
    assert (await _pay(client, auth, loss, 100.0, conversion_rate=1.0)).status_code == 200
    cid = auth["company_id"]
    assert await _account_net(session, cid, "4910") == -10.0
    assert await _account_net(session, cid, "6961") == 10.0
    assert await _account_net(session, cid, "6960") == 0.0
    lines = (await _state(session, auth, f"je:auto:{gain}:pay:0"))["entries"]
    assert [e["account_roles"] for e in lines if e["account"] == "4910"] == [["fx_gain"]]


@pytest.mark.asyncio
async def test_a_bill_expense_line_posts_to_the_general_expense_role(session, client, auth):
    await _account(client, auth, "6951", "expense")
    await _remap(session, auth, "general_expense", "6951")
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "bill", "line_items": [{"name": "Rent", "quantity": 1, "unit_price": 50.0,
                                            "receive_as": "expense"}], "total": 50.0})
    assert r.status_code == 200, r.text
    bill = r.json()["id"]
    r = await client.post(f"/docs/{bill}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    cid = auth["company_id"]
    assert (await _account_net(session, cid, "6951"), await _account_net(session, cid, "6950")) == (50.0, 0.0)
    assert await _account_net(session, cid, "2110") == -50.0


@pytest.mark.asyncio
async def test_audit_shrinkage_and_overage_post_to_their_roles(session, auth):
    cid = auth["company_id"]
    from celerp_accounting.models import Account
    session.add_all([Account(company_id=cid, code="6971", name="Shrink", account_type="expense"),
                     Account(company_id=cid, code="4301", name="Gain", account_type="revenue")])
    await session.flush()
    await set_role(session, cid, "stock_shrinkage", "6971")
    await set_role(session, cid, "stock_gain", "4301")
    await auto_je.create_for_audit_adjustment(
        session, company_id=cid, user_id=auth["user_id"], list_id="list:audit-roles",
        shrinkage={"1130-P": 7.0}, overage={"1130-P": 3.0})
    await session.commit()
    assert (await _account_net(session, cid, "6971"), await _account_net(session, cid, "4301")) == (7.0, -3.0)
    assert (await _account_net(session, cid, "6970"), await _account_net(session, cid, "4300")) == (0.0, 0.0)


@pytest.mark.asyncio
async def test_a_missing_stock_role_refuses_the_cogs_entry_rather_than_guessing(session, auth):
    await _unmap(session, auth, "cogs")
    with pytest.raises(PostingRoleError):
        await auto_je.create_for_doc_cogs_backfill(
            session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="doc:none", by_account={"1130-P": 5.0}, ts=None)
