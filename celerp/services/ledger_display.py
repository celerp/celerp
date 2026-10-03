# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Presentation fields for ledger entries, shared by every activity feed.

One place decides how an entry reads to a person: the time in the company's
business timezone, the record's name and document type, and who made the change
(a user of this installation, or the name a company backup carried for it).
Stored entries are never changed.
"""

from __future__ import annotations

from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.business_time import business_timezone


def _zone(settings: dict | None):
    try:
        return business_timezone((settings or {}).get("timezone"))
    except ValueError:
        return business_timezone(None)


def entry_ts(entry, settings: dict | None) -> str:
    """The entry's instant as ISO text in the company's business timezone."""
    ts = entry.ts
    if not hasattr(ts, "isoformat"):
        return str(ts)
    if ts.tzinfo is None:
        return ts.isoformat()
    return ts.astimezone(_zone(settings)).isoformat()


def _actor(entry, actor_map: dict[str, str]) -> dict:
    if entry.actor_id and str(entry.actor_id) in actor_map:
        return {"actor_name": actor_map[str(entry.actor_id)]}
    carried = ((entry.metadata_ or {}).get("backup_actor") or {}) if isinstance(entry.metadata_, dict) else {}
    if not entry.actor_id and isinstance(carried, dict) and carried.get("name"):
        return {"actor_name": str(carried["name"]), "actor_historical": True}
    return {"actor_name": str(entry.actor_id) if entry.actor_id else ""}


async def display_fields(rows, company_id, session) -> list[dict]:
    """Per row: the record's name and document type, and who made the change."""
    entity_ids = list({r.entity_id for r in rows})
    states: dict[str, dict] = {}
    if entity_ids:
        for eid, state in (await session.execute(
            select(Projection.entity_id, Projection.state).where(
                Projection.company_id == company_id, Projection.entity_id.in_(entity_ids),
            )
        )).all():
            states[eid] = state or {}
    actor_ids = list({r.actor_id for r in rows if r.actor_id})
    actor_map: dict[str, str] = {}
    if actor_ids:
        from celerp.models.company import User
        actor_map = {
            str(uid): uname for uid, uname in (await session.execute(
                select(User.id, User.name).where(User.id.in_(actor_ids))
            )).all()
        }
    fields = []
    for r in rows:
        state = states.get(r.entity_id, {})
        fields.append({
            "name": state.get("name") or state.get("sku") or state.get("doc_number") or state.get("title") or "",
            "doc_type": state.get("doc_type") or "",
            **_actor(r, actor_map),
        })
    return fields
