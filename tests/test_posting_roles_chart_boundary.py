# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A posting lands on a concrete account, and the chart keeps it that way.

A role that receives a debit or credit must point at an active account with nothing
under it, the same rule a manual journal follows. Only the two grouping roles, cash
and inventory, name a header whose children carry the postings. Once a role points
at an account, the chart cannot grow a child under it, and a header cannot be
switched off while accounts under it are still in use.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from celerp.accounting_roles import ROLE_TYPES, AccountRole
from celerp.services.account_roles import set_role
from celerp_accounting.chart_rules import change_account
from celerp_accounting.import_service import create_chart_account
from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)

pytestmark = pytest.mark.asyncio

_GROUPING = (AccountRole.CASH_AND_EQUIVALENTS, AccountRole.INVENTORY)
_DIRECT = [r for r in AccountRole if r not in _GROUPING]


async def _header(session, cid, code: str, account_type: str, parent: str | None = None) -> str:
    """An account ``code`` with one active child under it."""
    await create_chart_account(session, cid, code=code, name=code, account_type=account_type,
                               parent_code=parent)
    await create_chart_account(session, cid, code=f"{code}-1", name=f"{code}-1",
                               account_type=account_type, parent_code=code)
    await session.flush()
    return code


@pytest.mark.parametrize("role", _DIRECT, ids=lambda r: r.value)
async def test_a_role_that_receives_postings_refuses_a_header(session, auth, role):
    cid = auth["company_id"]
    code = await _header(session, cid, "9100", sorted(ROLE_TYPES[role])[0])
    with pytest.raises(HTTPException) as err:
        await set_role(session, cid, role.value, code)
    assert err.value.status_code == 422
    assert "header account" in err.value.detail["message"]


@pytest.mark.parametrize("role", _GROUPING, ids=lambda r: r.value)
async def test_a_grouping_role_can_name_a_header(session, auth, role):
    cid = auth["company_id"]
    code = await _header(session, cid, "9100", "asset")
    settings = await set_role(session, cid, role.value, code)
    assert settings["posting_roles"][role.value] == code


async def test_no_child_can_be_added_under_a_role_target(session, auth):
    cid = auth["company_id"]
    with pytest.raises(HTTPException) as err:
        await create_chart_account(session, cid, code="1120-9", name="Sub", account_type="asset",
                                   parent_code="1120")
    assert err.value.status_code == 422
    assert "posting account for Accounts receivable" in err.value.detail


async def test_no_account_can_be_moved_under_a_role_target(session, auth):
    cid = auth["company_id"]
    await create_chart_account(session, cid, code="9200", name="Loose", account_type="asset", parent_code=None)
    await session.flush()
    with pytest.raises(HTTPException) as err:
        await change_account(session, cid, "9200", parent_code="1120")
    assert err.value.status_code == 422
    assert "posting account for Accounts receivable" in err.value.detail


async def test_a_grouping_target_still_takes_children(session, auth):
    cid = auth["company_id"]
    acc = await create_chart_account(session, cid, code="1119", name="Petty cash", account_type="asset",
                                     parent_code="1110")
    assert acc.parent_code == "1110"


async def test_a_header_with_active_accounts_under_it_stays_active(session, auth):
    cid = auth["company_id"]
    await _header(session, cid, "9300", "expense")
    with pytest.raises(HTTPException) as err:
        await change_account(session, cid, "9300", is_active=False)
    assert err.value.status_code == 409
    assert "9300-1" in err.value.detail
    await change_account(session, cid, "9300-1", is_active=False)
    assert (await change_account(session, cid, "9300", is_active=False)).is_active is False


async def test_an_account_cannot_be_switched_on_under_an_inactive_header(session, auth):
    cid = auth["company_id"]
    await _header(session, cid, "9400", "expense")
    await change_account(session, cid, "9400-1", is_active=False)
    await change_account(session, cid, "9400", is_active=False)
    with pytest.raises(HTTPException) as err:
        await change_account(session, cid, "9400-1", is_active=True)
    assert err.value.status_code == 422
    assert "9400 is inactive" in err.value.detail


async def test_a_chart_file_cannot_add_an_account_under_a_role_target(client, auth):
    r = await client.post("/accounting/accounts/import/batch", headers=auth["headers"], json={"records": [
        {"code": "1120-9", "name": "Sub", "account_type": "asset", "parent_code": "1120"},
        {"code": "1119", "name": "Petty cash", "account_type": "asset", "parent_code": "1110"},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1
    assert "posting account for Accounts receivable" in " ".join(body["errors"])


async def test_a_refused_type_is_named_without_a_wrong_article(session, auth):
    """'a asset account' and 'a expense account' read wrong; each refusal names the type plainly."""
    cid = auth["company_id"]
    with pytest.raises(HTTPException) as served:
        await change_account(session, cid, "1120", account_type="expense")
    assert served.value.detail.endswith("; an account of type expense cannot serve it.")
    await _header(session, cid, "9300", "asset")
    with pytest.raises(HTTPException) as child:
        await change_account(session, cid, "9300", account_type="expense")
    assert child.value.detail == "Account 9300-1 (asset) sits under 9300; it cannot sit under an account of type expense."
    with pytest.raises(HTTPException) as parent:
        await create_chart_account(session, cid, code="9301", name="Fees", account_type="expense", parent_code="9300")
    assert parent.value.detail == "An account of type expense cannot sit under 9300, an account of type asset."
