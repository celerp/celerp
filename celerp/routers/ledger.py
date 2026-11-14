# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, cast, false, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_lifecycle_session_ctx, get_session
from celerp.models.ledger import LedgerEntry
from celerp.projections.engine import ProjectionEngine
from celerp.services.activity_redaction import (
    COST_DATA_KEYS, can_read_event, can_see_costs, readable_events, redact_entries_for_role, redact_event_costs,
)
from celerp.services.ledger_display import actor_choices, display_fields, entry_ts
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.company_lock import lock_company
from ui.i18n import t
from celerp.services.permissions import carry_authority, get_current_company_settings, require_permission

router = APIRouter(dependencies=[Depends(get_current_user)])


# The most events one read returns: a record's history is read a page at a time.
MAX_PAGE = 500


def _automatic():
    return func.coalesce(LedgerEntry.metadata_["automatic"].as_string(), "") == "true"


def _who_matches(key: str):
    """Entries made by the actor a History's Who filter names, in the terms ``actor_key`` gives:
    "auto" for automatic work, a user id, or "h:<name>" for a name carried in from a backup."""
    if key == "auto":
        return _automatic()
    if key.startswith("h:"):
        return and_(LedgerEntry.actor_id.is_(None), ~_automatic(),
                    LedgerEntry.metadata_["backup_actor"]["name"].as_string() == key[2:])
    try:
        return and_(LedgerEntry.actor_id == uuid.UUID(key), ~_automatic())
    except ValueError:
        return false()


def _changes_any(fields: list[str]):
    """Entries whose recorded changes include any of ``fields``."""
    changed = LedgerEntry.data["fields_changed"]
    return and_(func.json_typeof(changed) == "object", cast(changed, JSONB).has_any(array(fields)))


def _scope(stmt, company_id, settings, role, entity_id=None, entity_type=None,
           event_type=None, field=None, who=None):
    """``stmt`` narrowed to the company, the events this role can read and the given filters;
    a cost field is no filter for a role that cannot see costs."""
    stmt = readable_events(stmt.where(LedgerEntry.company_id == company_id), settings, role)
    if entity_id:
        stmt = stmt.where(LedgerEntry.entity_id == entity_id)
    if entity_type:
        stmt = stmt.where(LedgerEntry.entity_type == entity_type)
    if event_type:
        stmt = stmt.where(LedgerEntry.event_type.in_(event_type))
    if field:
        visible = field if can_see_costs(settings, role) else [f for f in field if f not in COST_DATA_KEYS]
        stmt = stmt.where(_changes_any(visible) if visible else false())
    if who:
        stmt = stmt.where(or_(*(_who_matches(k) for k in who)))
    return stmt


@router.get("")
async def list_entries(
    entity_id: str | None = None,
    entity_type: str | None = None,
    event_type: list[str] | None = Query(None),
    field: list[str] | None = Query(None),
    who: list[str] | None = Query(None),
    resolve: bool = False,
    limit: int = Query(100, ge=1, le=MAX_PAGE),
    before: int | None = None,
    after: int | None = None,
    company_id: str = Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One page of ledger entries, newest first, read from a stable event-id cursor: the page
    just older than ``before``, or just newer than ``after``, or the newest. ``total`` counts every
    entry the filters match, ``newer`` those newer than the page, ``has_older`` whether any is older."""
    scope = lambda stmt: _scope(stmt, company_id, settings, role, entity_id, entity_type,  # noqa: E731
                                event_type, field, who)
    q = scope(select(LedgerEntry))
    if after is not None:
        rows = list(reversed((await session.execute(
            q.where(LedgerEntry.id > after).order_by(LedgerEntry.id.asc()).limit(limit))).scalars().all()))
    else:
        if before is not None:
            q = q.where(LedgerEntry.id < before)
        rows = (await session.execute(q.order_by(LedgerEntry.id.desc()).limit(limit))).scalars().all()
    count = lambda *where: scope(select(func.count(LedgerEntry.id)).where(*where))  # noqa: E731
    total = (await session.execute(count())).scalar() or 0
    newer = (await session.execute(count(LedgerEntry.id > rows[0].id))).scalar() if rows else 0
    has_older = bool(rows) and (await session.execute(
        scope(select(LedgerEntry.id)).where(LedgerEntry.id < rows[-1].id).limit(1))).first() is not None

    shown = await display_fields(rows, company_id, session) if resolve and rows else [{} for _ in rows]
    items = [
        {
            "id": r.id,
            "entity_id": r.entity_id,
            "entity_type": r.entity_type,
            "event_type": r.event_type,
            "data": r.data,
            "metadata": r.metadata_ or {},
            "ts": entry_ts(r, settings),
            **({
                "entity_name": f["name"],
                "entity_doc_type": f["doc_type"],
                **{k: v for k, v in f.items() if k.startswith("actor_")},
            } if resolve else {}),
        }
        for r, f in zip(rows, shown)
    ]
    # Fail-closed cost redaction: never ship cost amounts to under-manager roles.
    return {"items": redact_entries_for_role(items, settings, role), "total": total,
            "newer": newer, "has_older": has_older}


@router.get("/facets")
async def history_facets(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What a record's History filters can offer, over its whole history: the event types, the
    fields its events changed, and who made them (``actor_key`` is the Who filter's value)."""
    scope = lambda stmt: _scope(stmt, company_id, settings, role, entity_id)  # noqa: E731
    event_types = (await session.execute(scope(select(LedgerEntry.event_type).distinct()))).scalars().all()
    changed = LedgerEntry.data["fields_changed"]
    fields = (await session.execute(scope(select(func.json_object_keys(changed)).where(
        func.json_typeof(changed) == "object")).distinct())).scalars().all()
    if not can_see_costs(settings, role):
        fields = [f for f in fields if f not in COST_DATA_KEYS]
    backup = LedgerEntry.metadata_["backup_actor"]["name"].as_string()
    makers = (await session.execute(scope(select(
        LedgerEntry.actor_id, _automatic(), backup)).distinct())).all()
    actors = await actor_choices(makers, session)
    return {"event_types": sorted(event_types), "fields": sorted(fields), "actors": actors}


@router.get("/{entry_id}")
async def get_entry(entry_id: int, company_id: str = Depends(get_current_company_id), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await session.get(LedgerEntry, entry_id)
    if entry is None or entry.company_id != company_id or not can_read_event(entry.event_type, settings, role):
        raise HTTPException(status_code=404, detail=t("error.record_not_found"))
    data = entry.data if can_see_costs(settings, role) else redact_event_costs(entry.event_type, entry.data or {})
    return {
        "id": entry.id,
        "entity_id": entry.entity_id,
        "entity_type": entry.entity_type,
        "event_type": entry.event_type,
        "data": data,
        "metadata": entry.metadata_ or {},
        "ts": entry_ts(entry, settings),
    }


@router.post("/rebuild")
async def rebuild(
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("manage_company_lifecycle"),
    request_session: AsyncSession = Depends(get_session),
) -> dict:
    # A full rebuild deletes every projection for the company and replays the
    # whole ledger, which can exceed the request statement_timeout on a mature
    # database. Run it on the unbounded lifecycle session so recovery is never
    # cancelled mid-replay, leaving projections half-rebuilt. That unbounded work
    # is gated on the owner-only company-lifecycle permission so an ordinary
    # subuser cannot open concurrent full replays outside the request pool budget.
    async with get_lifecycle_session_ctx() as session:
        carry_authority(request_session, session)
        await lock_company(session, company_id)
        await ProjectionEngine.rebuild(session, company_id=company_id)
        await session.commit()
    return {"ok": True}
