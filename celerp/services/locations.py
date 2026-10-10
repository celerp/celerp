# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A location reference resolves in the caller's company.

One rule for every writer: a location id named by a request, an import row or an event
must be one of the company's own locations. Another company's location, or a value that
is not a location id at all, is refused with ``location.not_found``. ``emit_event`` applies
it to every event it writes, so a route, import, batch, conversion or migration cannot
place anything at a location the company does not hold.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from celerp.accounting_roles import refusal
from celerp.models.company import Location

# The event-data keys that name a local location. ``from_location_id`` is history the
# writer reads from the item itself, so it is never a caller's choice.
LOCATION_REFERENCE_KEYS = ("location_id", "to_location_id")


def _not_found(value) -> HTTPException:
    return HTTPException(status_code=422, detail=refusal(
        "location.not_found",
        f"{value} is not one of this company's locations. Choose a location from "
        "Settings > Inventory > Locations, or add it there first.",
        value=str(value)))


async def require_company_location(session, company_id, location_id) -> uuid.UUID | None:
    """The location's id when it belongs to the company; None when none is named."""
    if location_id is None or location_id == "":
        return None
    try:
        parsed = location_id if isinstance(location_id, uuid.UUID) else uuid.UUID(str(location_id))
        company = company_id if isinstance(company_id, uuid.UUID) else uuid.UUID(str(company_id))
    except (TypeError, ValueError, AttributeError):
        raise _not_found(location_id)
    found = (await session.execute(
        select(Location.id).where(Location.id == parsed, Location.company_id == company)
    )).scalar_one_or_none()
    if found is None:
        raise _not_found(location_id)
    return parsed


def event_location_references(location_id, data: dict | None) -> list:
    """Every location an event names: its ledger location and the location keys of its
    data, including the new value of a changed location field."""
    data = data or {}
    refs = [location_id, *(data.get(k) for k in LOCATION_REFERENCE_KEYS)]
    changed = data.get("fields_changed")
    if isinstance(changed, dict):
        for key in LOCATION_REFERENCE_KEYS:
            change = changed.get(key)
            if isinstance(change, dict):
                refs.append(change.get("new"))
    seen: list = []
    for ref in refs:
        if ref not in (None, "") and str(ref) not in {str(s) for s in seen}:
            seen.append(ref)
    return seen


async def require_event_locations(session, company_id, location_id, data: dict | None) -> None:
    for ref in event_location_references(location_id, data):
        await require_company_location(session, company_id, ref)
