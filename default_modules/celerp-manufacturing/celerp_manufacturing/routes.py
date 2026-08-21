# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-manufacturing API routes.

Registered into the FastAPI app by the module loader via setup_api_routes().
All routes are mounted under /manufacturing (set in PLUGIN_MANIFEST or by the
loader's register_api_routes calling setup_api_routes with the app directly).
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.events.schemas import (
    _WORKFLOW_TIME_UNITS,
    RecipeSpec,
    WorkflowSpec,
    workflow_step_minutes,
)
from celerp.models.company import Company, User, WorkCenter
from celerp.models.projections import Projection
from celerp.notifications import service as notif_svc
from celerp.services import auto_je
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.permissions import require_permission

from .costing import RecipeError, labor_hours, roll_up_cost, where_used

# Default hours-per-day for converting daily labor lines into the est-hours column.
# Set per work center; the company's default center supplies the value.
DEFAULT_HOURS_PER_DAY = 8.0
from .expansion import expand_recipe, explode_demand, is_manufacturable
from .labor import apply_labor_providers

router = APIRouter(prefix="/manufacturing", dependencies=[Depends(get_current_user)], tags=["manufacturing"])

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class MfgInput(BaseModel):
    item_id: str
    quantity: float


class MfgOutput(BaseModel):
    sku: str
    name: str
    quantity: float
    category: str | None = None


class MfgOrderCreate(BaseModel):
    description: str
    order_type: str = "assembly"
    inputs: list[MfgInput] = Field(default_factory=list)
    expected_outputs: list[MfgOutput] = Field(default_factory=list)
    location_id: str | None = None
    assigned_to: str | None = None
    due_date: str | None = None
    estimated_cost: float | None = None
    notes: str | None = None
    idempotency_key: str | None = None


class ScheduleBody(BaseModel):
    # Phase-A scheduling. Each field is optional; only the keys present are written (a blank
    # string clears that field). priority is a free label (e.g. low / normal / high / urgent).
    due_date: str | None = None
    planned_start: str | None = None
    priority: str | None = None
    idempotency_key: str | None = None


class IssueBody(BaseModel):
    # Components to issue from stock into a run. Omit `items` to issue everything still outstanding.
    items: list[MfgInput] | None = None
    idempotency_key: str | None = None


class ReceiveBody(BaseModel):
    # Finished-goods quantity to receive. Omit `quantity` to receive everything still outstanding.
    quantity: float | None = None
    idempotency_key: str | None = None


class CompleteBody(BaseModel):
    actual_outputs: list[MfgOutput] | None = None
    waste_quantity: float | None = Field(default=None, ge=0)
    waste_unit: str | None = None
    waste_reason: str | None = None
    labor_hours: float | None = None
    idempotency_key: str | None = None


class CancelBody(BaseModel):
    reason: str | None = None
    idempotency_key: str | None = None


class MfgImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


class MfgBatchImportRequest(BaseModel):
    records: list[MfgImportRecord]


class BatchImportResult(BaseModel):
    created: int
    skipped: int
    updated: int = 0
    errors: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_order(session: AsyncSession, company_id, order_id: str) -> Projection:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": order_id})
    if row is None or row.entity_type != "mfg_order":
        raise HTTPException(status_code=404, detail="Manufacturing order not found")
    return row


async def _load_recipe_graph(session: AsyncSession, company_id, root_id: str, root_state: dict) -> tuple[dict[str, dict], list[str]]:
    """Load every item referenced (transitively) by ``root_state``'s recipe into a
    lookup dict, so the pure cost roll-up can resolve nested sub-assemblies offline.

    Returns ``(graph, missing)`` where graph maps entity_id -> item state (the root is
    keyed by root_id with its *new* recipe already overlaid) and missing lists any
    referenced item_id that does not resolve to an item in this company.
    """
    graph: dict[str, dict] = {root_id: root_state}
    missing: list[str] = []
    seen: set[str] = {root_id}
    queue: list[str] = [c.get("item_id") for c in (root_state.get("recipe") or {}).get("components", [])]
    while queue:
        cid = queue.pop()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        row = await session.get(Projection, {"company_id": company_id, "entity_id": cid})
        if row is None or row.entity_type != "item":
            missing.append(cid)
            continue
        graph[cid] = row.state
        queue.extend(c.get("item_id") for c in (row.state.get("recipe") or {}).get("components", []))
    return graph, missing


# ---------------------------------------------------------------------------
# Recipe endpoints (the manufacturing recipe attached to an inventory item)
# ---------------------------------------------------------------------------

@router.put("/items/{item_id}/recipe")
async def set_item_recipe(
    item_id: str,
    payload: RecipeSpec,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set (full-replace) the manufacturing recipe on an inventory item.

    Validates components, rolls the standard cost up from current component costs,
    and emits ``item.recipe.set``. Hard errors (422) on self-reference, unknown
    component SKUs, and recipe cycles — per GDR, validation lives at the function level.
    """
    item = await session.get(Projection, {"company_id": company_id, "entity_id": item_id})
    if item is None or item.entity_type != "item":
        raise HTTPException(status_code=404, detail="Item not found")

    recipe = payload.model_dump()
    if any(c.get("item_id") == item_id for c in recipe["components"]):
        raise HTTPException(status_code=422, detail="An item cannot be a component of itself")

    # Merge any auto-labor from registered providers (the future-module seam; no-op in v1).
    recipe["labor"] = apply_labor_providers(recipe["components"], recipe.get("labor", []))

    root_state = {**item.state, "recipe": recipe}
    graph, missing = await _load_recipe_graph(session, company_id, item_id, root_state)
    if missing:
        raise HTTPException(status_code=422, detail=f"Component item(s) not found: {', '.join(sorted(set(missing)))}")

    # The component unit is not free text — it is the component item's own sell unit.
    for comp in recipe["components"]:
        cstate = graph.get(comp.get("item_id")) or {}
        comp["unit"] = cstate.get("sell_by") or cstate.get("unit") or comp.get("unit")
        comp["sku"] = cstate.get("sku") or comp.get("sku")

    try:
        breakdown = roll_up_cost(recipe, graph.get, _path=frozenset({item_id}))
    except RecipeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    recipe.update(breakdown)

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=item_id,
        entity_type="item",
        event_type="item.recipe.set",
        data={"recipe": recipe},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await _apply_standard_cost(session, company_id, user, item_id, item.state, recipe)
    # Editing this recipe changes its cost, so cascade to anything that uses it (mark-to-market).
    await _recost_dependents_of(session, company_id, user, item_id)
    await session.commit()
    return {"event_id": entry.id, "recipe": recipe}


# ---------------------------------------------------------------------------
# Production workflow endpoints (the ordered build steps attached to an item)
# ---------------------------------------------------------------------------

@router.put("/items/{item_id}/workflow")
async def set_item_workflow(
    item_id: str,
    payload: WorkflowSpec,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set (full-replace) the production workflow on an inventory item.

    The workflow is the shop-floor build sequence — independent of the costing
    recipe. We validate at the function level (GDR), assign a stable id to any
    new step, and normalize each step's elapsed time to canonical minutes so the
    stored data is always single-unit. Emits ``item.workflow.set``; the inventory
    projection stores it verbatim.
    """
    item = await session.get(Projection, {"company_id": company_id, "entity_id": item_id})
    if item is None or item.entity_type != "item":
        raise HTTPException(status_code=404, detail="Item not found")

    file_ids = {f.get("id") for f in (item.state.get("files") or [])}
    steps = []
    for step in payload.steps:
        if step.time_unit not in _WORKFLOW_TIME_UNITS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid time unit '{step.time_unit}' (use one of {', '.join(_WORKFLOW_TIME_UNITS)})",
            )
        if step.ref_file_id and step.ref_file_id not in file_ids:
            raise HTTPException(
                status_code=422,
                detail=f"Reference file '{step.ref_file_id}' is not attached to this item",
            )
        data = step.model_dump()
        data["id"] = step.id or str(uuid.uuid4())
        data["time_minutes"] = workflow_step_minutes(step.time_value, step.time_unit)
        steps.append(data)

    workflow = {"steps": steps}
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=item_id,
        entity_type="item",
        event_type="item.workflow.set",
        data={"workflow": workflow},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "workflow": workflow}


