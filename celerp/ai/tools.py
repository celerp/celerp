# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""AI tools - ERP-aware callable functions for the AI assistant.

Tools are pure functions that query the database and return structured data.
The AI service calls these to answer queries about inventory, sales, etc.
Each tool takes (session, company_id) plus optional parameters.

Tool registry: TOOLS dict maps tool_name -> ToolDef.
Callers invoke execute_tool(name, params, session, company_id).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection


@dataclass
class ToolDef:
    name: str
    description: str
    params: list[str]
    fn: Callable[..., Awaitable[dict[str, Any]]]


# -- Shared query helper ----------------------------------------------------

async def _projections(
    session: AsyncSession,
    company_id: uuid.UUID,
    entity_type: str,
) -> list[Projection]:
    """Fetch all projections for a company + entity type."""
    return list(
        (await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == entity_type,
            )
        )).scalars().all()
    )


# -- Tool implementations --------------------------------------------------

async def _dashboard_kpis(session: AsyncSession, company_id: uuid.UUID, **_) -> dict:
    """Return high-level business KPIs."""
    items = await _projections(session, company_id, "item")
    docs = await _projections(session, company_id, "doc")
    deals = await _projections(session, company_id, "deal")

    return {
        "total_items": len(items),
        "inventory_value": round(sum(float(i.state.get("total_cost", 0) or 0) for i in items), 2),
        "low_stock_items": sum(1 for i in items if float(i.state.get("quantity", 0) or 0) <= 0),
        "ar_outstanding": round(
            sum(
                float(d.state.get("amount_outstanding", 0) or 0)
                for d in docs if d.state.get("doc_type") == "invoice"
            ), 2
        ),
        "active_deals": sum(1 for d in deals if d.state.get("status") not in {"won", "lost"}),
    }


async def _low_stock_items(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 20, **_,
) -> dict:
    """List items with zero or negative quantity."""
    items = await _projections(session, company_id, "item")
    low = sorted(
        [
            {"sku": r.state.get("sku"), "name": r.state.get("name"), "quantity": r.state.get("quantity", 0)}
            for r in items if float(r.state.get("quantity", 0) or 0) <= 0
        ],
        key=lambda x: float(x["quantity"] or 0),
    )
    return {"items": low[:int(limit)], "total_count": len(low)}


async def _outstanding_invoices(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 20, **_,
) -> dict:
    """List unpaid invoices with outstanding balances."""
    docs = await _projections(session, company_id, "doc")
    invoices = sorted(
        [
            {
                "doc_number": r.state.get("doc_number"),
                "contact_name": r.state.get("contact_name"),
                "amount_outstanding": r.state.get("amount_outstanding", 0),
                "due_date": r.state.get("due_date"),
                "status": r.state.get("status"),
            }
            for r in docs
            if r.state.get("doc_type") == "invoice"
            and float(r.state.get("amount_outstanding", 0) or 0) > 0
        ],
        key=lambda x: float(x["amount_outstanding"] or 0),
        reverse=True,
    )
    return {"invoices": invoices[:int(limit)], "total_count": len(invoices)}


async def _top_items_by_value(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 10, **_,
) -> dict:
    """List items ranked by total inventory value (qty x cost)."""
    items = await _projections(session, company_id, "item")
    ranked = sorted(
        [
            {
                "sku": r.state.get("sku"),
                "name": r.state.get("name"),
                "quantity": r.state.get("quantity", 0),
                "total_cost": r.state.get("total_cost", 0),
            }
            for r in items
        ],
        key=lambda x: float(x["total_cost"] or 0),
        reverse=True,
    )
    return {"items": ranked[:int(limit)]}


async def _active_deals_summary(session: AsyncSession, company_id: uuid.UUID, **_) -> dict:
    """Summarise CRM pipeline - open deals by stage."""
    deals = await _projections(session, company_id, "deal")
    pipeline: dict[str, dict] = {}
    for r in deals:
        status = r.state.get("status", "unknown")
        if status in {"won", "lost"}:
            continue
        stage = r.state.get("stage", status)
        if stage not in pipeline:
            pipeline[stage] = {"count": 0, "value": 0.0}
        pipeline[stage]["count"] += 1
        pipeline[stage]["value"] += float(r.state.get("value", 0) or 0)
    return {
        "stages": [
            {"stage": k, "count": v["count"], "value": round(v["value"], 2)}
            for k, v in pipeline.items()
        ]
    }


async def _active_contacts_list(session: AsyncSession, company_id: uuid.UUID, **_) -> dict:
    """Return a list of all contacts for entity mapping."""
    contacts = await _projections(session, company_id, "contact")
    return {
        "contacts": [
            {"id": str(r.entity_id), "name": r.state.get("name"), "type": r.state.get("contact_type")}
            for r in contacts
        ]
    }


