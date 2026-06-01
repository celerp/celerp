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
    subscriptions = [r for r in rows if r.entity_type == "doc" and r.state.get("doc_type") in {"subscription_invoice", "subscription_po"}]

    now = datetime.now(UTC).date().isoformat()
    month_prefix = now[:7]  # "YYYY-MM"
    year_prefix = now[:4]   # "YYYY"

    # AR: only non-void, non-draft invoices count as outstanding.
    # Exclude pro-forma (draft/sent pre-approval), void, and fully-paid with no balance.
    _AR_STATUSES = {"final", "sent", "awaiting_payment", "partial", "paid"}
    ar_docs = [d for d in docs if d.state.get("doc_type") == "invoice" and d.state.get("status") in _AR_STATUSES]
    ar_outstanding = sum(float(d.state.get("amount_outstanding", 0) or 0) for d in ar_docs)
    ap_outstanding = sum(float(d.state.get("amount_outstanding", d.state.get("total", 0)) or 0) for d in docs if d.state.get("doc_type") == "purchase_order" and d.state.get("status") not in {"void", "draft"})

    # Inventory: delegate entirely to the canonical valuation endpoint.
    # This is the single source of truth for item counts and price totals.
    from celerp_inventory.routes import get_valuation as _get_valuation
    valuation = await _get_valuation(company_id=company_id, session=session)
    total_value_cost = valuation["cost_total"]
    total_value_retail = valuation["retail_total"]
    active_item_count_inv = valuation["active_item_count"]

    # For KPI sub-fields that need per-item access, re-use already-loaded items list.
    _CONSIGNMENT_IN = "in"
    _INACTIVE_STATUSES = frozenset({"archived", "deleted", "void", "sold", "fulfilled", "merged", "expired"})
    active_items = []
    for i in items:
        s = i.state
        if s.get("consignment_flag") == _CONSIGNMENT_IN or i.consignment_flag == _CONSIGNMENT_IN:
            continue
        if (s.get("inventory_type") or "stocked") != "stocked":
            continue
        status = str(s.get("status") or "").lower()
        if status in _INACTIVE_STATUSES:
            continue
        active_items.append(i)

    # Revenue: filter to current month / year using issue_date or finalized_at
    def _doc_month(d: "Projection") -> str:
        ts = d.state.get("issue_date") or d.state.get("finalized_at") or ""
        return str(ts)[:7]

    _REVENUE_STATUSES = {"paid", "partial", "final", "awaiting_payment"}
    revenue_docs = [d for d in docs if d.state.get("doc_type") == "invoice" and d.state.get("status") in _REVENUE_STATUSES]
    revenue_mtd = sum(float(d.state.get("total", 0) or 0) for d in revenue_docs if _doc_month(d) == month_prefix)
    revenue_ytd = sum(float(d.state.get("total", 0) or 0) for d in revenue_docs if _doc_month(d).startswith(year_prefix))

    return {
        "inventory": {
            "total_items": active_item_count_inv,
            "total_value_cost": total_value_cost,
            "total_value_retail": total_value_retail,
            "items_expiring_30d": 0,
            "items_on_memo": sum(1 for i in active_items if i.state.get("is_on_memo")),
            "items_reserved": sum(1 for i in active_items if float(i.state.get("reserved_quantity", 0) or 0) > 0),
            "items_in_production": sum(1 for i in active_items if i.state.get("is_in_production")),
            "low_stock_items": sum(1 for i in active_items if float(i.state.get("quantity", 0) or 0) <= 0),
        },
        "sales": {
            "revenue_mtd": revenue_mtd,
            "revenue_ytd": revenue_ytd,
            "invoices_outstanding": sum(1 for d in ar_docs if float(d.state.get("amount_outstanding", 0) or 0) > 0),
            "ar_outstanding": ar_outstanding,
            "ar_overdue": sum(float(d.state.get("amount_outstanding", 0) or 0) for d in ar_docs if d.state.get("due_date") and d.state.get("due_date") < now and float(d.state.get("amount_outstanding", 0) or 0) > 0),
        },
        "purchasing": {
            "spend_mtd": sum(float(d.state.get("total", 0) or 0) for d in docs if d.state.get("doc_type") == "purchase_order" and _doc_month(d) == month_prefix),
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
    return {"activities": await _hydrate_entries(rows, company_id, session)}


@router.get("/activity/search")
async def search_activity(
    q: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import and_, func
    import sqlalchemy as _sa

    stmt = select(LedgerEntry).where(LedgerEntry.company_id == company_id)

    if date_from:
        try:
            dt_from = datetime.fromisoformat(date_from).replace(tzinfo=UTC)
            stmt = stmt.where(LedgerEntry.ts >= dt_from)
        except ValueError:
            pass
    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to).replace(tzinfo=UTC)
            stmt = stmt.where(LedgerEntry.ts <= dt_to)
        except ValueError:
            pass
    if q:
        ql = f"%{q.lower()}%"
        stmt = stmt.where(
            _sa.or_(
                _sa.func.lower(LedgerEntry.event_type).like(ql),
                _sa.func.lower(LedgerEntry.entity_id).like(ql),
                _sa.cast(LedgerEntry.data, _sa.Text).ilike(ql),
            )
        )

    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(
        stmt.order_by(LedgerEntry.id.desc()).offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()

    return {
        "activities": await _hydrate_entries(rows, company_id, session),
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


async def _hydrate_entries(rows, company_id: str, session) -> list[dict]:
    """Batch-resolve entity names and actor names for a list of LedgerEntry rows."""
    entity_ids = list({e.entity_id for e in rows})
    proj_rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_id.in_(entity_ids))
    )).scalars().all() if entity_ids else []
    proj_by_id = {p.entity_id: p.state for p in proj_rows}

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
        name = state.get("name") or state.get("sku") or state.get("doc_number") or state.get("title") or None
        activities.append({
            "ts": e.ts.isoformat() if hasattr(e.ts, "isoformat") else str(e.ts),
            "event_type": e.event_type,
            "entity_id": e.entity_id,
            "entity_type": e.entity_type,
            "name": name,
            "actor_name": actor_map.get(str(e.actor_id), str(e.actor_id) if e.actor_id else ""),
            "data": e.data if isinstance(e.data, dict) else {},
        })
    return activities