async def _apply_standard_cost(session: AsyncSession, company_id, user, item_id: str,
                               item_state: dict, recipe: dict) -> None:
    """Keep a manufactured item's cost_price equal to its rolled standard cost.

    The recipe is the single source of truth for a manufactured item's cost, so cost_price
    tracks the rolled unit cost automatically (no manual "apply" step). Only fires for a
    real recipe (has components) and only when the value actually changes, so editing a
    non-cost field or re-saving the same value emits no redundant pricing event.
    """
    if not recipe.get("components"):
        return
    new_cost = recipe.get("unit_cost")
    if new_cost is None:
        return
    current = item_state.get("cost_price")
    try:
        if current is not None and float(current) == float(new_cost):
            return
    except (TypeError, ValueError):
        pass
    await emit_event(
        session, company_id=company_id, entity_id=item_id, entity_type="item",
        event_type="item.pricing.set", data={"price_type": "cost_price", "new_price": new_cost},
        actor_id=user.id, location_id=None, source="auto", idempotency_key=str(uuid.uuid4()), metadata_={},
    )


class BuildBody(BaseModel):
    quantity: float = 1.0
    # One-tap build: create the run and immediately issue components + receive output + complete,
    # all in one action (restaurant / simple make-to-stock). False leaves a Planned run to be
    # issued/received step by step (jewelry stock room / WIP).
    complete: bool = False
    idempotency_key: str | None = None


@router.post("/items/{item_id}/build")
async def build_item(
    item_id: str,
    payload: BuildBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a production run to build N of a manufacturable item — inputs expand from its recipe.

    With ``complete=true`` this is a one-tap build: the run is created, its components issued,
    its output received as a discrete lot, and the run completed in a single call.
    """
    item = await session.get(Projection, {"company_id": company_id, "entity_id": item_id})
    if item is None or item.entity_type != "item":
        raise HTTPException(status_code=404, detail="Item not found")
    if not is_manufacturable(item.state):
        raise HTTPException(status_code=422, detail="Item has no recipe to build from")
    if str((item.state or {}).get("status") or "").lower() == "draft":
        raise HTTPException(status_code=422, detail="Cannot build into a draft item; make it available first.")
    if payload.quantity <= 0:
        raise HTTPException(status_code=422, detail="Build quantity must be greater than zero")
    inputs, outputs = expand_recipe(item.state, payload.quantity)
    order_id = f"mfg:{uuid.uuid4()}"
    entry = await emit_event(
        session, company_id=company_id, entity_id=order_id, entity_type="mfg_order",
        event_type="mfg.order.created",
        data={
            "description": f"Build {payload.quantity:g} x {item.state.get('sku', '')}",
            "order_type": "assembly", "inputs": inputs, "expected_outputs": outputs,
            # The product this run makes — links the run to its product Manufacturing tab.
            "output_item_id": item_id,
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    if payload.complete:
        states = await _all_item_states(session, company_id)
        run = await _get_order(session, company_id, order_id)
        await _issue_and_record(session, company_id, user, order_id, run.state.get("inputs", []), states)
        await _receive(session, company_id, user, order_id, run.state, payload.quantity, states)
        run = await _get_order(session, company_id, order_id)
        await _close_run(session, company_id, user, order_id, run.state, states)
    await session.commit()
    return {"event_id": entry.id, "id": order_id}


async def _all_item_states(session: AsyncSession, company_id) -> dict[str, dict]:
    """Load every item's projection state for this company, keyed by entity_id."""
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "item")
    )).scalars().all()
    return {r.entity_id: r.state for r in rows}


def _deps_from_states(states: dict[str, dict]) -> dict[str, set[str]]:
    """item_id -> set of component item_ids it directly uses (for where_used)."""
    return {
        iid: {c.get("item_id") for c in (st.get("recipe") or {}).get("components", []) if c.get("item_id")}
        for iid, st in states.items()
    }


async def _recost_one(session: AsyncSession, company_id, user, item_id: str, states: dict[str, dict]) -> dict | None:
    """Re-roll a manufactured item's recipe from current component costs and persist it.

    Returns the fresh recipe, or None if the item has no recipe. roll_up_cost recomputes the
    whole subtree live from current leaf costs, so re-costing order does not affect correctness.
    """
    st = states.get(item_id) or {}
    recipe = st.get("recipe")
    if not recipe or not recipe.get("components"):
        return None
    breakdown = roll_up_cost(recipe, states.get, _path=frozenset({item_id}))
    new_recipe = {**recipe, **breakdown}
    await emit_event(
        session, company_id=company_id, entity_id=item_id, entity_type="item",
        event_type="item.recipe.set", data={"recipe": new_recipe}, actor_id=user.id,
        location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={"recosted": True},
    )
    # Mark-to-market also flows through to cost_price (same rule as a direct recipe edit).
    await _apply_standard_cost(session, company_id, user, item_id, st, new_recipe)
    states[item_id] = {**st, "recipe": new_recipe, "cost_price": new_recipe.get("unit_cost")}
    return new_recipe


async def _recost_dependents_of(session: AsyncSession, company_id, user, item_id: str,
                                states: dict[str, dict] | None = None) -> list[str]:
    """Re-roll every item whose recipe uses ``item_id`` (directly or transitively) and persist.

    Does not commit — the caller owns the transaction. Order-independent: roll_up_cost recomputes
    each ancestor's whole subtree live from current leaf costs.
    """
    if states is None:
        states = await _all_item_states(session, company_id)
    targets = where_used(item_id, _deps_from_states(states))
    return [tid for tid in targets if await _recost_one(session, company_id, user, tid, states) is not None]


