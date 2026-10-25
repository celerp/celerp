# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.projections import Projection
from celerp.output.doc_print import EMPTY
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.business_time import company_today
from celerp.services.doc_balance import (
    canonical_doc_type, doc_value, is_awaiting_payment, is_open_credit, is_owed, outstanding_balance,
)
from celerp.services.money import document_line_amount
from celerp.services.permissions import require_permission
from celerp_contacts.references import party_name_sql

router = APIRouter(dependencies=[Depends(get_current_user)])


def _line_amount(line: dict, doc: dict) -> Decimal:
    """A line's amount as its document charges it; a line that cannot be read adds nothing,
    as an unreadable figure does everywhere in these reports."""
    amount = document_line_amount(line, doc.get("currency") or "USD")
    return Decimal(0) if amount is None else amount


def _parse_d(val: str | None) -> Decimal:
    if val is None:
        return Decimal(0)
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal(0)


def _aged_balance(state: dict, doc_types: tuple[str, ...], credit_types: tuple[str, ...]) -> Decimal | None:
    """What a document of one of ``doc_types`` awaiting payment still owes, or, negative, what an
    open credit of one of ``credit_types`` still has to apply or refund. None when it is not
    aged: another type, not issued, or settled."""
    doc_type = canonical_doc_type(state.get("doc_type", state.get("type", "")))
    if doc_type in credit_types:
        return -outstanding_balance(state) if is_open_credit({**state, "doc_type": doc_type}) else None
    if doc_type not in doc_types or not is_awaiting_payment(doc_type, state.get("status")) or not is_owed(state):
        return None
    return outstanding_balance(state)


def _doc_cogs(inv: dict) -> Decimal:
    """Compute COGS for a document from its line_items (cost_price * quantity).

    Falls back to cost_total field if present (legacy/future use).
    """
    if inv.get("cost_total"):
        return _parse_d(inv["cost_total"])
    return sum(
        _parse_d(li.get("cost_price") or 0) * _parse_d(li.get("quantity") or 0)
        for li in (inv.get("line_items") or [])
    )


def _in_range(ts: str | None, date_from: str | None, date_to: str | None) -> bool:
    if not ts:
        return True
    if date_from and ts < date_from:
        return False
    if date_to and ts > date_to + "T23:59:59":
        return False
    return True


# ---------------------------------------------------------------------------
# AR and AP aging
# ---------------------------------------------------------------------------

_BUCKETS = (("current", "current"), ("d30", "1-30"), ("d60", "31-60"), ("d90", "61-90"), ("d90plus", "90+"))


def _bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    return next((key for key, limit in (("d30", 30), ("d60", 60), ("d90", 90)) if days_overdue <= limit), "d90plus")


async def _aging(session: AsyncSession, company_id: uuid.UUID, *, party: str, debit_types: tuple[str, ...],
                 credit_types: tuple[str, ...], due_fallback: str, unlinked: tuple[str, str]) -> dict:
    """Aging buckets per ``party`` (customer or supplier): what each document of ``debit_types``
    still owes, by days past its due date (``due_fallback`` when it has none), less what each
    open credit of ``credit_types`` has left, by days since it was issued. A party shows by its
    contact's name, else the name the document holds, else "--". ``unlinked`` is the (id, name)
    of documents that name no party."""
    rows = (await session.execute(select(Projection.state, party_name_sql(party=party)).where(
        Projection.company_id == company_id, Projection.entity_type == "doc"))).all()

    today = await company_today(session, company_id)
    buckets: dict[str, dict[str, Decimal]] = defaultdict(lambda: {key: Decimal(0) for key, _ in _BUCKETS})
    names: dict[str, str] = {}

    for state, shown in rows:
        amount = _aged_balance(state, debit_types, credit_types)
        if amount is None:
            continue
        party_id = state.get("contact_id") or state.get(f"{party}_id")
        if party_id:
            names[party_id] = shown or EMPTY
        else:
            party_id, names[unlinked[0]] = unlinked
        dated = doc_value(state, "issue_date") if amount < 0 else (doc_value(state, "due_date") or state.get(due_fallback))
        try:
            since = date.fromisoformat(str(dated or today.isoformat())[:10])
        except ValueError:
            since = today
        buckets[party_id][_bucket((today - since).days)] += amount

    lines = [{
        f"{party}_id": pid,
        f"{party}_name": names.get(pid, pid),
        **{key: float(b[key]) for key, _ in _BUCKETS},
        "total": float(sum(b.values())),
    } for pid, b in sorted(buckets.items(), key=lambda x: names.get(x[0], x[0]))]
    totals = {label: float(sum(b[key] for b in buckets.values())) for key, label in _BUCKETS}
    return {"as_of": today.isoformat(), "lines": lines, "buckets": totals}


