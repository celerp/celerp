# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Tell each company its saved taxes still hold a row an earlier seed wrote and that is no
longer right (tax_regimes.RETIRED_SEED_ROWS).

Runs at every startup, and for one company whenever its tax lists are saved, imported or its
migration finishes. A company's taxes are its own, so this only reads them: each matched
entry is one notice, told once, linking to a tax list that still holds it; an entry the company
has since fixed has its notice removed, so a notice that stands is always still true. A company
staged for a migration is left as it is until its migration finishes.

The same pass tells a company which of its quotations still hold totals an earlier version
worked out by the old list rule (tax before the header discount, rate-only lines rounded once),
which the document rule now gives them; their stored totals are left as they are.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.notifications import service as notification_service
from celerp.services.list_behavior import DRAFT, FINALIZED, is_money_list
from celerp.services.migrations import is_company_migration_staged
from celerp.tax_regimes import RETIRED_SEED_ROWS, retired_seed_rows
from ui.i18n import t

log = logging.getLogger(__name__)

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


_LIST_NOTICE = "notice.list_tax_rule"


async def reconcile_list_tax_notice(session: AsyncSession, company_id, settings: dict | None) -> None:
    """Tell the company which of its open lists (draft or issued) hold totals the document rule
    would change, by number; reads only, and clears the notice once none does. Without Documents
    (turned off for the company, or not started in this process) there is no rule to judge its lists
    by, so its notice is left as it is. Caller commits."""
    from celerp.modules.boundary import ModuleOff, require_on
    try:
        require_on(settings, "celerp-docs")
    except ModuleOff:
        return
    from celerp_docs.doc_money import list_totals_differ

    title = t(f"{_LIST_NOTICE}.title", "en")
    rows = (await session.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "list",
        Projection.state["status"].as_string().in_((DRAFT, FINALIZED))))).scalars().all()
    refs = sorted(str(s.get("ref_id") or "") for s in rows
                  if is_money_list(s.get("list_type")) and list_totals_differ(s))
    if not refs:
        await notification_service.clear_standing(session, company_id, _CATEGORY, title)
        return
    params = {"count": len(refs), "refs": ", ".join(refs)}
    await notification_service.notify_standing(
        session, company_id, _CATEGORY, title, t(f"{_LIST_NOTICE}.body", "en", **params), action_url="/lists",
        i18n={"title": f"{_LIST_NOTICE}.title", "body": f"{_LIST_NOTICE}.body", "params": params})


async def run_tax_seed_notice(session: AsyncSession) -> None:
    """Bring every company's retired-seed notices up to date. Each company is its own step: one
    that fails is logged and left for a later start, and every other company is still told.
    Caller commits."""
    for company_id, settings in (await session.execute(select(Company.id, Company.settings))).all():
        try:
            async with session.begin_nested():
                await reconcile_tax_seed_notices(session, company_id, settings)
                if not await is_company_migration_staged(session, company_id):
                    await reconcile_list_tax_notice(session, company_id, settings)
        except Exception:
            log.exception("Could not bring the tax seed notices of company %s up to date", company_id)