@router.post("/items/{item_id}/recost-dependents")
async def recost_dependents(
    item_id: str,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Re-cost every item whose recipe uses this one — mark-to-market.

    Called automatically after a component's cost changes (the inventory pricing page) so
    manufactured items that depend on it stay current with no manual step.
    """
    states = await _all_item_states(session, company_id)
    if item_id not in states:
        raise HTTPException(status_code=404, detail="Item not found")
    recosted = await _recost_dependents_of(session, company_id, user, item_id, states)
    await session.commit()
    return {"recosted": recosted, "count": len(recosted)}


# ---------------------------------------------------------------------------
# Manufacture-from-document endpoints (List / Pro Forma / Invoice → orders)
# ---------------------------------------------------------------------------

def _doc_lines(doc_state: dict) -> list[tuple[int, str | None, str, float, str]]:
    """Normalize a document's line_items to (index, item_id, line_id, qty, label)."""
    out = []
    for idx, li in enumerate(doc_state.get("line_items", [])):
        item_id = li.get("entity_id") or li.get("item_id")
        line_id = str(li.get("id") or li.get("line_id") or idx)
        qty = float(li.get("quantity") or 0)
        label = li.get("sku") or li.get("name") or item_id or f"line {idx + 1}"
        out.append((idx, item_id, line_id, qty, label))
    return out


# Document statuses whose lines no longer drive production.
_CLOSED_DOC_STATUSES = {"void", "cancelled", "converted", "expired"}


def _skip_as_demand(doc_state: dict) -> bool:
    """Should this document's lines be ignored as production demand?

    Only customer invoices and internal stock orders (production orders) drive production - nothing
    else (quotations, transfers, audits and other lists, bills, POs, etc. are not demand). Closed docs
    never drive production. A draft invoice is a pro forma - a tentative, pre-sale document - so it is
    NOT counted as committed demand. An internal production order is likewise only active demand once
    finalized (a draft production order is still being composed)."""
    if doc_state.get("doc_type") not in ("invoice", "production_order"):
        return True
    status = (doc_state.get("status") or "")
    if status in _CLOSED_DOC_STATUSES:
        return True
    if status == "draft" and doc_state.get("doc_type") in ("production_order", "invoice"):
        return True
    return False


def _peg(supply: float, docs: list[dict]) -> None:
    """FIFO-assign available supply (on hand + in progress) to demand documents by due date —
    soonest due first, undated last — so each doc shows how much of its demand is covered.
    Annotates each doc in place with covered / shortfall / coverage (covered|partial|short)."""
    remaining = max(0.0, supply)
    for d in sorted(docs, key=lambda x: (x.get("due") is None, x.get("due") or "")):
        q = float(d.get("quantity") or 0)
        cov = min(remaining, q)
        remaining -= cov
        d["covered"] = round(cov, 4)
        d["shortfall"] = round(max(0.0, q - cov), 4)
        d["coverage"] = "covered" if cov >= q else ("partial" if cov > 0 else "short")


def _in_progress_by_item(runs: list) -> dict[str, float]:
    """Expected output still coming from open production runs (planned/in_progress/on_hold),
    summed per product. This is supply already committed, so it offsets net demand."""
    out: dict[str, float] = {}
    for r in runs:
        rs = r.state or {}
        if (rs.get("status") or "").lower() not in _INCOMPLETE_STATUSES:
            continue
        item_id = rs.get("output_item_id")
        if not item_id:
            continue
        out[item_id] = out.get(item_id, 0.0) + sum(
            float(o.get("quantity") or 0) for o in rs.get("expected_outputs", []))
    return out


async def _compute_to_make(session: AsyncSession, company_id) -> list[dict]:
    """Open demand aggregated BY PRODUCT across every open demand document (customer
    invoices/pro formas/lists + internal production orders), netted against on-hand stock AND
    in-progress production, with each demanding document FIFO-pegged to available supply.

    Each row: the manufacturable product, total open demand, on hand, in progress, net to make
    (demand - on hand - in progress, clamped at 0), the soonest due date, the per-document
    breakdown with pegged coverage, and the rolled est unit cost / est cost / est hours. Rows
    where the product is not manufacturable (no recipe) are skipped. Sort: no-due-date first,
    then earliest due, then name (act on undated/soonest first)."""
    states = await _all_item_states(session, company_id)
    docs = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type.in_(("doc", "list")),
        )
    )).scalars().all()

    agg: dict[str, dict] = {}
    for doc in docs:
        st = doc.state or {}
        if _skip_as_demand(st):
            continue
        ref = st.get("ref_id") or doc.entity_id
        due = st.get("due_date") or st.get("promised_date") or None
        for _idx, item_id, _line_id, qty, _label in _doc_lines(st):
            if not item_id or qty <= 0:
                continue
            ist = states.get(item_id)
            if not is_manufacturable(ist):
                continue
            row = agg.setdefault(item_id, {
                "item_id": item_id, "sku": (ist or {}).get("sku"), "name": (ist or {}).get("name"),
                "demand": 0.0, "docs": {}, "due": None,
            })
            row["demand"] += qty
            d = row["docs"].setdefault(doc.entity_id, {
                "doc_id": doc.entity_id, "doc_number": ref,
                "doc_type": st.get("doc_type") or doc.entity_type,
                "contact_name": st.get("contact_name") or "", "due": due, "quantity": 0.0,
            })
            d["quantity"] += qty
            if due and (row["due"] is None or due < row["due"]):
                row["due"] = due

    runs = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "mfg_order",
        )
    )).scalars().all()
    in_progress = _in_progress_by_item(runs)

    # Every produced output is a discrete lot under the product, so on-hand must include them.
    lot_qty = _lot_qty_by_parent(states)
    hours_per_day = await _default_hours_per_day(session, company_id)
    items: list[dict] = []
    for item_id, row in agg.items():
        ist = states.get(item_id) or {}
        on_hand = float(ist.get("quantity") or 0) + lot_qty.get(item_id, 0.0)
        wip = in_progress.get(item_id, 0.0)
        supply = on_hand + wip
        to_make_qty = max(0.0, row["demand"] - supply)
        docs_list = list(row["docs"].values())
        _peg(supply, docs_list)
        recipe = ist.get("recipe") or {}
        out_qty = float(recipe.get("output_qty") or 1) or 1
        unit_cost = float(recipe.get("unit_cost") or 0)
        hours_per_unit = labor_hours(recipe, hours_per_day) / out_qty
        items.append({
            **{k: row[k] for k in ("item_id", "sku", "name", "due")},
            "unit": ist.get("sell_by") or ist.get("unit"),
            "demand": row["demand"], "on_hand": on_hand, "in_progress": wip,
            "to_make": to_make_qty,
            "doc_count": len(docs_list), "docs": docs_list,
            "est_unit_cost": round(unit_cost, 4),
            "est_cost": round(unit_cost * to_make_qty, 2),
            "est_hours": round(hours_per_unit * to_make_qty, 2),
        })
    items.sort(key=lambda r: (r["due"] is not None, r["due"] or "", (r["sku"] or r["name"] or "")))
    return items


