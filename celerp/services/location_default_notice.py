# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Tell each company whose extra default locations an upgrade cleared.

The one-default-location revision keeps a company's oldest default and records, in the runtime
state, which location it kept and which it cleared. At the next start each such company gets one
notice naming them and pointing to Locations, where setting another default undoes the choice.
The record of a company is removed in the same commit as its notice, so a later start tells it
nothing more; a company that cannot be told is left recorded for a later start.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.auth import SystemRuntimeState
from celerp.models.company import Location
from celerp.notifications import service as notification_service
from celerp.services.runtime_state import _SINGLETON_ID, _drain_cache_bust
from ui.i18n import t

log = logging.getLogger(__name__)

RECORD = "location_default_cleared"  # the runtime state key the revision writes
_KEY = "notice.location_default_cleared"
_LOCATIONS = "/settings/inventory?tab=locations"


async def run_location_default_notice(session: AsyncSession) -> None:
    """Tell every recorded company once and remove its record. Caller commits."""
    row = await session.get(SystemRuntimeState, _SINGLETON_ID)
    if row is None or not (row.value or {}).get(RECORD):
        return
    await session.refresh(row, with_for_update=True)
    pending = dict((row.value or {}).get(RECORD) or {})
    left = {}
    for company_id, entry in pending.items():
        try:
            async with session.begin_nested():
                await _tell(session, uuid.UUID(company_id), entry)
        except Exception:
            log.exception("Could not tell company %s its default location changed", company_id)
            left[company_id] = entry
    value = {k: v for k, v in row.value.items() if k != RECORD}
    row.value = {**value, RECORD: left} if left else value
    _drain_cache_bust()  # this row's cache is shared with the drain flag


async def _tell(session: AsyncSession, company_id: uuid.UUID, entry: dict) -> None:
    """One notice naming the kept location and the cleared ones that still exist; nothing when
    the company or its kept location is gone, or none of the cleared ones is left."""
    ids = [uuid.UUID(entry["kept"]), *(uuid.UUID(i) for i in entry["cleared"])]
    names = dict((await session.execute(select(Location.id, Location.name).where(
        Location.company_id == company_id, Location.id.in_(ids)))).all())
    kept, cleared = names.get(ids[0]), [names[i] for i in ids[1:] if i in names]
    if kept is None or not cleared:
        return
    params = {"kept": kept, "cleared": ", ".join(cleared)}
    await notification_service.notify_once(
        session, company_id, "settings", t(f"{_KEY}.title", "en"), t(f"{_KEY}.body", "en", **params),
        action_url=_LOCATIONS, i18n={"title": f"{_KEY}.title", "body": f"{_KEY}.body", "params": params})
