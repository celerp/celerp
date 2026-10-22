# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""One-time save of customs codes on shipping documents issued by an earlier release.

Issue now copies each line's HS code and country of origin from its item
(routes.freeze_customs_lines), so later item edits never reach an issued document. An earlier
release saved none: its issued documents printed the item's current codes. This pass gives each
of them the codes it prints today, so nothing on paper changes, and tells each company once.

Runs at startup, gated by a marker so it runs once per database. Each list is saved in its own
savepoint: a list that fails is rolled back alone, logged, and the marker stays unset so the next
start tries it again. The hook never raises, so the module is never held back by it.
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
    """Save the customs codes on every issued customs list that lacks them. Caller commits."""
    from celerp.migrations._data_reconcile import get_meta, set_meta
    from celerp.models.projections import Projection
    from celerp.notifications.service import notify_once
    from celerp.services.list_behavior import DRAFT, LIST_TYPES, behavior
    from celerp_docs.routes import freeze_customs_lines
    from ui.i18n import t

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, CUSTOMS_FREEZE_KEY)):
        return {"frozen": 0, "failed": 0}

    customs_types = [lt for lt in LIST_TYPES if behavior(lt).customs]
    rows = (await session.execute(
        select(Projection.company_id, Projection.entity_id)
        .where(Projection.entity_type == "list",
               Projection.state["list_type"].as_string().in_(customs_types),
               Projection.state["status"].as_string() != DRAFT)
        .order_by(Projection.company_id, Projection.entity_id)
    )).all()
    frozen: dict = {}
    failed = 0
    for company_id, entity_id in rows:
        try:
            async with session.begin_nested():
                row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
                lines = list((row.state or {}).get("line_items") or [])
                filled = await freeze_customs_lines(session, company_id, lines)
                if filled == lines:
                    continue
                await emit_event(
                    session, company_id=company_id, entity_id=entity_id, entity_type="list",
                    event_type="list.updated", data={"fields_changed": {"line_items": {"new": filled}}},
                    actor_id=None, location_id=None, source="system",
                    idempotency_key=f"customs-freeze:{entity_id}",
                )
        except Exception:
            failed += 1
            log.exception("Could not save customs codes on issued list %s", entity_id)
            continue
        frozen[company_id] = frozen.get(company_id, 0) + 1

    for company_id, count in frozen.items():
        await notify_once(session, company_id, "system", t(f"{_NOTICE}.title", "en"),
                          t(f"{_NOTICE}.body", "en", count=count), action_url="/lists",
                          i18n={"title": f"{_NOTICE}.title", "body": f"{_NOTICE}.body", "params": {"count": count}})
    if not failed:
        await conn.run_sync(lambda c: set_meta(c, CUSTOMS_FREEZE_KEY, "done"))
    total = sum(frozen.values())
    if total or failed:
        log.info("Saved customs codes on %d issued list(s); %d failed%s", total, failed,
                 " (marker left unset; the next start retries)" if failed else "")
    return {"frozen": total, "failed": failed}


async def customs_freeze_hook(*, session: AsyncSession) -> None:
    try:
        await freeze_issued_customs_lists(session)
    except Exception:
        log.exception("Saving customs codes on issued lists failed; the next start retries")
