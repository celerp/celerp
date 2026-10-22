# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The startup pass that tells a company its stored tax rows match a retired seed."""
from __future__ import annotations

import copy
import json
import logging
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from celerp.models.company import Company
from celerp.models.notification import Notification
from celerp.services.company_lock import locked_company

from test_tax_seeds import _OLD_ET

pytestmark = pytest.mark.asyncio


async def _company(session, *, sales: list[dict] | None = None, purchasing: list[dict] | None = None) -> uuid.UUID:
    settings: dict = {}
    if sales is not None:
        settings["taxes"] = copy.deepcopy(sales)
    if purchasing is not None:
        settings["purchasing_taxes"] = copy.deepcopy(purchasing)
    company_id = uuid.uuid4()
    session.add(Company(id=company_id, name="Seed notice co", slug=f"seed-{company_id.hex[:8]}", settings=settings))
    await session.commit()
    return company_id


async def _run(session) -> None:
    from celerp.services.tax_seed_notice import run_tax_seed_notice

    await run_tax_seed_notice(session)
    await session.commit()


async def _notices(session, company_id) -> dict[str, Notification]:
    """The company's tax seed notices, keyed by their retired-seed entry."""
    rows = (await session.execute(
        select(Notification).where(Notification.company_id == company_id))).scalars().all()
    prefix = "notice.tax_seed."
    return {n.i18n["title"][len(prefix):-len(".title")]: n for n in rows
            if (n.i18n or {}).get("title", "").startswith(prefix)}


async def test_tax_seed_notice_one_per_entry_and_idempotent(session):
    from celerp.tax_regimes import TAX_REGIMES

    old = await _company(session, sales=_OLD_ET)
    fresh = await _company(session, sales=TAX_REGIMES["TH"]["taxes"])

    await _run(session)
    notices = await _notices(session, old)
    assert set(notices) == {"et-wht-2025", "et-tot-2025"}
    assert all(n.category == "accounting" and n.priority == "high" for n in notices.values())
    assert all(n.i18n["body"].endswith(".body") for n in notices.values())
    assert await _notices(session, fresh) == {}

    await _run(session)
    assert {k: n.id for k, n in (await _notices(session, old)).items()} == {k: n.id for k, n in notices.items()}


async def test_tax_seed_notice_failure_is_logged_and_start_continues(monkeypatch, caplog):
    from test_main_lifespan import _boot_in_order

    monkeypatch.setattr("celerp.services.tax_seed_notice.run_tax_seed_notice",
                        AsyncMock(side_effect=RuntimeError("tax rows unreadable")))
    with caplog.at_level(logging.ERROR, logger="celerp.main"):
        order, _notify, app = await _boot_in_order(
            monkeypatch, AsyncMock(return_value={"changed": False, "rebuilt": False, "current": True}))

    assert "tax rows unreadable" in caplog.text
    assert app.state.data_current is True
    assert "cogs_backfill" in order


async def test_tax_seed_notice_clears_when_row_fixed(session):
    company_id = await _company(session, sales=_OLD_ET)
    await _run(session)
    assert set(await _notices(session, company_id)) == {"et-wht-2025", "et-tot-2025"}

    company = await locked_company(session, company_id)
    fixed = [r for r in copy.deepcopy(_OLD_ET) if r["name"] != "WHT 2%"]
    fixed.append({"name": "WHT 3%", "rate": -3.0, "tax_type": "purchase", "is_default": False, "is_compound": False})
    company.settings = {**company.settings, "taxes": fixed}
    await session.commit()

    await _run(session)
    assert set(await _notices(session, company_id)) == {"et-tot-2025"}


async def test_tax_seed_notice_links_purchasing_tab_for_purchasing_match(session):
    vat = [r for r in _OLD_ET if r["name"] == "VAT 15%"]
    wht = [r for r in _OLD_ET if r["name"] == "WHT 2%"]
    purchasing_only = await _company(session, sales=vat, purchasing=vat + wht)
    sales_side = await _company(session, sales=_OLD_ET)

    await _run(session)
    notices = await _notices(session, purchasing_only)
    assert set(notices) == {"et-wht-2025"}
    assert notices["et-wht-2025"].action_url == "/settings/purchasing?tab=taxes"
    assert (await _notices(session, sales_side))["et-wht-2025"].action_url == "/settings/sales?tab=taxes"


async def test_tax_seed_notice_never_changes_company_taxes(session):
    vat = [r for r in _OLD_ET if r["name"] == "VAT 15%"]
    company_id = await _company(session, sales=_OLD_ET, purchasing=vat + _OLD_ET[-1:])
    before = json.dumps((await session.get(Company, company_id)).settings, sort_keys=True)

    await _run(session)
    session.expire_all()
    assert await _notices(session, company_id)
    assert json.dumps((await session.get(Company, company_id)).settings, sort_keys=True) == before
