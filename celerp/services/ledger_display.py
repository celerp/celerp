# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Presentation fields for ledger entries, shared by every activity feed.

One place decides how an entry reads to a person: the time in the company's
business timezone, the record's name and document type, and who made the change
(a user of this installation, the name a company backup carried for it, or the app itself
for work nobody started by hand).
Stored entries are never changed.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.business_time import business_timezone


def run_reference(order_id: str) -> str:
    """A production run as people see it: WO- and the first eight characters of its id."""
    return "WO-" + str(order_id).split(":")[-1][:8]


_RUN_ID = re.compile(r"mfg:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def readable_memo(memo: str) -> str:
    """A journal memo as people read it: a production run named by its internal id, as
    older releases posted it, reads as the run's reference. The stored memo is unchanged."""
    return _RUN_ID.sub(lambda m: run_reference(m.group(0)), memo)


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


def _maker(actor_id, automatic: bool, backup_name, actor_map: dict[str, str]) -> dict:
    """Who made an entry, for display, and ``actor_key``: the same maker as the History's Who
    filter names it ("auto", a user id, or "h:<name>" for a name a company backup carried)."""
    if automatic:
        return {"actor_name": actor_map.get(str(actor_id), ""), "actor_automatic": True, "actor_key": "auto"}
    if actor_id and str(actor_id) in actor_map:
        return {"actor_name": actor_map[str(actor_id)], "actor_key": str(actor_id)}
    if not actor_id and backup_name:
        return {"actor_name": str(backup_name), "actor_historical": True, "actor_key": f"h:{backup_name}"}
    return {"actor_name": str(actor_id) if actor_id else "", "actor_key": str(actor_id) if actor_id else ""}


def _actor(entry, actor_map: dict[str, str]) -> dict:
    metadata = entry.metadata_ if isinstance(entry.metadata_, dict) else {}
    carried = metadata.get("backup_actor")
    backup_name = carried.get("name") if isinstance(carried, dict) else None
    return _maker(entry.actor_id, bool(metadata.get("automatic")), backup_name, actor_map)


async def _actor_names(actor_ids, session) -> dict[str, str]:
    if not actor_ids:
        return {}
    from celerp.models.company import User
    return {str(uid): uname for uid, uname in (await session.execute(
        select(User.id, User.name).where(User.id.in_(list(actor_ids))))).all()}


async def actor_choices(makers, session) -> list[dict]:
    """Every distinct maker of (actor id, automatic, backup name), as ``display_fields`` shows it."""
    names = await _actor_names({a for a, _, _ in makers if a}, session)
    found = {}
    for actor_id, automatic, backup_name in makers:
        maker = _maker(actor_id, bool(automatic), backup_name, names)
        found.setdefault(maker["actor_key"], maker)
    return [found[k] for k in sorted(found)]


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
    actor_map = await _actor_names({r.actor_id for r in rows if r.actor_id}, session)
    fields = []
    for r in rows:
        state = states.get(r.entity_id, {})
        fields.append({
            "name": state.get("name") or state.get("sku") or state.get("doc_number") or state.get("title") or "",
            "doc_type": state.get("doc_type") or "",
            **_actor(r, actor_map),
        })
    return fields
