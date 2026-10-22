# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Tell each company its saved taxes still hold a row an earlier seed wrote and that is no
longer right (tax_regimes.RETIRED_SEED_ROWS).

Runs at every startup, and for one company whenever its tax lists are saved, imported or its
migration finishes. A company's taxes are its own, so this only reads them: each matched
entry is one notice, told once, linking to the tax list it was found in; an entry the company
has since fixed has its notice removed, so a notice that stands is always still true. A company
staged for a migration is left as it is until its migration finishes.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company
from celerp.notifications import service as notification_service
from celerp.services.migrations import is_company_migration_staged
from celerp.tax_regimes import RETIRED_SEED_ROWS, retired_seed_rows
from ui.i18n import t

_CATEGORY = "accounting"
_TAX_TABS = {"sales": "/settings/sales?tab=taxes", "purchase": "/settings/purchasing?tab=taxes"}


async def reconcile_tax_seed_notices(session: AsyncSession, company_id, settings: dict | None) -> None:
    """Bring one company's retired-seed notices up to date with its saved *settings*, so a tax
    list saved or imported is told about, or cleared, the moment it is written. Caller commits."""
    if await is_company_migration_staged(session, company_id):
        return
    found = {m["id"]: m["lists"] for m in retired_seed_rows(settings or {})}
    for entry in RETIRED_SEED_ROWS:
        key = f"notice.tax_seed.{entry['id']}"
        title = t(f"{key}.title", "en")
        lists = found.get(entry["id"])
        if lists is None:
            await notification_service.clear_standing(session, company_id, _CATEGORY, title)
            continue
        await notification_service.notify_once(
            session, company_id, _CATEGORY, title, t(f"{key}.body", "en"),
            action_url=_TAX_TABS["sales" if "sales" in lists else "purchase"],
            i18n={"title": f"{key}.title", "body": f"{key}.body", "params": {}})


async def run_tax_seed_notice(session: AsyncSession) -> None:
    """Bring every company's retired-seed notices up to date. Caller commits."""
    for company_id, settings in (await session.execute(select(Company.id, Company.settings))).all():
        await reconcile_tax_seed_notices(session, company_id, settings)