@router.get("/ar-aging")
async def ar_aging(
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Accounts receivable aging buckets per customer: open invoices less open credit notes."""
    return await _aging(session, company_id, party="customer", debit_types=("invoice",),
                        credit_types=("credit_note",), due_fallback="date", unlinked=("unlinked", "Unlinked Invoices"))


@router.get("/ap-aging")
async def ap_aging(
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Accounts payable aging buckets per supplier: open bills and purchase orders less open
    supplier credit notes."""
    return await _aging(session, company_id, party="supplier", debit_types=("purchase_order", "bill"),
                        credit_types=("debit_note",), due_fallback="expected_delivery", unlinked=("Unlinked", "Unlinked"))


# ---------------------------------------------------------------------------
# Sales report
# ---------------------------------------------------------------------------

@router.get("/sales")
async def sales_report(
    group_by: str = "customer",  # customer | item | period
    period: str = "monthly",     # daily | weekly | monthly (for group_by=period)
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Sales report from invoice projections.

    group_by=customer: revenue/cost/profit per customer.
    group_by=item: revenue/cost/profit per item (from line items).
    group_by=period: revenue/cost/profit aggregated by day/week/month.
    """
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
            )
        )
    ).scalars().all()

    # Only finalized (non-void, non-draft) invoices
    invoices = [
        r.state for r in rows
        if r.state.get("doc_type", r.state.get("type", "")) in ("invoice", "Invoice")
        and r.state.get("status", "") not in ("void", "draft")
        and _in_range(r.state.get("date") or r.state.get("created_at"), date_from, date_to)
    ]

    if group_by == "customer":
        data: dict[str, dict] = defaultdict(lambda: {
            "invoice_count": 0, "total_revenue": Decimal(0),
            "total_cost": Decimal(0), "name": "",
        })
        for inv in invoices:
            cid = inv.get("contact_id") or inv.get("customer_id") or "Unlinked"
            data[cid]["name"] = inv.get("contact_name") or inv.get("customer_name") or cid
            data[cid]["invoice_count"] += 1
            data[cid]["total_revenue"] += _parse_d(inv.get("total"))
            data[cid]["total_cost"] += _doc_cogs(inv)
        lines = []
        for cid, d in sorted(data.items(), key=lambda x: -x[1]["total_revenue"]):
            rev = float(d["total_revenue"])
            cost = float(d["total_cost"])
            gp = rev - cost
            lines.append({
                "customer_id": cid,
                "customer_name": d["name"],
                "invoice_count": d["invoice_count"],
                "total_revenue": rev,
                "total_cost": cost,
                "gross_profit": gp,
                "margin_pct": round(gp / rev * 100, 1) if rev else 0,
            })

    elif group_by == "item":
        data = defaultdict(lambda: {
            "qty_sold": Decimal(0), "total_revenue": Decimal(0),
            "total_cost": Decimal(0), "name": "",
        })
        for inv in invoices:
            for line in inv.get("line_items", inv.get("items", [])):
                iid = line.get("item_id") or line.get("entity_id") or "Unlinked"
                data[iid]["name"] = line.get("name") or line.get("description") or iid
                data[iid]["qty_sold"] += _parse_d(line.get("quantity") or 1)
                data[iid]["total_revenue"] += _line_amount(line, inv)
                data[iid]["total_cost"] += _parse_d(line.get("cost_price") or 0) * _parse_d(line.get("quantity") or 0)
        lines = []
        for iid, d in sorted(data.items(), key=lambda x: -x[1]["qty_sold"]):
            rev = float(d["total_revenue"])
            cost = float(d["total_cost"])
            gp = rev - cost
            lines.append({
                "item_id": iid,
                "item_name": d["name"],
                "qty_sold": float(d["qty_sold"]),
                "total_revenue": rev,
                "total_cost": cost,
                "gross_profit": gp,
                "avg_price": round(rev / float(d["qty_sold"]), 2) if d["qty_sold"] else 0,
            })

    elif group_by == "price_range":
        _BUCKETS = [
            (0, 10, "0-10"),
            (11, 25, "11-25"),
            (26, 50, "26-50"),
            (51, 100, "51-100"),
            (101, 200, "101-200"),
            (201, 500, "201-500"),
            (501, 1000, "501-1000"),
            (1001, 2500, "1001-2500"),
            (2501, 5000, "2501-5000"),
            (5001, 15000, "5001-15000"),
            (15001, 50000, "15001-50000"),
            (50001, None, "50000+"),
        ]
        # Aggregate per unique item (by item_id or item_name fallback)
        data_pr: dict[str, dict] = {}
        for inv in invoices:
            for li in inv.get("line_items", []):
                item_id = li.get("item_id") or ""
                item_name = li.get("item_name") or li.get("description") or li.get("name") or ""
                key = item_id or item_name or "__unknown__"
                unit_price = _parse_d(li.get("unit_price") or li.get("price") or 0)
                qty = _parse_d(li.get("quantity") or 1)
                line_total = _parse_d(li.get("total") or (unit_price * qty))
                cost_price = _parse_d(li.get("cost_price") or 0)
                bucket = _BUCKETS[-1][2]
                for _lo, hi, lbl in _BUCKETS:
                    if hi is None or unit_price <= hi:
                        bucket = lbl
                        break
                if key not in data_pr:
                    data_pr[key] = {
                        "item_id": item_id,
                        "item_name": item_name,
                        "unit_price": unit_price,
                        "price_range": bucket,
                        "qty_sold": Decimal(0),
                        "total_revenue": Decimal(0),
                        "total_cost": Decimal(0),
                    }
                data_pr[key]["qty_sold"] += qty
                data_pr[key]["total_revenue"] += line_total
                data_pr[key]["total_cost"] += cost_price * qty
        lines = []
        for entry in data_pr.values():
            rev = float(entry["total_revenue"])
            cost = float(entry["total_cost"])
            gp = rev - cost
            lines.append({
                "item_id": entry["item_id"],
                "item_name": entry["item_name"],
                "unit_price": float(entry["unit_price"]),
                "price_range": entry["price_range"],
                "qty_sold": float(entry["qty_sold"]),
                "total_revenue": rev,
                "total_cost": cost,
                "gross_profit": gp,
                "margin_pct": round(gp / rev * 100, 1) if rev else 0,
            })
        # Sort by price range bucket order then item name
        _bucket_order = {lbl: i for i, (_, _, lbl) in enumerate(_BUCKETS)}
        lines.sort(key=lambda l: (_bucket_order.get(l["price_range"], 99), l["item_name"].lower()))

    else:  # period
        def _period_key(ts: str) -> str:
            try:
                d = date.fromisoformat(ts[:10])
            except ValueError:
                return "unknown"
            if period == "daily":
                return d.isoformat()
            elif period == "weekly":
                # ISO week start (Monday)
                return (d - timedelta(days=d.weekday())).isoformat()
            else:  # monthly
                return d.strftime("%Y-%m")

        data = defaultdict(lambda: {
            "invoice_count": 0, "total_revenue": Decimal(0), "total_cost": Decimal(0),
        })
        for inv in invoices:
            ts = inv.get("date") or inv.get("created_at") or ""
            key = _period_key(ts)
            data[key]["invoice_count"] += 1
            data[key]["total_revenue"] += _parse_d(inv.get("total"))
            data[key]["total_cost"] += _doc_cogs(inv)
        lines = []
        for key in sorted(data):
            rev = float(data[key]["total_revenue"])
            cost = float(data[key]["total_cost"])
            gp = rev - cost
            lines.append({
                "period": key,
                "invoice_count": data[key]["invoice_count"],
                "total_revenue": rev,
                "total_cost": cost,
                "gross_profit": gp,
            })

    total_revenue = sum(l["total_revenue"] for l in lines)
    total_cost = sum(l.get("total_cost", 0) for l in lines)

    return {
        "group_by": group_by,
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "lines": lines,
        "total_revenue": total_revenue,
        "total_cost": total_cost,
        "gross_profit": total_revenue - total_cost,
    }


# ---------------------------------------------------------------------------
# Purchasing report
# ---------------------------------------------------------------------------

@router.get("/purchases")
async def purchases_report(
    group_by: str = "supplier",  # supplier | item | period
    period: str = "monthly",
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = require_permission("view_financial_reports"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Purchasing report from purchase order projections."""
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
            )
        )
    ).scalars().all()

    pos = [
        r.state for r in rows
        if r.state.get("doc_type", r.state.get("type", "")) in ("purchase_order", "PO")
        and r.state.get("status", "") not in ("void", "draft")
        and _in_range(r.state.get("date") or r.state.get("created_at"), date_from, date_to)
    ]

    if group_by == "supplier":
        data: dict[str, dict] = defaultdict(lambda: {
            "po_count": 0, "total_spend": Decimal(0), "name": "",
        })
        for po in pos:
            sid = po.get("contact_id") or po.get("supplier_id") or "Unlinked"
            data[sid]["name"] = po.get("contact_name") or po.get("supplier_name") or sid
            data[sid]["po_count"] += 1
            data[sid]["total_spend"] += _parse_d(po.get("total"))
        lines = [
            {
                "supplier_id": sid,
                "supplier_name": d["name"],
                "po_count": d["po_count"],
                "total_spend": float(d["total_spend"]),
            }
            for sid, d in sorted(data.items(), key=lambda x: -x[1]["total_spend"])
        ]

    elif group_by == "item":
        data = defaultdict(lambda: {
            "qty_purchased": Decimal(0), "total_spend": Decimal(0), "name": "",
        })
        for po in pos:
            for line in po.get("line_items", po.get("items", [])):
                iid = line.get("item_id") or line.get("entity_id") or "Unlinked"
                data[iid]["name"] = line.get("name") or line.get("description") or iid
                data[iid]["qty_purchased"] += _parse_d(line.get("quantity") or 1)
                data[iid]["total_spend"] += _line_amount(line, po)
        lines = [
            {
                "item_id": iid,
                "item_name": d["name"],
                "qty_purchased": float(d["qty_purchased"]),
                "total_spend": float(d["total_spend"]),
                "avg_unit_cost": round(float(d["total_spend"]) / float(d["qty_purchased"]), 2) if d["qty_purchased"] else 0,
            }
            for iid, d in sorted(data.items(), key=lambda x: -x[1]["total_spend"])
        ]

    elif group_by == "price_range":
        _BUCKETS_PO = [
            (0, 10, "0-10"),
            (11, 25, "11-25"),
            (26, 50, "26-50"),
            (51, 100, "51-100"),
            (101, 200, "101-200"),
            (201, 500, "201-500"),
            (501, 1000, "501-1000"),
            (1001, 2500, "1001-2500"),
            (2501, 5000, "2501-5000"),
            (5001, 15000, "5001-15000"),
            (15001, 50000, "15001-50000"),
            (50001, None, "50000+"),
        ]
        _bucket_order_po = {lbl: i for i, (_, _, lbl) in enumerate(_BUCKETS_PO)}
        data_pr_po: dict[str, dict] = {}
        for po in pos:
            for li in po.get("line_items", []):
                item_id = li.get("item_id") or ""
                item_name = li.get("item_name") or li.get("description") or li.get("name") or ""
                key = item_id or item_name or "__unknown__"
                unit_price = _parse_d(li.get("unit_price") or li.get("price") or 0)
                qty = _parse_d(li.get("quantity") or 1)
                line_total = _parse_d(li.get("total") or (unit_price * qty))
                cost_price = _parse_d(li.get("cost_price") or 0)
                bucket = _BUCKETS_PO[-1][2]
                for _lo, hi, lbl in _BUCKETS_PO:
                    if hi is None or unit_price <= hi:
                        bucket = lbl
                        break
                if key not in data_pr_po:
                    data_pr_po[key] = {
                        "item_id": item_id,
                        "item_name": item_name,
                        "unit_price": unit_price,
                        "price_range": bucket,
                        "qty_purchased": Decimal(0),
                        "total_spend": Decimal(0),
                        "total_cost": Decimal(0),
                    }
                data_pr_po[key]["qty_purchased"] += qty
                data_pr_po[key]["total_spend"] += line_total
                data_pr_po[key]["total_cost"] += cost_price * qty
        lines = []
        for entry in data_pr_po.values():
            lines.append({
                "item_id": entry["item_id"],
                "item_name": entry["item_name"],
                "unit_price": float(entry["unit_price"]),
                "price_range": entry["price_range"],
                "qty_purchased": float(entry["qty_purchased"]),
                "total_spend": float(entry["total_spend"]),
            })
        lines.sort(key=lambda l: (_bucket_order_po.get(l["price_range"], 99), l["item_name"].lower()))

    else:  # period
        def _period_key(ts: str) -> str:
            try:
                d = date.fromisoformat(ts[:10])
            except ValueError:
                return "unknown"
            if period == "daily":
                return d.isoformat()
            elif period == "weekly":
                return (d - timedelta(days=d.weekday())).isoformat()
            else:
                return d.strftime("%Y-%m")

        data = defaultdict(lambda: {"po_count": 0, "total_spend": Decimal(0)})
        for po in pos:
            ts = po.get("date") or po.get("created_at") or ""
            key = _period_key(ts)
            data[key]["po_count"] += 1
            data[key]["total_spend"] += _parse_d(po.get("total"))
        lines = [
            {
                "period": key,
                "po_count": data[key]["po_count"],
                "total_spend": float(data[key]["total_spend"]),
            }
            for key in sorted(data)
        ]

    total_spend = sum(l["total_spend"] for l in lines)

    return {
        "group_by": group_by,
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "lines": lines,
        "total_spend": total_spend,
    }


# ---------------------------------------------------------------------------
# Expiring items report
# ---------------------------------------------------------------------------

@router.get("/expiring")
async def expiring_items(
    days: int = 30,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Items expiring within `days` days, sorted by soonest first."""
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
            )
        )
    ).scalars().all()

    today = await company_today(session, company_id)
    cutoff = today + timedelta(days=days)
    lines = []

    for row in rows:
        state = row.state
        exp_str = state.get("expires_at")
        if not exp_str:
            continue
        try:
            exp = date.fromisoformat(str(exp_str)[:10])
        except ValueError:
            continue
        days_remaining = (exp - today).days
        if days_remaining > days:
            continue
        lines.append({
            "entity_id": row.entity_id,
            "sku": state.get("sku"),
            "name": state.get("name"),
            "category": state.get("category"),
            "expires_at": exp.isoformat(),
            "days_remaining": days_remaining,
            "location_id": str(row.location_id) if row.location_id else None,
            "status": state.get("status"),
        })

    lines.sort(key=lambda x: x["days_remaining"])
    return {"as_of": today.isoformat(), "days_threshold": days, "count": len(lines), "lines": lines}