async def _active_items_list(session: AsyncSession, company_id: uuid.UUID, **_) -> dict:
    """Return a list of all active items for entity mapping."""
    items = await _projections(session, company_id, "item")
    return {
        "items": [
            {"id": str(r.entity_id), "name": r.state.get("name"), "sku": r.state.get("sku")}
            for r in items
        ]
    }


async def _dormant_contacts(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 20, **_,
) -> dict:
    """List contacts with no invoice/bill activity in the past 90 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    contacts = await _projections(session, company_id, "contact")
    docs = await _projections(session, company_id, "doc")

    # Build map: contact_id -> latest doc updated_at
    latest_activity: dict[str, datetime] = {}
    for doc in docs:
        if doc.state.get("doc_type") not in ("invoice", "bill"):
            continue
        contact_id = doc.state.get("contact_id")
        if not contact_id:
            continue
        ts = doc.updated_at
        if ts and (contact_id not in latest_activity or ts > latest_activity[contact_id]):
            latest_activity[contact_id] = ts

    dormant = []
    for r in contacts:
        cid = str(r.entity_id)
        last = latest_activity.get(cid)
        ts = last.replace(tzinfo=timezone.utc) if last and last.tzinfo is None else last
        if ts is None or ts < cutoff:
            dormant.append({
                "id": cid,
                "name": r.state.get("name"),
                "type": r.state.get("contact_type"),
                "last_activity": last.isoformat() if last else None,
            })
    return {"dormant_contacts": dormant[:int(limit)], "total_count": len(dormant)}


async def _top_sellers(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 10, **_,
) -> dict:
    """List best selling items by aggregating invoice line items."""
    docs = await _projections(session, company_id, "doc")

    sales: dict[str, dict] = {}
    for doc in docs:
        if doc.state.get("doc_type") != "invoice":
            continue
        for line in doc.state.get("line_items", []):
            key = line.get("item_id") or line.get("sku") or line.get("description", "unknown")
            qty = float(line.get("quantity", 0) or 0)
            revenue = float(line.get("line_total", 0) or 0)
            if key not in sales:
                sales[key] = {
                    "sku": line.get("sku") or key,
                    "name": line.get("description") or line.get("name") or key,
                    "qty_sold": 0.0,
                    "revenue": 0.0,
                }
            sales[key]["qty_sold"] += qty
            sales[key]["revenue"] += revenue

    ranked = sorted(sales.values(), key=lambda x: x["qty_sold"], reverse=True)
    return {"top_sellers": ranked[:int(limit)], "total_count": len(ranked)}


async def _pending_pos(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 20, **_,
) -> dict:
    """List pending purchase orders."""
    docs = await _projections(session, company_id, "doc")
    pos = [
        {
            "doc_number": r.state.get("doc_number"),
            "contact_name": r.state.get("contact_name"),
            "total": r.state.get("total", 0),
            "status": r.state.get("status"),
        }
        for r in docs
        if r.state.get("doc_type") == "po"
        and r.state.get("status") not in ("received", "billed", "void")
    ]
    return {"pending_pos": pos[:int(limit)], "total_count": len(pos)}


# -- Registry ---------------------------------------------------------------

TOOLS: dict[str, ToolDef] = {
    t.name: t
    for t in [
        ToolDef("dashboard_kpis", "Return high-level KPIs: inventory value, AR outstanding, low stock count, active deals.", [], _dashboard_kpis),
        ToolDef("low_stock_items", "List items with zero or negative quantity. Param: limit (default 20).", ["limit"], _low_stock_items),
        ToolDef("outstanding_invoices", "List unpaid invoices. Param: limit (default 20).", ["limit"], _outstanding_invoices),
        ToolDef("top_items_by_value", "List items ranked by total inventory value. Param: limit (default 10).", ["limit"], _top_items_by_value),
        ToolDef("active_deals_summary", "Summarise the CRM pipeline - open deals by stage with count and total value.", [], _active_deals_summary),
        ToolDef("active_contacts_list", "Return a list of all contacts for entity mapping.", [], _active_contacts_list),
        ToolDef("active_items_list", "Return a list of all active items for entity mapping.", [], _active_items_list),
        ToolDef("dormant_contacts", "List contacts with no recent activity. Param: limit (default 20).", ["limit"], _dormant_contacts),
        ToolDef("top_sellers", "List best selling items. Param: limit (default 10).", ["limit"], _top_sellers),
        ToolDef("pending_pos", "List pending purchase orders. Param: limit (default 20).", ["limit"], _pending_pos),
    ]
}


async def execute_tool(
    name: str,
    params: dict[str, Any],
    session: AsyncSession,
    company_id: uuid.UUID,
) -> dict[str, Any]:
    """Run a tool by name. Raises KeyError if name is unknown."""
    return await TOOLS[name].fn(session=session, company_id=company_id, **params)
