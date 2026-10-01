# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Startup maps a company's posting accounts only where its own chart proves them.

A company whose posting accounts came from its source books is never mapped onto
Celerp's default account numbers, even when its chart happens to hold one. A role
startup cannot map leaves one high-priority notice pointing at the fix, and startup
itself carries on.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, update

from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)


async def _settings(session, company_id) -> dict:
    from celerp.models.company import Company

    company = await session.get(Company, company_id, populate_existing=True)
    return dict(company.settings or {})


async def _write_settings(session, company_id, **changes) -> None:
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), **changes}
    await session.commit()


async def _startup(session) -> None:
    from celerp_accounting.routes import backfill_chart_of_accounts_hook

    await backfill_chart_of_accounts_hook(session=session)
    await session.commit()


async def _notices(session, company_id) -> list:
    from celerp.models.notification import Notification

    return list((await session.execute(select(Notification).where(
        Notification.company_id == company_id, Notification.category == "accounting",
        Notification.title == "Posting accounts need attention"))).scalars())


@pytest.mark.asyncio
async def test_startup_never_maps_a_migrated_chart_onto_default_account_numbers(session, auth):
    from celerp.accounting_roles import ROLES_KEY, SOURCE_CONTROLS_KEY

    cid = auth["company_id"]
    roles = dict((await _settings(session, cid))[ROLES_KEY])
    roles.pop("receivable")
    await _write_settings(session, cid, **{ROLES_KEY: roles, SOURCE_CONTROLS_KEY: {"payable": ["2110"]}})
    await _startup(session)
    assert "receivable" not in (await _settings(session, cid))[ROLES_KEY]


@pytest.mark.asyncio
async def test_startup_still_maps_a_default_chart(session, auth):
    from celerp.accounting_roles import ROLES_KEY

    cid = auth["company_id"]
    roles = dict((await _settings(session, cid))[ROLES_KEY])
    roles.pop("receivable")
    await _write_settings(session, cid, **{ROLES_KEY: roles})
    await _startup(session)
    assert (await _settings(session, cid))[ROLES_KEY]["receivable"] == "1120"


@pytest.mark.asyncio
async def test_a_role_startup_cannot_map_leaves_one_notice_with_the_fix(session, auth):
    from celerp.accounting_roles import POSTING_ACCOUNTS_PATH, ROLES_KEY
    from celerp_accounting.models import Account

    cid = auth["company_id"]
    roles = dict((await _settings(session, cid))[ROLES_KEY])
    roles.pop("receivable")
    await _write_settings(session, cid, **{ROLES_KEY: roles})
    await session.execute(update(Account).where(Account.company_id == cid, Account.code == "1120")
                          .values(is_active=False))
    await session.commit()
    await _startup(session)
    await _startup(session)
    [notice] = await _notices(session, cid)
    assert notice.priority == "high"
    assert notice.action_url == POSTING_ACCOUNTS_PATH
    assert "Accounts receivable" in notice.body
    assert "receivable" not in (await _settings(session, cid))[ROLES_KEY]


@pytest.mark.asyncio
async def test_a_fully_mapped_company_gets_no_notice(session, auth):
    await _startup(session)
    assert await _notices(session, auth["company_id"]) == []
