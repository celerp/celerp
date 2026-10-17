# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The fixed asset posting roles are added like work in progress: without guessing.

A new company's chart maps all nine. A chart an older release seeded gets the missing
accounts added (intangible assets, 1230, every seeded chart already holds) and mapped on
the next start, only while its anchor accounts are still as seeded, and the shared gain or loss on disposal account is added once for both
roles. Gain and loss on disposal may share one account of either type, as exchange
gain and loss do.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from celerp.accounting_roles import (
    ROLE_GROUPS,
    ROLES_KEY,
    SCHEMA_KEY,
    SCOPES_KEY,
    SEEDED_TARGETS,
    UNGUESSED_ROLES,
    AccountRole,
    POSTING_ROLES_SCHEMA,
    allowed_types,
    pair_of,
)
from celerp_accounting.models import Account
from celerp.services.company_lock import locked_company
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

R = AccountRole
FA_ROLES = (R.INTANGIBLE_ASSETS, R.ACCUMULATED_DEPRECIATION, R.DEPRECIATION_EXPENSE, R.ASSETS_UNDER_CONSTRUCTION,
            R.ASSET_DISPOSAL_CLEARING, R.DISPOSAL_GAIN, R.DISPOSAL_LOSS, R.IMPAIRMENT_LOSS,
            R.REVALUATION_SURPLUS)
ADDED_CODES = ("1160", "1240", "3400", "6410", "6980")


async def _settings(session, cid) -> dict:
    session.expire_all()
    return (await locked_company(session, cid)).settings


async def _codes(session, cid, code: str) -> list:
    session.expire_all()
    return list((await session.execute(select(Account).where(
        Account.company_id == cid, Account.code == code))).scalars())


async def _older_release(session, cid, *, extra: dict | None = None) -> None:
    """The company as a release before fixed assets left it: no roles, no new accounts."""
    company = await locked_company(session, cid)
    drop = {r.value for r in FA_ROLES}
    roles = {k: v for k, v in company.settings[ROLES_KEY].items() if k not in drop}
    scopes = {k: v for k, v in company.settings[SCOPES_KEY].items() if k not in drop}
    company.settings = {**company.settings, SCHEMA_KEY: 3, ROLES_KEY: roles, SCOPES_KEY: scopes, **(extra or {})}
    await session.execute(delete(Account).where(Account.company_id == cid, Account.code.in_(ADDED_CODES)))
    await session.commit()


async def test_the_fixed_asset_roles_are_one_group_never_guessed():
    assert set(ROLE_GROUPS["fixed_assets"]) == {R.FIXED_ASSETS, *FA_ROLES}
    assert set(FA_ROLES) <= set(UNGUESSED_ROLES)
    assert POSTING_ROLES_SCHEMA == 4


async def test_a_new_company_has_every_fixed_asset_role_mapped(session, auth):
    settings = await _settings(session, auth["company_id"])
    for role in FA_ROLES:
        assert settings[ROLES_KEY][role.value] == SEEDED_TARGETS[role]
        assert settings[SCOPES_KEY][role.value] == [SEEDED_TARGETS[role]]


async def test_an_older_seeded_chart_gets_the_accounts_added_and_mapped_once(session, auth):
    cid = auth["company_id"]
    await _older_release(session, cid)

    await _startup(session)
    await _startup(session)

    settings = await _settings(session, cid)
    assert settings[SCHEMA_KEY] == POSTING_ROLES_SCHEMA
    for role in FA_ROLES:
        assert settings[ROLES_KEY][role.value] == SEEDED_TARGETS[role]
    for code in ADDED_CODES:
        assert len(await _codes(session, cid, code)) == 1
    (shared,) = await _codes(session, cid, "6980")
    assert (shared.account_type, shared.parent_code) == ("expense", "6000")


async def test_a_restored_chart_gets_no_fixed_asset_account(session, auth):
    cid = auth["company_id"]
    await _older_release(session, cid, extra={"restored_backup": {"backup_id": "b-fa"}})

    await _startup(session)

    settings = await _settings(session, cid)
    for code in ADDED_CODES:
        assert await _codes(session, cid, code) == []
    assert not {r.value for r in FA_ROLES} & set(settings[ROLES_KEY])


async def test_a_changed_anchor_leaves_its_roles_to_posting_accounts(session, auth):
    cid = auth["company_id"]
    await _older_release(session, cid)
    (fixed,) = await _codes(session, cid, "1210")
    fixed.parent_code = "1100"
    await session.commit()

    await _startup(session)

    roles = (await _settings(session, cid))[ROLES_KEY]
    assert R.ACCUMULATED_DEPRECIATION.value not in roles
    assert R.ASSETS_UNDER_CONSTRUCTION.value not in roles
    assert R.INTANGIBLE_ASSETS.value not in roles
    assert await _codes(session, cid, "1240") == []
    assert roles[R.DEPRECIATION_EXPENSE.value] == "6400"


async def test_gain_and_loss_on_disposal_share_an_account_of_either_type():
    assert pair_of(R.DISPOSAL_GAIN.value) == (R.DISPOSAL_GAIN, R.DISPOSAL_LOSS)
    assert pair_of(R.FX_LOSS.value) == (R.FX_GAIN, R.FX_LOSS)
    assert pair_of(R.DEPRECIATION_EXPENSE.value) is None
    shared = {R.DISPOSAL_GAIN.value: "6980", R.DISPOSAL_LOSS.value: "6980"}
    assert {"revenue", "expense"} <= allowed_types(R.DISPOSAL_GAIN.value, shared)
