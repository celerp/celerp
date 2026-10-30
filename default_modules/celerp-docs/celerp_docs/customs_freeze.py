# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""One-time save of the item details on shipping documents issued by an earlier release.

Issue now copies each line's HS code and country of origin from its item and saves the item as it
is (weights, measures, barcode; routes.customs_snapshot), so later item edits never reach an
issued document. An earlier release saved none: its issued documents printed the item as it is
now. This pass gives each of them what it prints today, so nothing on paper changes, and tells
each company once. Until a list is saved (``customs_saved``) its print and share read the item, so
a list this pass could not save never prints blank.

Runs at startup, gated by a marker so it runs once per database. A company staged for a
migration, or that has Documents turned off, is skipped and waits for a later start. Each list is saved in its own savepoint: a list that fails is rolled back
alone and logged. Either way the marker stays unset so the next start tries it again. The hook never raises, so the module is never held back by it.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event

log = logging.getLogger(__name__)

CUSTOMS_FREEZE_KEY = "customs_freeze"
_NOTICE = "notice.customs_freeze"


async def freeze_issued_customs_lists(session: AsyncSession) -> dict:
    """Save the item details on every issued customs list that lacks them. Caller commits."""
    from celerp.migrations._data_reconcile import get_meta, set_meta
    from celerp.models.company import Company
    from celerp.modules.registry import uses_module
    from celerp.models.projections import Projection
    from celerp.notifications.service import notify_once
    from celerp.services.list_behavior import DRAFT, LIST_TYPES, behavior
    from celerp.services.migrations import is_company_migration_staged
    from celerp_docs.routes import customs_snapshot
    from ui.i18n import t

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, CUSTOMS_FREEZE_KEY)):
        return {"frozen": 0, "failed": 0, "staged": 0}

    customs_types = [lt for lt in LIST_TYPES if behavior(lt).customs]
    rows = (await session.execute(
        select(Projection.company_id, Projection.entity_id)
        .where(Projection.entity_type == "list",
               Projection.state["list_type"].as_string().in_(customs_types),
               Projection.state["status"].as_string() != DRAFT,
               Projection.state["customs_saved"].as_boolean().isnot(True))
        .order_by(Projection.company_id, Projection.entity_id)
    )).all()
    frozen: dict = {}
    failed = 0
    staged: dict = {}
    for company_id, entity_id in rows:
        if company_id not in staged:
            company = await session.get(Company, company_id)
            staged[company_id] = (await is_company_migration_staged(session, company_id)
                                  or not uses_module(company.settings if company is not None else None, "celerp-docs"))
        if staged[company_id]:
            continue
        try:
            async with session.begin_nested():
                row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
                if (row.state or {}).get("customs_saved"):
                    continue
                lines = list((row.state or {}).get("line_items") or [])
                saved = await customs_snapshot(session, company_id, lines)
                await emit_event(
                    session, company_id=company_id, entity_id=entity_id, entity_type="list",
                    event_type="list.updated",
                    data={"fields_changed": {k: {"new": v} for k, v in saved.items()}},
                    actor_id=None, location_id=None, source="system",
                    idempotency_key=f"customs-freeze:{entity_id}",
                )
        except Exception:
            failed += 1
            log.exception("Could not save item details on issued list %s", entity_id)
            continue
        if saved["line_items"] != lines:
            frozen[company_id] = frozen.get(company_id, 0) + 1

    for company_id in frozen:
        await notify_once(session, company_id, "system", t(f"{_NOTICE}.title", "en"),
                          t(f"{_NOTICE}.body", "en"), action_url="/lists",
                          i18n={"title": f"{_NOTICE}.title", "body": f"{_NOTICE}.body"})
    if not failed and not any(staged.values()):
        await conn.run_sync(lambda c: set_meta(c, CUSTOMS_FREEZE_KEY, "done"))
    total = sum(frozen.values())
    waiting = sum(staged.values())
    if total or failed or waiting:
        log.info("Saved item details on %d issued list(s); %d failed, %d company(ies) staged for a migration "
                 "or without Documents%s",
                 total, failed, waiting, " (marker left unset; the next start retries)" if failed or waiting else "")
    return {"frozen": total, "failed": failed, "staged": waiting}


async def customs_freeze_hook(*, session: AsyncSession) -> None:
    try:
        await freeze_issued_customs_lists(session)
    except Exception:
        log.exception("Saving item details on issued lists failed; the next start retries")
