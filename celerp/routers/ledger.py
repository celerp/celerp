# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.ledger import LedgerEntry
from celerp.projections.engine import ProjectionEngine
from celerp.services.auth import get_current_company_id, get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("")
async def list_entries(
    entity_id: str | None = None,
    entity_type: str | None = None,
    event_type: str | None = None,
    resolve: bool = False,
    limit: int = 100,
    offset: int = 0,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    q = select(LedgerEntry).where(LedgerEntry.company_id == company_id).order_by(LedgerEntry.id.desc())
    count_q = select(func.count(LedgerEntry.id)).where(LedgerEntry.company_id == company_id)
    if entity_id:
        q = q.where(LedgerEntry.entity_id == entity_id)
        count_q = count_q.where(LedgerEntry.entity_id == entity_id)
    if entity_type:
        q = q.where(LedgerEntry.entity_type == entity_type)
        count_q = count_q.where(LedgerEntry.entity_type == entity_type)
    if event_type:
        q = q.where(LedgerEntry.event_type == event_type)
        count_q = count_q.where(LedgerEntry.event_type == event_type)

    total = (await session.execute(count_q)).scalar() or 0
    rows = (await session.execute(q.offset(offset).limit(limit))).scalars().all()

    # Optionally resolve entity names and actor names
    name_map: dict[str, str] = {}
    actor_map: dict[str, str] = {}
    if resolve and rows:
        eids = list({r.entity_id for r in rows})
        from celerp.models.projections import Projection
        proj_rows = (await session.execute(
            select(Projection.entity_id, Projection.state).where(
                Projection.company_id == company_id, Projection.entity_id.in_(eids),
            )
        )).all()
        for eid, state in proj_rows:
            name_map[eid] = (
                state.get("name") or state.get("sku") or state.get("doc_number") or state.get("title") or ""
            )
        actor_ids = list({r.actor_id for r in rows if r.actor_id})
        if actor_ids:
            from celerp.models.company import User
            user_rows = (await session.execute(
                select(User.id, User.name).where(User.id.in_(actor_ids))
            )).all()
            actor_map = {str(uid): uname for uid, uname in user_rows}

    return {"items": [
        {
            "id": r.id,
            "entity_id": r.entity_id,
            "entity_type": r.entity_type,
            "event_type": r.event_type,
            "data": r.data,
            "metadata": r.metadata_ or {},
            "ts": r.ts.isoformat() if hasattr(r.ts, "isoformat") else str(r.ts),
            **({"entity_name": name_map.get(r.entity_id, "")} if resolve else {}),
            **({"actor_name": actor_map.get(str(r.actor_id), str(r.actor_id) if r.actor_id else "")} if resolve else {}),
        }
        for r in rows
    ], "total": total}


@router.get("/{entry_id}")
async def get_entry(entry_id: int, company_id: str = Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await session.get(LedgerEntry, entry_id)
    if entry is None or entry.company_id != company_id:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "id": entry.id,
        "entity_id": entry.entity_id,
        "entity_type": entry.entity_type,
        "event_type": entry.event_type,
        "data": entry.data,
        "metadata": entry.metadata_ or {},
        "ts": str(entry.ts),
    }


@router.post("/rebuild")
async def rebuild(company_id: str = Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    await ProjectionEngine.rebuild(session, company_id=company_id)
    await session.commit()
    return {"ok": True}
