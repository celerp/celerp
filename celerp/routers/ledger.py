# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, and_, cast, false, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_lifecycle_session_ctx, get_session
from celerp.models.ledger import LedgerEntry
from celerp.projections.engine import ProjectionEngine
from celerp.services.activity_redaction import (
    can_read_event, fields_for_role, readable_events, serialize_event,
)
from celerp.services.ledger_display import actor_choices, display_fields, shown_in_history
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


def _named(values: list[str]) -> list[str]:
    """The filter values that can name stored text: the database never stores a NUL byte in
    text, so a value carrying one names nothing."""
    return [v for v in values if "\x00" not in v]


def _scope(stmt: Select, company_id, settings, role, entity_id=None, entity_type=None,
           event_type=None, field=None, who=None, history=False) -> Select:
    """``stmt`` narrowed to the company, the events this role can read and the given filters,
    and with ``history`` to the events History shows; a cost field is no filter for a role that
    cannot see costs."""
    stmt = readable_events(stmt.where(LedgerEntry.company_id == company_id), settings, role)
    if history:
        stmt = stmt.where(shown_in_history(next(iter(_named([entity_id])), None) if entity_id else None))
    if entity_id:
        stmt = stmt.where(LedgerEntry.entity_id.in_(_named([entity_id])))
    if entity_type:
        stmt = stmt.where(LedgerEntry.entity_type.in_(_named([entity_type])))
    if event_type:
        stmt = stmt.where(LedgerEntry.event_type.in_(_named(event_type)))
    if field:
        visible = _named(fields_for_role(field, settings, role))
        stmt = stmt.where(_changes_any(visible) if visible else false())
    if who:
        keys = _named(who)
        stmt = stmt.where(or_(*(_who_matches(k) for k in keys)) if keys else false())
    return stmt


@router.get("")
async def list_entries(
    entity_id: str | None = None,
    entity_type: str | None = None,
    event_type: list[str] | None = Query(None),
    field: list[str] | None = Query(None),
    who: list[str] | None = Query(None),
    resolve: bool = False,
    history: bool = False,
    limit: int = Query(100, ge=1, le=MAX_PAGE),
    before: int | None = None,
    after: int | None = None,
    offset: int | None = Query(None, ge=0),
    company_id: str = Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One page of ledger entries, newest first, read from a stable event-id cursor: the page
    just older than ``before``, or just newer than ``after``, or the newest; ``offset`` skips that
    many of the newest instead, and cannot be combined with a cursor. ``total`` counts every
    entry the filters match, ``newer`` those newer than the page, ``has_older`` whether any is older.
    With ``history`` only the events History shows are read and counted."""
    filters = (company_id, settings, role, entity_id, entity_type, event_type, field, who, history)
    if offset is not None and (before is not None or after is not None):
        raise HTTPException(status_code=422, detail="offset cannot be combined with before or after")
    q = _scope(select(LedgerEntry), *filters)
    if after is not None:
        rows = list(reversed((await session.execute(
            q.where(LedgerEntry.id > after).order_by(LedgerEntry.id.asc()).limit(limit))).scalars().all()))
    else:
        if before is not None:
            q = q.where(LedgerEntry.id < before)
        rows = (await session.execute(
            q.order_by(LedgerEntry.id.desc()).offset(offset or 0).limit(limit))).scalars().all()
    count = select(func.count(LedgerEntry.id))
    total = (await session.execute(_scope(count, *filters))).scalar() or 0
    newer = (await session.execute(_scope(count.where(LedgerEntry.id > rows[0].id), *filters))).scalar() if rows else 0
    has_older = bool(rows) and (await session.execute(
        _scope(select(LedgerEntry.id), *filters).where(LedgerEntry.id < rows[-1].id).limit(1))).first() is not None

    shown = await display_fields(rows, company_id, session) if resolve and rows else [{} for _ in rows]
    items = [
        {
            **serialize_event(r, settings, role),
            **({
                "entity_name": f["name"],
                "entity_doc_type": f["doc_type"],
                **{k: v for k, v in f.items() if k.startswith("actor_")},
            } if resolve else {}),
        }
        for r, f in zip(rows, shown)
    ]
    return {"items": items, "total": total,
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
    if not entity_id.strip():
        raise HTTPException(status_code=422, detail="entity_id must name a record")
    filters = (company_id, settings, role, entity_id)
    event_types = (await session.execute(_scope(select(LedgerEntry.event_type).distinct(), *filters, history=True))).scalars().all()
    changed = LedgerEntry.data["fields_changed"]
    fields = (await session.execute(_scope(select(func.json_object_keys(changed)).where(
        func.json_typeof(changed) == "object"), *filters, history=True).distinct())).scalars().all()
    fields = fields_for_role(fields, settings, role)
    backup = LedgerEntry.metadata_["backup_actor"]["name"].as_string()
    makers = (await session.execute(_scope(select(
        LedgerEntry.actor_id, _automatic(), backup), *filters, history=True).distinct())).all()
    actors = await actor_choices(makers, session)
    return {"event_types": sorted(event_types), "fields": sorted(fields), "actors": actors}


@router.get("/{entry_id}")
async def get_entry(entry_id: int, company_id: str = Depends(get_current_company_id), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await session.get(LedgerEntry, entry_id)
    if entry is None or entry.company_id != company_id or not can_read_event(entry.event_type, settings, role):
        raise HTTPException(status_code=404, detail=t("error.record_not_found"))
    return serialize_event(entry, settings, role)


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