@router.get("/to-make")
async def to_make(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The product-centric demand board (see _compute_to_make)."""
    items = await _compute_to_make(session, company_id)
    return {"items": items, "total": len(items)}


class BulkBuildBody(BaseModel):
    item_ids: list[str] = Field(default_factory=list)


async def _emit_work_order(session, company_id, actor_id, item_id: str, item_state: dict, qty: float,
                           source: dict | None = None) -> str:
    """Create a work order (mfg_order) to build qty of item_id, optionally linked 1:1 to a source
    order line via source_doc_* fields. Returns the new order id; the caller commits."""
    inputs, outputs = expand_recipe(item_state, qty)
    order_id = f"mfg:{uuid.uuid4()}"
    data = {
        "description": f"Build {qty:g} x {item_state.get('sku', '')}",
        "order_type": "assembly", "inputs": inputs, "expected_outputs": outputs,
        "output_item_id": item_id,
    }
    if source:
        data.update({k: v for k, v in source.items() if v not in (None, "")})
    await emit_event(
        session, company_id=company_id, entity_id=order_id, entity_type="mfg_order",
        event_type="mfg.order.created", data=data, actor_id=actor_id, location_id=None,
        source="api", idempotency_key=str(uuid.uuid4()), metadata_={})
    return order_id


async def _complete_work_order_now(session, company_id, user, order_id: str, qty: float, states: dict) -> None:
    """One-tap: issue all components, receive the output and close the work order."""
    run = await _get_order(session, company_id, order_id)
    await _issue_and_record(session, company_id, user, order_id, run.state.get("inputs", []), states)
    await _receive(session, company_id, user, order_id, run.state, qty, states)
    run = await _get_order(session, company_id, order_id)
    await _close_run(session, company_id, user, order_id, run.state, states)


def _line_source(doc: dict) -> dict:
    """Denormalised source-order fields stored on a work order so it shows which order it's for."""
    return {
        "source_doc_id": doc.get("doc_id"), "source_doc_number": doc.get("doc_number"),
        "source_doc_type": doc.get("doc_type"), "source_contact_name": doc.get("contact_name"),
        "source_due": doc.get("due"),
    }


class WorkOrderLineRef(BaseModel):
    item_id: str
    doc_id: str = ""  # the demand document the work order is for (1:1 link); blank = make-to-stock


class MakeWorkOrdersBody(BaseModel):
    lines: list[WorkOrderLineRef] = Field(default_factory=list)
    complete: bool = False  # one-tap: also issue components, receive output and close each work order


@router.post("/to-make/make")
async def make_work_orders(
    payload: MakeWorkOrdersBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create one work order per selected demand line, linked 1:1 to its source order, for the
    line's net shortfall (the FIFO-pegged uncovered quantity). With ``complete=true`` each is also
    issued, received and closed. This is Demand Planning's 'Make selected' / 'Make & complete'."""
    if not payload.lines:
        return {"created": [], "skipped": []}
    rows = {r["item_id"]: r for r in await _compute_to_make(session, company_id)}
    states = await _all_item_states(session, company_id)
    created: list[dict] = []
    skipped: list[dict] = []
    for ln in payload.lines:
        row = rows.get(ln.item_id)
        st = states.get(ln.item_id)
        if not row or not is_manufacturable(st):
            skipped.append({"item_id": ln.item_id, "doc_id": ln.doc_id, "reason": "not manufacturable"})
            continue
        doc = next((d for d in row.get("docs", []) if d.get("doc_id") == ln.doc_id), None) if ln.doc_id else None
        qty = float((doc.get("shortfall") if doc else row.get("to_make")) or 0)
        if qty <= 0:
            skipped.append({"item_id": ln.item_id, "doc_id": ln.doc_id, "reason": "nothing to make"})
            continue
        order_id = await _emit_work_order(session, company_id, user.id, ln.item_id, st, qty,
                                          _line_source(doc) if doc else None)
        if payload.complete:
            await _complete_work_order_now(session, company_id, user, order_id, qty, states)
        created.append({"item_id": ln.item_id, "doc_id": ln.doc_id, "run_id": order_id, "quantity": qty})
    await session.commit()
    return {"created": created, "skipped": skipped}


async def auto_create_work_orders_on_finalize(session, entity_id, doc_state, company_id, user_id,
                                              doc_type=None, **kwargs) -> None:
    """doc_finalize_hook: when the company has work-order auto-creation enabled, create a linked work
    order for each manufacturable line on the just-finalized order (ordered qty minus on-hand). Runs
    inside the finalize transaction (the caller commits); failures are logged and non-fatal."""
    settings = await _mfg_settings(session, company_id)
    if not settings.get("auto_create_work_orders"):
        return
    auto_complete = bool(settings.get("auto_complete_work_orders"))
    user = await session.get(User, user_id) if auto_complete else None
    completed: list[str] = []
    failed: list[str] = []
    states = await _all_item_states(session, company_id)
    # Idempotent across re-finalize: skip items already linked to an open work order for this order.
    existing = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "mfg_order")
    )).scalars().all()
    linked = {(r.state or {}).get("output_item_id") for r in existing
              if (r.state or {}).get("source_doc_id") == entity_id
              and (r.state or {}).get("status") != "cancelled"}
    doc = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    dstate = (doc.state if doc else None) or doc_state or {}
    source = {
        "source_doc_id": entity_id, "source_doc_number": dstate.get("ref_id"),
        "source_doc_type": dstate.get("doc_type") or doc_type,
        "source_contact_name": dstate.get("contact_name"),
        "source_due": dstate.get("due_date") or dstate.get("promised_date"),
    }
    for _idx, item_id, _line_id, qty, _label in _doc_lines(doc_state):
        st = states.get(item_id)
        if not item_id or qty <= 0 or item_id in linked or not is_manufacturable(st):
            continue
        make_qty = max(0.0, qty - float((st or {}).get("quantity") or 0))
        if make_qty <= 0:
            continue
        order_id = await _emit_work_order(session, company_id, user_id, item_id, st, make_qty, source)
        linked.add(item_id)
        if auto_complete:
            # Complete the planned run on the spot, inside a savepoint so a mid-completion failure
            # (e.g. a raised event) rolls back only this line to a surviving planned run and neither
            # aborts the loop nor escapes the hook into the finalize commit.
            try:
                async with session.begin_nested():
                    await _complete_work_order_now(session, company_id, user, order_id, make_qty, states)
                completed.append(order_id)
            except Exception as exc:
                failed.append(order_id)
                log.warning("auto-complete failed for %s: %s", order_id, exc)

    if auto_complete and (completed or failed):
        # Disclose the automatic action (GDR 2d), itself savepoint-guarded so a failed notification
        # flush/prune cannot abort the invoice commit.
        try:
            async with session.begin_nested():
                if failed:
                    await notif_svc.create(
                        session, company_id, category="manufacturing",
                        title="Work orders need completing",
                        body=(f"{len(failed)} work order(s) could not auto-complete on invoice posting "
                              f"and are left planned: {', '.join(failed)}. Complete them manually."),
                        priority="high", action_url="/manufacturing/production?status=planned")
                else:
                    await notif_svc.create(
                        session, company_id, category="manufacturing",
                        title="Work orders auto-completed",
                        body=(f"{len(completed)} work order(s) were completed automatically on invoice "
                              f"posting: {', '.join(completed)}."),
                        priority="medium", action_url="/manufacturing/production")
        except Exception as exc:
            log.warning("auto-complete notification failed: %s", exc)


