# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.lot_origin import RETIRED, held_value
from celerp.services.doc_balance import is_awaiting_payment, is_overdue_document, outstanding_balance, today_iso
from celerp.services.auth import get_current_company_id, get_current_user, get_current_role
from celerp.services.permissions import get_current_company_settings
from celerp.services.reorder import is_below_reorder

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("/kpis")
async def get_kpis(company_id=Depends(get_current_company_id), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(Projection).where(Projection.company_id == company_id))).scalars().all()
    items = [r for r in rows if r.entity_type == "item"]
    docs = [r for r in rows if r.entity_type == "doc"]
    mfg = [r for r in rows if r.entity_type == "mfg_order"]
    contacts = [r for r in rows if r.entity_type == "contact"]
    deals = [r for r in rows if r.entity_type == "deal"]
    subscriptions = [r for r in rows if r.entity_type == "doc" and r.state.get("doc_type") in {"subscription_invoice", "subscription_po"}]

    now = today_iso()
    month_prefix = now[:7]  # "YYYY-MM"
    year_prefix = now[:4]   # "YYYY"

    # Receivables and payables: what the invoices and purchase orders awaiting payment still owe,
    # the same documents the invoice list's Awaiting Payment and Overdue cards count.
    def _owed(doc_type: str) -> list[Projection]:
        return [d for d in docs if d.state.get("doc_type") == doc_type and is_awaiting_payment(doc_type, d.state.get("status"))]

    def _balance(rows: list[Projection]) -> float:
        return float(sum(outstanding_balance(d.state) or 0 for d in rows))

    ar_docs = _owed("invoice")
    overdue_docs = [d for d in ar_docs if is_overdue_document(d.state, now)]
    ar_outstanding = _balance(ar_docs)
    ap_outstanding = _balance(_owed("purchase_order"))

    # Inventory: item counts and the retail total come from the canonical valuation
    # endpoint. Inventory value is what the company's stock holds on the books
    # (lot_origin.held_value), archived and expired stock it keeps included, shown only
    # to a role that may see costs.
    from celerp_inventory.routes import ItemListFilters, get_valuation as _get_valuation
    valuation = await _get_valuation(filters=ItemListFilters(), attr_filters=[], company_id=company_id,
                                     role=role, settings=settings, session=session)
    held = [(i, held_value(i)) for i in items]
    total_value_cost = (round(float(sum(v for _, v in held if v is not None)), 2)
                        if "cost_total" in valuation else 0.0)
    total_value_retail = valuation.get("retail_total", 0.0)
    active_item_count_inv = valuation.get("active_item_count", 0)

    # Stock on hand in the catalog, for the per-item KPI sub-fields.
    active_items = [i for i, v in held if v is not None and str(i.state.get("status") or "").lower() not in RETIRED]

    # Revenue: filter to current month / year using issue_date or finalized_at
    def _doc_month(d: "Projection") -> str:
        ts = d.state.get("issue_date") or d.state.get("finalized_at") or ""
        return str(ts)[:7]

    _REVENUE_STATUSES = {"paid", "partial", "final", "awaiting_payment"}
    revenue_docs = [d for d in docs if d.state.get("doc_type") == "invoice" and d.state.get("status") in _REVENUE_STATUSES]
    revenue_mtd = sum(float(d.state.get("total", 0) or 0) for d in revenue_docs if _doc_month(d) == month_prefix)
    revenue_ytd = sum(float(d.state.get("total", 0) or 0) for d in revenue_docs if _doc_month(d).startswith(year_prefix))

    # Revenue trend: invoiced revenue per month for the last 6 months (oldest -> current),
    # for the dashboard line chart. Months with no invoices show as 0 so the axis stays continuous.
    def _month_minus(n: int) -> str:
        y, m = int(year_prefix), int(month_prefix[5:7]) - n
        while m <= 0:
            m += 12
            y -= 1
        return f"{y:04d}-{m:02d}"

    trend_months = [_month_minus(n) for n in range(5, -1, -1)]
    rev_by_month: dict[str, float] = {}
    for d in revenue_docs:
        mk = _doc_month(d)
        rev_by_month[mk] = rev_by_month.get(mk, 0.0) + float(d.state.get("total", 0) or 0)
    revenue_trend = [{"month": mk, "total": round(rev_by_month.get(mk, 0.0), 2)} for mk in trend_months]

    return {
        "inventory": {
            "total_items": active_item_count_inv,
            "total_value_cost": total_value_cost,
            "total_value_retail": total_value_retail,
            "items_expiring_30d": 0,
            "items_on_memo": sum(1 for i in active_items if i.state.get("is_on_memo")),
            "items_reserved": sum(1 for i in active_items if float(i.state.get("reserved_quantity", 0) or 0) > 0),
            "items_in_production": sum(1 for i in active_items if i.state.get("is_in_production")),
            "low_stock_items": sum(1 for i in active_items if is_below_reorder(i.state)),
        },
        "sales": {
            "revenue_mtd": revenue_mtd,
            "revenue_ytd": revenue_ytd,
            "revenue_trend": revenue_trend,
            "invoices_outstanding": len(ar_docs),
            "invoices_overdue": len(overdue_docs),
            "ar_outstanding": ar_outstanding,
            "ar_overdue": _balance(overdue_docs),
        },
        "purchasing": {
            "spend_mtd": sum(float(d.state.get("total", 0) or 0) for d in docs if d.state.get("doc_type") == "purchase_order" and _doc_month(d) == month_prefix),
            "pending_pos": sum(1 for d in docs if d.state.get("doc_type") == "purchase_order" and d.state.get("status") not in {"received", "void"}),
            "ap_outstanding": ap_outstanding,
        },
        "manufacturing": {
            "orders_in_progress": sum(1 for o in mfg if o.state.get("status") == "in_progress"),
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
async def get_activity(limit: int = Query(default=15, le=100), company_id=Depends(get_current_company_id), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(LedgerEntry).where(LedgerEntry.company_id == company_id).order_by(LedgerEntry.id.desc()).limit(limit))).scalars().all()
    return {"activities": await _hydrate_entries(rows, company_id, session, settings, role)}


@router.get("/activity/search")
async def search_activity(
    q: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import and_, func
    import sqlalchemy as _sa

    stmt = select(LedgerEntry).where(LedgerEntry.company_id == company_id)

    if date_from:
        try:
            dt_from = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            stmt = stmt.where(LedgerEntry.ts >= dt_from)
        except ValueError:
            pass
    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
            # Advance by one day so a date-only value includes the full chosen day.
            if "T" not in date_to and " " not in date_to:
                dt_to += timedelta(days=1)
            stmt = stmt.where(LedgerEntry.ts < dt_to)
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
        "activities": await _hydrate_entries(rows, company_id, session, settings, role),
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


async def _hydrate_entries(rows, company_id: str, session, settings: dict | None = None, role: str | None = None) -> list[dict]:
    """Batch-resolve entity names and actor names for a list of LedgerEntry rows."""
    from celerp.services.activity_redaction import can_see_costs, redact_event_costs
    from celerp.services.ledger_display import display_fields, entry_ts
    show_costs = can_see_costs(settings, role)

    activities = []
    for e, shown in zip(rows, await display_fields(rows, company_id, session)):
        data = e.data if isinstance(e.data, dict) else {}
        if not show_costs:
            data = redact_event_costs(e.event_type, data)
        activities.append({
            "ts": entry_ts(e, settings),
            "event_type": e.event_type,
            "entity_id": e.entity_id,
            "entity_type": e.entity_type,
            "name": shown["name"] or None,
            "entity_doc_type": shown["doc_type"],
            **{k: v for k, v in shown.items() if k.startswith("actor_")},
            "data": data,
            "metadata_": e.metadata_ if isinstance(e.metadata_, dict) else {},
        })
    return activities
