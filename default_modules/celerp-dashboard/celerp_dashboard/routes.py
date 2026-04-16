# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("/kpis")
async def get_kpis(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(Projection).where(Projection.company_id == company_id))).scalars().all()
    items = [r for r in rows if r.entity_type == "item"]
    docs = [r for r in rows if r.entity_type == "doc"]
    mfg = [r for r in rows if r.entity_type == "mfg_order"]
    contacts = [r for r in rows if r.entity_type == "contact"]
    deals = [r for r in rows if r.entity_type == "deal"]
    subscriptions = [r for r in rows if r.entity_type == "subscription"]

    now = datetime.now(UTC).date().isoformat()
    ar_outstanding = sum(float(d.state.get("amount_outstanding", 0) or 0) for d in docs if d.state.get("doc_type") == "invoice")
    ap_outstanding = sum(float(d.state.get("amount_outstanding", d.state.get("total", 0)) or 0) for d in docs if d.state.get("doc_type") == "purchase_order")
    return {
        "inventory": {
            "total_items": len(items),
            "total_value_cost": sum(float(i.state.get("total_cost", 0) or 0) for i in items),
            "total_value_retail": sum(float(i.state.get("retail_price", 0) or 0) for i in items),
            "items_expiring_30d": 0,
            "items_on_memo": sum(1 for i in items if i.state.get("is_on_memo")),
            "items_reserved": sum(1 for i in items if float(i.state.get("reserved_quantity", 0) or 0) > 0),
            "items_in_production": sum(1 for i in items if i.state.get("is_in_production")),
            "low_stock_items": sum(1 for i in items if float(i.state.get("quantity", 0) or 0) <= 0),
        },
        "sales": {
            "revenue_mtd": sum(float(d.state.get("total", 0) or 0) for d in docs if d.state.get("doc_type") == "invoice" and d.state.get("status") in {"paid", "partial", "final"}),
            "revenue_ytd": sum(float(d.state.get("total", 0) or 0) for d in docs if d.state.get("doc_type") == "invoice"),
            "invoices_outstanding": sum(1 for d in docs if d.state.get("doc_type") == "invoice" and float(d.state.get("amount_outstanding", 0) or 0) > 0),
            "ar_outstanding": ar_outstanding,
            "ar_overdue": sum(float(d.state.get("amount_outstanding", 0) or 0) for d in docs if d.state.get("doc_type") == "invoice" and d.state.get("due_date") and d.state.get("due_date") < now and float(d.state.get("amount_outstanding", 0) or 0) > 0),
        },
        "purchasing": {
            "spend_mtd": sum(float(d.state.get("total", 0) or 0) for d in docs if d.state.get("doc_type") == "purchase_order"),
            "pending_pos": sum(1 for d in docs if d.state.get("doc_type") == "purchase_order" and d.state.get("status") not in {"received", "void"}),
            "ap_outstanding": ap_outstanding,
        },
        "manufacturing": {
            "orders_in_progress": sum(1 for o in mfg if o.state.get("status") == "started"),
            "orders_completed_mtd": sum(1 for o in mfg if o.state.get("status") == "completed"),
            "orders_overdue": sum(1 for o in mfg if o.state.get("due_date") and o.state.get("due_date") < now and o.state.get("status") != "completed"),
        },
        "crm": {
            "total_contacts": len(contacts),
            "active_deals": sum(1 for d in deals if d.state.get("status") not in {"won", "lost"}),
            "deals_won_mtd": sum(1 for d in deals if d.state.get("status") == "won"),
            "deal_value_pipeline": sum(float(d.state.get("value", 0) or 0) for d in deals if d.state.get("status") not in {"won", "lost"}),
        },
        "subscriptions": {
            "active_count": sum(1 for s in subscriptions if s.state.get("status") == "active"),
        },
    }


@router.get("/activity")
async def get_activity(limit: int = Query(default=15, le=100), company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(LedgerEntry).where(LedgerEntry.company_id == company_id).order_by(LedgerEntry.id.desc()).limit(limit))).scalars().all()

    # Batch-load projections to resolve human-readable names
    entity_ids = list({e.entity_id for e in rows})
    proj_rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_id.in_(entity_ids))
    )).scalars().all()
    proj_by_id = {p.entity_id: p.state for p in proj_rows}

    # Resolve actor names
    actor_ids = list({e.actor_id for e in rows if e.actor_id})
    actor_map: dict[str, str] = {}
    if actor_ids:
        from celerp.models.company import User
        user_rows = (await session.execute(
            select(User.id, User.name).where(User.id.in_(actor_ids))
        )).all()
        actor_map = {str(uid): uname for uid, uname in user_rows}

    activities = []
    for e in rows:
        state = proj_by_id.get(e.entity_id, {})
        name = (
            state.get("name") or
            state.get("sku") or
            state.get("doc_number") or
            state.get("title") or
            None
        )
        activities.append({
            "ts": e.ts.isoformat() if hasattr(e.ts, "isoformat") else str(e.ts),
            "event_type": e.event_type,
            "entity_id": e.entity_id,
            "entity_type": e.entity_type,
            "name": name,
            "actor_name": actor_map.get(str(e.actor_id), str(e.actor_id) if e.actor_id else ""),
        })
    return {"activities": activities}