@router.post("/to-make/requirements")
async def bulk_requirements(
    payload: BulkBuildBody,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Aggregated, recursively-exploded raw-material + sub-assembly requirements to make the net
    shortfall of the selected products - the 'Print component requirements' pick list."""
    rows = {r["item_id"]: r for r in await _compute_to_make(session, company_id)}
    states = await _all_item_states(session, company_id)
    lines: list[tuple[str, float]] = []
    products: list[dict] = []
    for item_id in payload.item_ids:
        qty = float((rows.get(item_id) or {}).get("to_make") or 0)
        if qty <= 0 or not is_manufacturable(states.get(item_id)):
            continue
        lines.append((item_id, qty))
        ist = states.get(item_id) or {}
        products.append({"item_id": item_id, "sku": ist.get("sku"), "name": ist.get("name"),
                         "quantity": qty, "unit": ist.get("sell_by") or ist.get("unit")})
    demand = explode_demand(lines, states.get) if lines else {"sub_assemblies": {}, "raw_materials": {}}

    def _detail(d: dict[str, float]) -> list[dict]:
        return [
            {"item_id": iid, "sku": (states.get(iid) or {}).get("sku"),
             "name": (states.get(iid) or {}).get("name"), "quantity": q,
             "unit": (states.get(iid) or {}).get("sell_by") or (states.get(iid) or {}).get("unit")}
            for iid, q in sorted(d.items(), key=lambda kv: (states.get(kv[0]) or {}).get("sku") or kv[0])
        ]

    # The selected products are shown in `products`; drop them from sub-assemblies so that section
    # lists only intermediate manufactured parts (not the finished goods themselves).
    selected_ids = {item_id for item_id, _ in lines}
    subs = {k: v for k, v in demand["sub_assemblies"].items() if k not in selected_ids}
    return {
        "products": products,
        "sub_assemblies": _detail(subs),
        "raw_materials": _detail(demand["raw_materials"]),
    }


class BulkRunActionBody(BaseModel):
    run_ids: list[str] = Field(default_factory=list)
    action: str = ""


_BULK_RUN_ACTIONS = {"start", "issue", "complete", "hold", "resume", "cancel"}
_CLOSED_RUN_STATUSES = {"completed", "cancelled"}


@router.post("/bulk-action")
async def bulk_run_action(
    payload: BulkRunActionBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Apply a lifecycle action (start/issue/complete/hold/resume/cancel) to many runs at once.
    Runs in a state that does not permit the action are skipped (not a hard error)."""
    action = payload.action
    if action not in _BULK_RUN_ACTIONS:
        raise HTTPException(status_code=422, detail=f"Unknown bulk action: {action}")
    states = await _all_item_states(session, company_id) if action in ("issue", "complete") else {}
    require_issued = (action == "complete" and bool(
        (await _mfg_settings(session, company_id)).get("require_issued_before_complete")))

    async def _emit(run_id: str, event_type: str, data: dict) -> None:
        await emit_event(session, company_id=company_id, entity_id=run_id, entity_type="mfg_order",
                         event_type=event_type, data=data, actor_id=user.id, location_id=None,
                         source="api", idempotency_key=str(uuid.uuid4()), metadata_={})

    done: list[str] = []
    skipped: list[dict] = []
    for run_id in payload.run_ids:
        try:
            row = await _get_order(session, company_id, run_id)
        except HTTPException:
            skipped.append({"id": run_id, "reason": "not found"})
            continue
        st = row.state or {}
        status = (st.get("status") or "").lower()
        try:
            if action in ("start", "hold", "cancel", "issue", "complete") and status in _CLOSED_RUN_STATUSES:
                raise ValueError("run is already closed")
            if action == "start":
                await _emit(run_id, "mfg.order.started", {"started_by": str(user.id)})
            elif action == "hold":
                await _emit(run_id, "mfg.order.on_hold", {"reason": None})
            elif action == "resume":
                if status != "on_hold":
                    raise ValueError("only an on-hold run can be resumed")
                await _emit(run_id, "mfg.order.resumed", {"resumed_by": str(user.id)})
            elif action == "cancel":
                await _emit(run_id, "mfg.order.cancelled", {})
            elif action == "issue":
                await _issue_and_record(session, company_id, user, run_id, _outstanding_inputs(st), states)
            elif action == "complete":
                outstanding = _outstanding_inputs(st)
                if outstanding:
                    if require_issued:
                        raise ValueError("components must be issued before completing (required by settings)")
                    await _issue_and_record(session, company_id, user, run_id, outstanding, states)
                    st = (await _get_order(session, company_id, run_id)).state
                qty = _outstanding_output(st)
                if qty > 0:
                    await _receive(session, company_id, user, run_id, st, qty, states)
                    st = (await _get_order(session, company_id, run_id)).state
                await _close_run(session, company_id, user, run_id, st, states)
            done.append(run_id)
        except ValueError as e:
            skipped.append({"id": run_id, "reason": str(e)})
    await session.commit()
    return {"done": done, "skipped": skipped}


def _run_makes(run_state: dict, item_id: str, item_sku: str) -> bool:
    """Does this run produce the given product? Match on output_item_id, else output SKU."""
    if run_state.get("output_item_id") == item_id:
        return True
    return any((o.get("sku") and o.get("sku") == item_sku) for o in run_state.get("expected_outputs", []))


@router.get("/items/{item_id}/hub")
async def item_manufacturing_hub(
    item_id: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The product Manufacturing-tab data: open demand for this product (which documents want it)
    + the production runs that make it. SKUs are resolved for human-readable display."""
    item = await session.get(Projection, {"company_id": company_id, "entity_id": item_id})
    if item is None or item.entity_type != "item":
        raise HTTPException(status_code=404, detail="Item not found")
    item_sku = (item.state or {}).get("sku")
    states = await _all_item_states(session, company_id)

    # Demand: open document lines for this product.
    docs = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type.in_(("doc", "list")),
        )
    )).scalars().all()
    demand = []
    for doc in docs:
        st = doc.state or {}
        if _skip_as_demand(st):
            continue
        for _idx, lid, _line_id, qty, _label in _doc_lines(st):
            if lid == item_id and qty > 0:
                demand.append({
                    "doc_id": doc.entity_id, "doc_number": st.get("ref_id") or doc.entity_id,
                    "doc_type": st.get("doc_type") or doc.entity_type,
                    "contact_name": st.get("contact_name") or "", "quantity": qty,
                    "due": st.get("due_date") or st.get("promised_date") or None,
                })

    # Runs that make this product, newest first, with input SKUs resolved.
    run_rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "mfg_order",
        )
    )).scalars().all()
    runs = []
    for r in run_rows:
        if not _run_makes(r.state or {}, item_id, item_sku):
            continue
        rs = r.state or {}
        inputs = [
            {**i, "sku": (states.get(i.get("item_id")) or {}).get("sku"),
             "name": (states.get(i.get("item_id")) or {}).get("name")}
            for i in rs.get("inputs", [])
        ]
        runs.append({**rs, "id": r.entity_id, "inputs": inputs,
                     "created_at": r.created_at.isoformat() if r.created_at else None})
    runs.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    # Coverage per demand line (Needed/Partial/Covered) from the FIFO pegging - shows at a glance
    # which orders still need making.
    my_row = next((r for r in await _compute_to_make(session, company_id) if r.get("item_id") == item_id), None)
    cov_by_doc = {d.get("doc_id"): d.get("coverage") for d in (my_row or {}).get("docs", [])}
    for d in demand:
        d["coverage"] = cov_by_doc.get(d.get("doc_id"))

    return {"demand": demand, "runs": runs}


# ---------------------------------------------------------------------------
# Import endpoints
# ---------------------------------------------------------------------------

@router.get("/import/template", response_class=PlainTextResponse, include_in_schema=False)
async def import_manufacturing_template():
    return PlainTextResponse(
        "entity_id,event_type,idempotency_key\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=manufacturing.csv"},
    )


@router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_manufacturing(
    body: MfgBatchImportRequest,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    from sqlalchemy import select as _select
    from celerp.models.ledger import LedgerEntry

    keys = [r.idempotency_key for r in body.records]
    existing_keys = set((await session.execute(
        _select(LedgerEntry.idempotency_key).where(LedgerEntry.idempotency_key.in_(keys))
    )).scalars().all())

    create_entity_ids = [r.entity_id for r in body.records if r.event_type == "mfg.order.created"]
    existing_entities: set[str] = set()
    if create_entity_ids:
        existing_entities = set((await session.execute(
            _select(Projection.entity_id).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(create_entity_ids),
            )
        )).scalars().all())

    created = skipped = 0
    errors: list[str] = []
    for rec in body.records:
        if rec.idempotency_key in existing_keys:
            skipped += 1
            continue
        if rec.event_type == "mfg.order.created" and rec.entity_id in existing_entities:
            skipped += 1
            continue
        try:
            await emit_event(
                session,
                company_id=company_id,
                entity_id=rec.entity_id,
                entity_type="mfg_order",
                event_type=rec.event_type,
                data=rec.data,
                actor_id=user.id,
                location_id=None,
                source=rec.source,
                idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing_keys.add(rec.idempotency_key)
            if rec.event_type == "mfg.order.created":
                existing_entities.add(rec.entity_id)
            created += 1
        except Exception as exc:
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: {exc}")

    await session.commit()
    return BatchImportResult(created=created, skipped=skipped, errors=errors)


# ---------------------------------------------------------------------------
# Manufacturing settings (stored under company.settings["manufacturing"])
# ---------------------------------------------------------------------------

async def _mfg_settings(session: AsyncSession, company_id) -> dict:
    company = await session.get(Company, company_id)
    return (company.settings or {}).get("manufacturing", {}) if company else {}


# ---------------------------------------------------------------------------
# Work Centers (relational master data, like Location) - registered before the
# catch-all GET /{order_id} so /work-centers is not swallowed by it.
# ---------------------------------------------------------------------------

DEFAULT_WORK_CENTER_NAME = "Default"


async def _default_hours_per_day(session: AsyncSession, company_id) -> float:
    """Working-day length from the company's default work center.

    Falls back to DEFAULT_HOURS_PER_DAY when the company has no default center
    or its value is unset/zero, so the board degrades to a neutral estimate
    rather than failing.
    """
    value = (await session.execute(
        select(WorkCenter.hours_per_day).where(
            WorkCenter.company_id == company_id, WorkCenter.is_default.is_(True),
        )
    )).scalars().first()
    return float(value or DEFAULT_HOURS_PER_DAY)


async def seed_default_work_center(session: AsyncSession, company_id) -> None:
    """Give a company its default work center, once.

    Shared by the on_company_created and on_modules_ready hooks and idempotent:
    a company that already has a default center (from either hook or the
    migration backfill) is left alone. The caller commits.
    """
    existing = (await session.execute(
        select(WorkCenter.id).where(
            WorkCenter.company_id == company_id, WorkCenter.is_default.is_(True),
        )
    )).scalars().first()
    if existing is not None:
        return
    session.add(WorkCenter(
        company_id=company_id, name=DEFAULT_WORK_CENTER_NAME,
        hours_per_day=DEFAULT_HOURS_PER_DAY, is_default=True,
    ))


async def provision_default_work_center_hook(*, session: AsyncSession, company_id) -> None:
    """on_company_created: seed the new company's default work center."""
    await seed_default_work_center(session, company_id)


async def backfill_default_work_center_hook(*, session: AsyncSession) -> None:
    """on_modules_ready: cover companies that enabled manufacturing after the
    migration ran, so they get a default center too."""
    company_ids = (await session.execute(select(Company.id))).scalars().all()
    for company_id in company_ids:
        await seed_default_work_center(session, company_id)


class WorkCenterCreate(BaseModel):
    name: str
    wip_location_id: str | None = None
    labor_rate: float | None = None
    capacity: float | None = None
    hours_per_day: float | None = None


class WorkCenterPatch(BaseModel):
    name: str | None = None
    wip_location_id: str | None = None
    labor_rate: float | None = None
    capacity: float | None = None
    hours_per_day: float | None = None
    is_default: bool | None = None


def _wc_dict(wc: WorkCenter) -> dict:
    return {
        "id": str(wc.id), "name": wc.name,
        "wip_location_id": str(wc.wip_location_id) if wc.wip_location_id else None,
        "labor_rate": wc.labor_rate, "capacity": wc.capacity,
        "hours_per_day": wc.hours_per_day, "is_default": bool(wc.is_default),
    }


def _clean_hours(value) -> float | None:
    """A working day is a positive number of hours. Zero, negative and unparsable
    values store as unset so the board falls back to DEFAULT_HOURS_PER_DAY rather
    than estimating against a nonsense day length."""
    if value is None:
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    return hours if hours > 0 else None


def _wc_conflict(exc: IntegrityError, name: str) -> HTTPException:
    """Tell the two work-center uniqueness violations apart, so the message names
    the actual problem rather than always blaming the name."""
    if "uq_work_center_one_default" in str(exc.orig):
        return HTTPException(status_code=409, detail="Another work center is already the default")
    return HTTPException(status_code=409, detail=f"A work center named '{name}' already exists")


async def _unset_other_defaults(session: AsyncSession, company_id, keep_id) -> None:
    """Clear the company's previous default so exactly one survives. Runs in the
    same transaction as the set, with the partial unique index as the backstop."""
    rows = (await session.execute(
        select(WorkCenter).where(
            WorkCenter.company_id == company_id,
            WorkCenter.is_default.is_(True),
            WorkCenter.id != keep_id,
        )
    )).scalars().all()
    for row in rows:
        row.is_default = False
    await session.flush()


def _parse_loc(value: str | None):
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        return None


@router.get("/work-centers")
async def list_work_centers(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (await session.execute(
        select(WorkCenter).where(WorkCenter.company_id == company_id).order_by(WorkCenter.name)
    )).scalars().all()
    return {"items": [_wc_dict(w) for w in rows], "total": len(rows)}


@router.post("/work-centers")
async def create_work_center(
    payload: WorkCenterCreate,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Work center name is required")
    # A company's first center becomes its default, so the board always has a
    # working-day length to read once any center exists.
    has_any = (await session.execute(
        select(WorkCenter.id).where(WorkCenter.company_id == company_id).limit(1)
    )).scalars().first()
    wc = WorkCenter(
        company_id=company_id, name=payload.name.strip(), wip_location_id=_parse_loc(payload.wip_location_id),
        labor_rate=payload.labor_rate, capacity=payload.capacity,
        hours_per_day=_clean_hours(payload.hours_per_day), is_default=has_any is None,
    )
    session.add(wc)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _wc_conflict(exc, payload.name.strip())
    return {"id": str(wc.id)}


@router.patch("/work-centers/{wc_id}")
async def patch_work_center(
    wc_id: str,
    payload: WorkCenterPatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    wc = await session.get(WorkCenter, _parse_loc(wc_id))
    if wc is None or wc.company_id != company_id:
        raise HTTPException(status_code=404, detail="Work center not found")
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields:
        if not (fields["name"] or "").strip():
            raise HTTPException(status_code=422, detail="Work center name is required")
        wc.name = fields["name"].strip()
    if "wip_location_id" in fields:
        wc.wip_location_id = _parse_loc(fields["wip_location_id"])
    if "labor_rate" in fields:
        wc.labor_rate = fields["labor_rate"]
    if "capacity" in fields:
        wc.capacity = fields["capacity"]
    if "hours_per_day" in fields:
        wc.hours_per_day = _clean_hours(fields["hours_per_day"])
    if fields.get("is_default"):
        await _unset_other_defaults(session, company_id, wc.id)
        wc.is_default = True
    # Read the name before committing: a rollback expires the instance, and
    # reloading it to build the error message would be IO in the error path.
    name = (wc.name or "").strip()
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _wc_conflict(exc, name)
    return {"ok": True}


@router.patch("/work-centers/{wc_id}/is_default")
async def set_default_work_center(
    wc_id: str,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Make this the company's default work center, clearing the previous one."""
    wc = await session.get(WorkCenter, _parse_loc(wc_id))
    if wc is None or wc.company_id != company_id:
        raise HTTPException(status_code=404, detail="Work center not found")
    await _unset_other_defaults(session, company_id, wc.id)
    wc.is_default = True
    name = wc.name or ""
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _wc_conflict(exc, name)
    return {"ok": True}


@router.delete("/work-centers/{wc_id}")
async def delete_work_center(
    wc_id: str,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    wc = await session.get(WorkCenter, _parse_loc(wc_id))
    if wc is None or wc.company_id != company_id:
        raise HTTPException(status_code=404, detail="Work center not found")
    others = (await session.execute(
        select(WorkCenter.id).where(
            WorkCenter.company_id == company_id, WorkCenter.id != wc.id,
        ).limit(1)
    )).scalars().first()
    if others is None:
        raise HTTPException(
            status_code=409,
            detail="This is your only work center. Add another before deleting this one.")
    if wc.is_default:
        raise HTTPException(
            status_code=409,
            detail="This is the default work center. Set another center as default before deleting it.")
    await session.delete(wc)
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Production-run execution (issue components / receive finished goods)
# ---------------------------------------------------------------------------

# Item statuses whose stock does not count toward a product's on-hand:
# sold/consumed/etc., plus draft (created but not yet committed to stock).
_INACTIVE_ITEM_STATUSES = frozenset({"sold", "memo_out", "archived", "merged", "expired", "draft"})


async def _reject_draft_item(session: AsyncSession, company_id, item_id: str, action: str) -> dict:
    """A draft isn't stock yet, so it cannot be consumed or produced into. Returns the
    item's state so the caller can reuse it instead of fetching twice."""
    row = await session.get(Projection, {"company_id": company_id, "entity_id": item_id})
    if row is None or row.entity_type != "item":
        raise HTTPException(status_code=404, detail=f"Item not found: {item_id}")
    if str((row.state or {}).get("status") or "").lower() == "draft":
        raise HTTPException(status_code=422, detail=f"Cannot {action} a draft item ({item_id}); make it available first.")
    return row.state


def _component_unit_cost(state: dict | None) -> float:
    """Standard unit cost of a component: cost_price if set, else cost_total / quantity."""
    if not state:
        return 0.0
    cp = state.get("cost_price")
    try:
        if cp is not None:
            return float(cp)
        total = state.get("cost_total")
        qty = float(state.get("quantity") or 0)
        if total is not None and qty > 0:
            return float(total) / qty
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def _run_input_cost(run_state: dict, states: dict[str, dict]) -> float:
    """Actual input cost of a run = sum(quantity issued x component unit cost), falling back to the
    planned required quantity for an input nothing has been issued against yet so a bare run still
    costs something. Feeds both the produced lot cost and the completion JE input relief, so the
    actuals propagate from this one edit."""
    total = 0.0
    for inp in run_state.get("inputs", []):
        unit = _component_unit_cost(states.get(inp.get("item_id")))
        issued = float(inp.get("issued_qty") or 0)
        qty = issued if issued > 0 else float(inp.get("quantity") or 0)
        total += qty * unit
    return round(total, 2)


# Namespace for deterministic produced-lot ids: a receipt re-submitted with the same idempotency
# key resolves to the same lot id, so the deduped event stream never mints a phantom second lot.
_MFG_LOT_NS = uuid.UUID("6f1d0c2a-7b3e-4a9c-8d5f-2e0a1b4c6d8e")


def _lot_qty_by_parent(states: dict[str, dict]) -> dict[str, float]:
    """Sum on-hand quantity of every lot (non-splittable produced entry) by its parent product id."""
    out: dict[str, float] = {}
    for st in states.values():
        pid = st.get("parent_item_id")
        if pid and str(st.get("status") or "available") not in _INACTIVE_ITEM_STATUSES:
            out[pid] = out.get(pid, 0.0) + float(st.get("quantity") or 0)
    return out


async def _consume_components(session: AsyncSession, company_id, user, order_id: str,
                              items: list[dict]) -> list[dict]:
    """Emit item.consumed for each issued component (the projection floors quantity at zero, which
    keeps made-to-order producible even when a component is briefly short). Returns what was issued."""
    consumed: list[dict] = []
    for it in items:
        item_id = it.get("item_id")
        qty = float(it.get("quantity") or 0)
        if not item_id or qty <= 0:
            continue
        await _reject_draft_item(session, company_id, item_id, "consume")
        await emit_event(
            session, company_id=company_id, entity_id=item_id, entity_type="item",
            event_type="item.consumed", data={"quantity_consumed": qty}, actor_id=user.id,
            location_id=None, source="api", idempotency_key=str(uuid.uuid4()),
            metadata_={"manufacturing_order_id": order_id},
        )
        consumed.append({"item_id": item_id, "quantity": qty})
    return consumed


async def _issue_and_record(session: AsyncSession, company_id, user, order_id: str,
                            items: list[dict], states: dict[str, dict]) -> list[dict]:
    """Consume the given components and record the issue on the run (auto-advances to In Progress)."""
    consumed = await _consume_components(session, company_id, user, order_id, items)
    if consumed:
        await emit_event(
            session, company_id=company_id, entity_id=order_id, entity_type="mfg_order",
            event_type="mfg.order.issued", data={"items": consumed, "issued_by": str(user.id)},
            actor_id=user.id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
    return consumed


async def _receive(session: AsyncSession, company_id, user, order_id: str, run_state: dict,
                   qty: float, states: dict[str, dict], idempotency_key: str | None = None) -> str | None:
    """Restock `qty` of the run's output as a new discrete lot and record the receipt on the run.

    Every receipt mints its own lot under the product, exactly like a received purchase, so a
    fungible product carries a true weighted-average cost across batches. The lot inherits the
    product's splittability (so a fungible product's lot stays partially sellable) and takes a
    provisional unit cost of issued-to-date input over received-to-date quantity; completion re-costs
    it to the final actual output cost. A key re-submitted with the same request resolves to the same
    lot id and deduped events, so a retry never mints a phantom second lot. Returns the lot id
    (or None if the run has no resolvable output product).
    """
    rk = idempotency_key or uuid.uuid4().hex
    out_id = run_state.get("output_item_id")
    product = states.get(out_id) if out_id else None
    if out_id and product is None:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": out_id})
        product = row.state if row is not None and row.entity_type == "item" else None

    lot_id: str | None = None
    if out_id and product is not None:
        if str(product.get("status") or "").lower() == "draft":
            raise HTTPException(status_code=422, detail=f"Cannot produce into a draft item ({out_id}); make it available first.")
        loc = product.get("location_id")
        received_after = float(run_state.get("received_qty") or 0) + qty
        unit_cost = _run_input_cost(run_state, states) / (received_after or 1)
        lot_id = f"item:{uuid.uuid5(_MFG_LOT_NS, f'{order_id}:{rk}')}"
        await emit_event(
            session, company_id=company_id, entity_id=lot_id, entity_type="item",
            event_type="item.created",
            data={
                "sku": product.get("sku"), "name": product.get("name"),
                "sell_by": product.get("sell_by"), "category": product.get("category"),
                "inventory_type": product.get("inventory_type"),
                "allow_splitting": bool(product.get("allow_splitting", True)),
                "quantity": 0, "location_id": loc, "parent_item_id": out_id, "lot": True,
                "manufacturing_order_id": order_id, "cost_total": round(unit_cost * qty, 2),
            },
            actor_id=user.id, location_id=loc, source="api",
            idempotency_key=f"mfg:{order_id}:receive:{rk}:created",
            metadata_={"manufacturing_order_id": order_id},
        )
        await emit_event(
            session, company_id=company_id, entity_id=lot_id, entity_type="item",
            event_type="item.produced", data={"quantity_produced": qty}, actor_id=user.id,
            location_id=loc, source="api",
            idempotency_key=f"mfg:{order_id}:receive:{rk}:produced",
            metadata_={"manufacturing_order_id": order_id},
        )

    await emit_event(
        session, company_id=company_id, entity_id=order_id, entity_type="mfg_order",
        event_type="mfg.order.received",
        data={"quantity": qty, "lot_item_id": lot_id, "received_by": str(user.id)},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=f"mfg:{order_id}:receive:{rk}:received", metadata_={},
    )
    return lot_id


async def _close_run(session: AsyncSession, company_id, user, order_id: str, run_state: dict,
                     states: dict[str, dict], payload: "CompleteBody | None" = None) -> None:
    """Close a run: record the actual yield, post the completion journal entry, and re-cost the run's
    lots to the final actual output cost so they reconcile exactly against that entry.

    Idempotent: the completion event, its journal entry, and every lot re-cost carry deterministic
    keys derived from the order id, so closing the same run twice dedups rather than double posting."""
    input_cost = _run_input_cost(run_state, states)
    waste = None
    waste_cost = 0.0
    if payload is not None and payload.waste_quantity is not None:
        waste = {"quantity": payload.waste_quantity, "unit": payload.waste_unit, "reason": payload.waste_reason}
        total_in = sum(float(i.get("quantity", 0) or 0) for i in run_state.get("inputs", [])) or 0.0
        if payload.waste_quantity and total_in > 0:
            # Clamp so waste can never exceed the input cost and drive output cost negative.
            waste_cost = min(input_cost * (float(payload.waste_quantity) / total_in), input_cost)
    if payload is not None and payload.actual_outputs is not None:
        actual_outputs = [o.model_dump() for o in payload.actual_outputs]
    else:
        # No explicit yield given: record what was actually received as the actual output.
        received = float(run_state.get("received_qty") or 0)
        expected = (run_state.get("expected_outputs") or [{}])[0]
        actual_outputs = [{**expected, "quantity": received}] if expected else []
    await emit_event(
        session, company_id=company_id, entity_id=order_id, entity_type="mfg_order",
        event_type="mfg.order.completed",
        data={
            "completed_by": str(user.id),
            "actual_outputs": actual_outputs,
            "waste": waste,
            "labor_hours": payload.labor_hours if payload is not None else None,
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=f"mfg:{order_id}:completed",
        metadata_={},
    )
    await auto_je.create_for_mfg_completed(
        session, company_id=company_id, user_id=user.id, order_id=order_id,
        input_cost=input_cost, waste_cost=waste_cost,
    )
    await _recost_run_lots(session, company_id, user, order_id, run_state, input_cost - waste_cost)


async def _recost_run_lots(session: AsyncSession, company_id, user, order_id: str, run_state: dict,
                           output_cost: float) -> None:
    """Restate every lot this run produced to a shared unit cost of output_cost / total received, so
    the sum of the lots' cost equals the completion entry's output leg under single or multiple
    receipts and with or without waste. Emits one deterministic item.cost_adjusted per lot."""
    total_received = float(run_state.get("received_qty") or 0)
    if total_received <= 0:
        return  # nothing received: no lot to re-cost, and never divide by a zero yield
    unit_cost = float(output_cost) / total_received
    fresh = await _all_item_states(session, company_id)
    for lot_id in run_state.get("received_lots") or []:
        lot = fresh.get(lot_id)
        if lot is None:
            continue
        new_total = round(unit_cost * float(lot.get("quantity") or 0), 2)
        await emit_event(
            session, company_id=company_id, entity_id=lot_id, entity_type="item",
            event_type="item.cost_adjusted",
            data={"cost_total": new_total, "manufacturing_order_id": order_id},
            actor_id=user.id, location_id=lot.get("location_id"), source="api",
            idempotency_key=f"mfg:{order_id}:recost:{lot_id}", metadata_={"manufacturing_order_id": order_id},
        )


# ---------------------------------------------------------------------------
# Manufacturing order endpoints
# ---------------------------------------------------------------------------

# Canonical run statuses. "incomplete" = everything still needing attention.
_RUN_STATUSES = ("planned", "in_progress", "on_hold", "completed", "cancelled")
_INCOMPLETE_STATUSES = frozenset({"planned", "in_progress", "on_hold"})


@router.get("")
async def list_orders(
    q: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List production runs, newest first. q matches the run id, description, source document
    (doc number) and output SKUs; `status` filters by canonical status or the pseudo-status
    "incomplete" (planned/in_progress/on_hold); dates filter on creation date.

    Runs are NOT auto-created from documents in the product-centric model; demand lives on the
    To-Make board (GET /manufacturing/to-make) and a run is created when you choose to produce."""
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "mfg_order",
        )
    )).scalars().all()
    items = [
        r.state | {"id": r.entity_id,
                   "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]
    if status:
        s = status.lower().strip()
        if s == "incomplete":
            items = [o for o in items if str(o.get("status") or "").lower() in _INCOMPLETE_STATUSES]
        else:
            items = [o for o in items if str(o.get("status") or "").lower() == s]
    if q:
        ql = q.lower().strip().strip(",")
        def _hay(o: dict) -> str:
            return " ".join([
                str(o.get("id", "")), str(o.get("description", "")), str(o.get("source_doc_id", "")),
                " ".join(str(x.get("sku", "")) for x in o.get("expected_outputs", [])),
            ]).lower()
        items = [o for o in items if ql in _hay(o)]
    if date_from:
        items = [o for o in items if (o.get("created_at") or "")[:10] >= date_from]
    if date_to:
        items = [o for o in items if (o.get("created_at") or "")[:10] <= date_to]
    items.sort(key=lambda o: o.get("created_at") or "", reverse=True)
    return {"items": items, "total": len(items)}


@router.post("")
async def create_order(
    payload: MfgOrderCreate,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not payload.description.strip():
        raise HTTPException(status_code=422, detail="description is required")
    if len(payload.inputs) == 0:
        raise HTTPException(status_code=409, detail="Cannot create/start order with no inputs")
    entity_id = f"mfg:{uuid.uuid4()}"
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="mfg_order",
        event_type="mfg.order.created",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=uuid.UUID(payload.location_id) if payload.location_id else None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "id": entity_id}


@router.get("/{order_id}")
async def get_order(
    order_id: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    return row.state | {"id": row.entity_id}


@router.post("/{order_id}/start")
async def start_order(
    order_id: str,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") == "completed":
        raise HTTPException(status_code=409, detail="Order already completed")
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=order_id,
        entity_type="mfg_order",
        event_type="mfg.order.started",
        data={"started_by": str(user.id)},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{order_id}/hold")
async def hold_order(
    order_id: str,
    payload: CancelBody | None = None,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Put an active run on hold (paused). Reversible via /resume."""
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Cannot hold a closed run")
    entry = await emit_event(
        session, company_id=company_id, entity_id=order_id, entity_type="mfg_order",
        event_type="mfg.order.on_hold", data={"reason": (payload.reason if payload else None)},
        actor_id=user.id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{order_id}/resume")
async def resume_order(
    order_id: str,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Resume an on-hold run (back to In Progress)."""
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") != "on_hold":
        raise HTTPException(status_code=409, detail="Only an on-hold run can be resumed")
    entry = await emit_event(
        session, company_id=company_id, entity_id=order_id, entity_type="mfg_order",
        event_type="mfg.order.resumed", data={"resumed_by": str(user.id)},
        actor_id=user.id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{order_id}/schedule")
async def schedule_order(
    order_id: str,
    payload: ScheduleBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set scheduling fields (due date / planned start / priority) on a run. Only provided keys are
    written; a blank value clears that field. A closed run cannot be rescheduled."""
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Cannot reschedule a closed run")
    data = payload.model_dump(exclude_unset=True, exclude={"idempotency_key"})
    if not data:
        raise HTTPException(status_code=422, detail="No scheduling fields provided")
    entry = await emit_event(
        session, company_id=company_id, entity_id=order_id, entity_type="mfg_order",
        event_type="mfg.order.scheduled", data=data, actor_id=user.id, location_id=None,
        source="api", idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


def _outstanding_inputs(run_state: dict) -> list[dict]:
    """Per input, the quantity still to issue (required - already issued)."""
    out = []
    for inp in run_state.get("inputs", []):
        rem = float(inp.get("quantity") or 0) - float(inp.get("issued_qty") or 0)
        if rem > 1e-9:
            out.append({"item_id": inp.get("item_id"), "quantity": round(rem, 6)})
    return out


def _outstanding_output(run_state: dict) -> float:
    """Finished-goods quantity still to receive (expected - already received)."""
    expected = float((run_state.get("expected_outputs") or [{}])[0].get("quantity") or 0)
    return max(0.0, expected - float(run_state.get("received_qty") or 0))


@router.post("/{order_id}/issue")
async def issue_order(
    order_id: str,
    payload: IssueBody | None = None,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Issue components from stock into a run (decrements them). Partial issues are allowed; omitting
    `items` issues everything still outstanding. Issuing auto-advances a planned run to In Progress."""
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Cannot issue to a closed run")
    states = await _all_item_states(session, company_id)
    items = ([{"item_id": i.item_id, "quantity": i.quantity} for i in payload.items]
             if (payload and payload.items) else _outstanding_inputs(row.state))
    consumed = await _issue_and_record(session, company_id, user, order_id, items, states)
    await session.commit()
    return {"issued": consumed}


@router.post("/{order_id}/receive")
async def receive_order(
    order_id: str,
    payload: ReceiveBody | None = None,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Receive finished goods from a run as a discrete lot under the product. Omitting
    `quantity` receives everything still outstanding; once fully received the run auto-completes."""
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Cannot receive into a closed run")
    qty = (payload.quantity if (payload and payload.quantity is not None)
           else _outstanding_output(row.state))
    if qty <= 0:
        raise HTTPException(status_code=422, detail="Receive quantity must be greater than zero")
    states = await _all_item_states(session, company_id)
    lot_id = await _receive(session, company_id, user, order_id, row.state, qty, states,
                            idempotency_key=(payload.idempotency_key if payload else None))
    # Auto-complete once the full expected output has been received.
    row = await _get_order(session, company_id, order_id)
    if _outstanding_output(row.state) <= 1e-9:
        await _close_run(session, company_id, user, order_id, row.state, states)
    await session.commit()
    return {"received": qty, "lot_item_id": lot_id}


@router.post("/{order_id}/complete")
async def complete_order(
    order_id: str,
    payload: CompleteBody | None = None,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Finish a run now: issue any outstanding components, receive all outstanding output, and close.

    This is the one-tap close used by the product hub's Complete action; the output lands as a
    discrete lot under the product, never a nameless throwaway item.
    """
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Cannot complete a closed run")
    states = await _all_item_states(session, company_id)
    outstanding = _outstanding_inputs(row.state)
    if outstanding:
        # When the company requires components issued first, completing must not silently auto-issue.
        if (await _mfg_settings(session, company_id)).get("require_issued_before_complete"):
            raise HTTPException(status_code=409,
                                detail="Issue all components before completing this run (required by settings)")
        await _issue_and_record(session, company_id, user, order_id, outstanding, states)
        row = await _get_order(session, company_id, order_id)
    qty = _outstanding_output(row.state)
    if qty > 0:
        await _receive(session, company_id, user, order_id, row.state, qty, states)
        row = await _get_order(session, company_id, order_id)
    await _close_run(session, company_id, user, order_id, row.state, states, payload)
    await session.commit()
    return {"status": "completed"}


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    payload: CancelBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_manufacturing"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Cannot cancel a closed run")
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=order_id,
        entity_type="mfg_order",
        event_type="mfg.order.cancelled",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

def setup_api_routes(app) -> None:
    """Called by the module loader to register manufacturing routes."""
    app.include_router(router)
