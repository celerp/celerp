# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Work in progress is a posting role like any other, added without guessing.

A new company's chart carries a Work in Progress account under Inventory, mapped to
the role. A company whose chart an older release seeded gets that account added and
mapped on the next start, but only when its inventory accounts are still exactly as
seeded; an account already holding the code is never changed, and a chart from a
migration or a restored backup never gets one, leaving the choice to Posting Accounts.
The role is needed only by a company that manufactures.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from celerp.accounting_roles import (
    INVENTORY_VALUE_ROLES,
    POSTABLE_ROLES,
    POSTING_ROLES_SCHEMA,
    ROLES_KEY,
    SCHEMA_KEY,
    SCOPES_KEY,
    AccountRole,
)
from celerp_accounting.models import Account
from celerp.services.company_lock import locked_company
from celerp.services.posting_readiness import readiness
from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

WIP = AccountRole.WORK_IN_PROGRESS.value


async def _account(session, cid, code: str):
    session.expire_all()
    return (await session.execute(select(Account).where(
        Account.company_id == cid, Account.code == code))).scalar_one_or_none()


async def _older_release(session, cid, *, extra: dict | None = None) -> None:
    """The company as a release before work in progress left it: no role, no account."""
    company = await locked_company(session, cid)
    roles = {k: v for k, v in company.settings[ROLES_KEY].items() if k != WIP}
    scopes = {k: v for k, v in company.settings[SCOPES_KEY].items() if k != WIP}
    company.settings = {**company.settings, SCHEMA_KEY: 1, ROLES_KEY: roles, SCOPES_KEY: scopes, **(extra or {})}
    await session.execute(delete(Account).where(Account.company_id == cid, Account.code == "1130-WIP"))
    await session.commit()


async def _roles(session, cid) -> dict:
    session.expire_all()
    return (await locked_company(session, cid)).settings


async def test_work_in_progress_is_a_postable_asset_role_outside_the_lot_inventory_roles():
    role = AccountRole.WORK_IN_PROGRESS
    assert role in POSTABLE_ROLES
    assert role not in INVENTORY_VALUE_ROLES
    assert POSTING_ROLES_SCHEMA >= 2


async def test_a_new_company_has_a_work_in_progress_account_under_inventory(session, auth):
    cid = auth["company_id"]
    account = await _account(session, cid, "1130-WIP")
    assert (account.account_type, account.parent_code, account.is_active) == ("asset", "1130", True)
    settings = await _roles(session, cid)
    assert settings[ROLES_KEY][WIP] == "1130-WIP"
    assert settings[SCOPES_KEY][WIP] == ["1130-WIP"]


async def test_an_older_seeded_chart_gets_the_account_added_and_mapped_once(session, auth):
    cid = auth["company_id"]
    await _older_release(session, cid)

    await _startup(session)
    await _startup(session)

    account = await _account(session, cid, "1130-WIP")
    assert (account.name, account.account_type, account.parent_code) == ("Inventory - Work in Progress", "asset", "1130")
    settings = await _roles(session, cid)
    assert settings[SCHEMA_KEY] == POSTING_ROLES_SCHEMA
    assert settings[ROLES_KEY][WIP] == "1130-WIP"
    count = (await session.execute(select(Account.id).where(
        Account.company_id == cid, Account.code == "1130-WIP"))).all()
    assert len(count) == 1


async def test_an_existing_account_holding_the_code_is_never_changed_or_mapped_when_unsuitable(session, auth):
    cid = auth["company_id"]
    await _older_release(session, cid)
    session.add(Account(company_id=cid, code="1130-WIP", name="Wages in payment", account_type="expense",
                        parent_code=None))
    await session.commit()

    await _startup(session)

    account = await _account(session, cid, "1130-WIP")
    assert (account.name, account.account_type, account.parent_code) == ("Wages in payment", "expense", None)
    assert WIP not in (await _roles(session, cid))[ROLES_KEY]


async def test_a_user_account_holding_the_code_is_never_taken_to_be_work_in_progress(session, auth):
    """An asset the user opened under the seeded number before work in progress existed is
    theirs, whatever it holds; the company chooses its account in Posting Accounts."""
    cid = auth["company_id"]
    await _older_release(session, cid)
    session.add(Account(company_id=cid, code="1130-WIP", name="Workbench tools", account_type="asset",
                        parent_code="1200"))
    await session.commit()

    await _startup(session)
    await _startup(session)

    account = await _account(session, cid, "1130-WIP")
    assert (account.name, account.account_type, account.parent_code) == ("Workbench tools", "asset", "1200")
    assert WIP not in (await _roles(session, cid))[ROLES_KEY]


@pytest.mark.parametrize("change", ["renamed_parent", "inactive_purchased", "missing_opening"])
async def test_a_chart_whose_inventory_accounts_are_not_as_seeded_gets_no_account(session, auth, change):
    cid = auth["company_id"]
    await _older_release(session, cid)
    if change == "renamed_parent":
        (await _account(session, cid, "1130-P")).parent_code = "1100"
    elif change == "inactive_purchased":
        (await _account(session, cid, "1130-P")).is_active = False
    else:
        await session.execute(delete(Account).where(Account.company_id == cid, Account.code == "1130-OB"))
    await session.commit()

    await _startup(session)

    assert await _account(session, cid, "1130-WIP") is None
    assert WIP not in (await _roles(session, cid))[ROLES_KEY]


@pytest.mark.parametrize("marker", [
    {"restored_backup": {"backup_id": "b-1"}},
    {"posting_source_controls": {"inventory_purchased": ["1300"]}},
], ids=["restored", "migrated"])
async def test_a_restored_or_migrated_chart_is_never_given_or_mapped_a_guessed_account(session, auth, marker):
    cid = auth["company_id"]
    await _older_release(session, cid, extra=marker)

    await _startup(session)
    assert await _account(session, cid, "1130-WIP") is None
    assert WIP not in (await _roles(session, cid))[ROLES_KEY]

    # Even an account holding the default code is not taken to be work in progress.
    session.add(Account(company_id=cid, code="1130-WIP", name="Work in Progress", account_type="asset",
                        parent_code="1130"))
    await session.commit()
    await _startup(session)
    assert WIP not in (await _roles(session, cid))[ROLES_KEY]


async def test_work_in_progress_is_needed_only_once_the_company_manufactures(client, session, auth):
    cid, h = auth["company_id"], auth["headers"]
    await _older_release(session, cid, extra={"restored_backup": {"backup_id": "b-2"}})
    r = await client.post("/items", headers=h, json={
        "sku": "RAW-RD", "name": "Raw", "quantity": 5, "sell_by": "piece", "cost_price": 2.0})
    assert r.status_code == 200, r.text

    rows = {row["role"]: row for row in await readiness(session, cid)}
    assert rows[WIP]["required"] is False
    assert rows[WIP]["group"] == "manufacturing"

    raw = r.json()["id"]
    made = (await client.post("/items", headers=h, json={
        "sku": "FG-RD", "name": "Out", "quantity": 0, "sell_by": "piece"})).json()["id"]
    r = await client.post("/manufacturing", headers=h, json={
        "description": "Run", "inputs": [{"item_id": raw, "quantity": 1}], "output_item_id": made})
    assert r.status_code == 200, r.text
    rows = {row["role"]: row for row in await readiness(session, cid)}
    assert rows[WIP]["required"] is True
    assert rows[WIP]["proposal"]["code"] == "1130-WIP"
