# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-manufacturing API routes.

Registered into the FastAPI app by the module loader via setup_api_routes().
All routes are mounted under /manufacturing (set in PLUGIN_MANIFEST or by the
loader's register_api_routes calling setup_api_routes with the app directly).
"""
from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.events.schemas import RecipeSpec
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.auth import get_current_company_id, get_current_user

from .costing import RecipeError, roll_up_cost, where_used
from .expansion import expand_recipe, explode_demand, is_manufacturable, mfg_idem_key
from .labor import apply_labor_providers

router = APIRouter(prefix="/manufacturing", dependencies=[Depends(get_current_user)], tags=["manufacturing"])


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


class ConsumeBody(BaseModel):
    item_id: str
    quantity: float
    idempotency_key: str | None = None


class StepBody(BaseModel):
    step_id: str
    notes: str | None = None
    idempotency_key: str | None = None


class CompleteBody(BaseModel):
    actual_outputs: list[MfgOutput] | None = None
    waste_quantity: float | None = None
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
    idempotency_key: str | None = None


@router.post("/items/{item_id}/build")
async def build_item(
    item_id: str,
    payload: BuildBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a manufacturing order to build N of a manufacturable item — inputs expand from its recipe."""
    item = await session.get(Projection, {"company_id": company_id, "entity_id": item_id})
    if item is None or item.entity_type != "item":
        raise HTTPException(status_code=404, detail="Item not found")
    if not is_manufacturable(item.state):
        raise HTTPException(status_code=422, detail="Item has no recipe to build from")
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
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
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

def _order_id_for(doc_id: str, line_id: str, cycle: int) -> str:
    """Deterministic order entity_id for a (document, line, fulfill-cycle) — idempotent re-runs."""
    h = hashlib.sha1(f"{doc_id}|{line_id}|{cycle}".encode()).hexdigest()[:20]
    return f"mfg:{h}"


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


async def _get_document(session: AsyncSession, company_id, doc_id: str) -> Projection:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
    if row is None or row.entity_type not in ("doc", "list"):
        raise HTTPException(status_code=404, detail="Document not found")
    return row


# Document statuses whose lines no longer drive production.
_CLOSED_DOC_STATUSES = {"void", "cancelled", "converted", "expired"}


async def _sync_doc_orders(session: AsyncSession, company_id, user, doc: Projection,
                           states: dict[str, dict]) -> list[str]:
    """Ensure one manufacturing order exists per recipe-bearing line of one document.

    Orders are created automatically from open sales documents (owner decision 2026-06-12,
    superseding the earlier manual-click flow). Deterministic order ids + idempotency keys
    per (doc, line, fulfill-cycle) make this safe to run on every read; a cancelled order's
    projection still exists, so cancelling never resurrects. Returns created order ids.
    """
    st = doc.state or {}
    if (st.get("status") or "") in _CLOSED_DOC_STATUSES:
        return []
    cycle = int(st.get("fulfill_cycle", 0) or 0)
    ref = st.get("ref_id") or doc.entity_id
    created: list[str] = []
    for _idx, item_id, line_id, qty, _label in _doc_lines(st):
        if not item_id or qty <= 0:
            continue
        ist = states.get(item_id)
        if not is_manufacturable(ist):
            continue
        order_id = _order_id_for(doc.entity_id, line_id, cycle)
        if await session.get(Projection, {"company_id": company_id, "entity_id": order_id}) is not None:
            continue
        inputs, outputs = expand_recipe(ist, qty)
        await emit_event(
            session, company_id=company_id, entity_id=order_id, entity_type="mfg_order",
            event_type="mfg.order.created",
            data={
                "description": f"Build {qty:g} x {ist.get('sku', '')} (from {ref})",
                "order_type": "assembly", "inputs": inputs, "expected_outputs": outputs,
                "source_doc_id": doc.entity_id, "source_doc_type": doc.entity_type, "source_line_id": line_id,
            },
            actor_id=user.id, location_id=None, source="auto",
            idempotency_key=mfg_idem_key(doc.entity_id, line_id, cycle), metadata_={},
        )
        created.append(order_id)
    return created


async def _ensure_orders_for_open_docs(session: AsyncSession, company_id, user) -> int:
    """Run _sync_doc_orders across every open sales document; returns count created."""
    states = await _all_item_states(session, company_id)
    docs = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type.in_(("doc", "list")),
        )
    )).scalars().all()
    created = 0
    for doc in docs:
        created += len(await _sync_doc_orders(session, company_id, user, doc, states))
    if created:
        await session.commit()
    return created


@router.get("/documents/{doc_id}/components-summary")
async def document_components_summary(
    doc_id: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Aggregated, recursively-exploded component demand across all manufacturable lines (JIT)."""
    doc = await _get_document(session, company_id, doc_id)
    states = await _all_item_states(session, company_id)
    lines = [(item_id, qty) for _, item_id, _, qty, _ in _doc_lines(doc.state) if item_id and qty > 0]
    demand = explode_demand(lines, states.get)

    def _detail(d: dict[str, float]) -> list[dict]:
        return [
            {"item_id": iid, "sku": (states.get(iid) or {}).get("sku"),
             "name": (states.get(iid) or {}).get("name"), "quantity": q}
            for iid, q in sorted(d.items(), key=lambda kv: (states.get(kv[0]) or {}).get("sku") or kv[0])
        ]

    return {"sub_assemblies": _detail(demand["sub_assemblies"]), "raw_materials": _detail(demand["raw_materials"])}


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
# Manufacturing order endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_orders(
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List manufacturing orders, newest first. q matches the order id, description,
    source document (doc number) and output SKUs; dates filter on creation date.

    Orders for open sales documents are ensured (auto-created, idempotently) before
    listing, so the queue always reflects current demand without a manual step."""
    await _ensure_orders_for_open_docs(session, company_id, user)
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


@router.post("/{order_id}/consume")
async def consume_input(
    order_id: str,
    payload: ConsumeBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Cannot consume for closed order")

    item = await session.get(Projection, {"company_id": company_id, "entity_id": payload.item_id})
    if item is None or item.entity_type != "item":
        raise HTTPException(status_code=404, detail="Input item not found")
    available = float(item.state.get("quantity", 0) or 0)
    reserved = float(item.state.get("reserved_quantity", 0) or 0)
    if payload.quantity > max(0.0, available - reserved) + 1e-9:
        raise HTTPException(status_code=409, detail="Cannot consume more than available quantity")

    item_ev = await emit_event(
        session,
        company_id=company_id,
        entity_id=payload.item_id,
        entity_type="item",
        event_type="item.consumed",
        data={"quantity_consumed": payload.quantity},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={"manufacturing_order_id": order_id},
    )
    await emit_event(
        session,
        company_id=company_id,
        entity_id=order_id,
        entity_type="mfg_order",
        event_type="mfg.step.completed",
        data={"step_id": f"consume:{payload.item_id}", "notes": f"qty={payload.quantity}"},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": item_ev.id}


@router.post("/{order_id}/step")
async def complete_step(
    order_id: str,
    payload: StepBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _get_order(session, company_id, order_id)
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=order_id,
        entity_type="mfg_order",
        event_type="mfg.step.completed",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{order_id}/complete")
async def complete_order(
    order_id: str,
    payload: CompleteBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    state = row.state
    if state.get("status") == "completed":
        raise HTTPException(status_code=409, detail="Cannot complete an order twice")

    consumed_item_ids = set()
    for step in state.get("steps_completed", []):
        if isinstance(step, str) and step.startswith("consume:"):
            consumed_item_ids.add(step.split(":", 1)[1])
    required_item_ids = {x.get("item_id") for x in state.get("inputs", []) if x.get("item_id")}
    if required_item_ids and not required_item_ids.issubset(consumed_item_ids):
        raise HTTPException(status_code=409, detail="Cannot complete order without consuming all inputs")

    outputs = payload.actual_outputs or [MfgOutput(**o) for o in state.get("expected_outputs", [])]
    for out in outputs:
        new_item_id = f"item:{uuid.uuid4()}"
        await emit_event(
            session,
            company_id=company_id,
            entity_id=new_item_id,
            entity_type="item",
            event_type="item.created",
            data={
                "sku": out.sku,
                "name": out.name,
                "quantity": 0,
                "category": out.category,
                "location_id": state.get("location_id"),
                "manufacturing_order_id": order_id,
            },
            actor_id=user.id,
            location_id=state.get("location_id"),
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"manufacturing_order_id": order_id},
        )
        await emit_event(
            session,
            company_id=company_id,
            entity_id=new_item_id,
            entity_type="item",
            event_type="item.produced",
            data={"quantity_produced": out.quantity},
            actor_id=user.id,
            location_id=state.get("location_id"),
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"manufacturing_order_id": order_id},
        )

    mfg_entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=order_id,
        entity_type="mfg_order",
        event_type="mfg.order.completed",
        data={
            "completed_by": str(user.id),
            "actual_outputs": [o.model_dump(exclude_none=True) for o in outputs],
            "waste": (
                {"quantity": payload.waste_quantity, "unit": payload.waste_unit, "reason": payload.waste_reason}
                if payload.waste_quantity is not None else None
            ),
            "labor_hours": payload.labor_hours,
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )

    input_cost = float(state.get("estimated_cost", 0) or 0)
    waste_cost = 0.0
    if payload.waste_quantity and payload.waste_quantity > 0:
        total_input_qty = sum(float(i.get("quantity", 0) or 0) for i in state.get("inputs", [])) or 0.0
        if total_input_qty > 0:
            waste_cost = input_cost * (float(payload.waste_quantity) / total_input_qty)

    await auto_je.create_for_mfg_completed(
        session,
        company_id=company_id,
        user_id=user.id,
        order_id=order_id,
        input_cost=input_cost,
        waste_cost=waste_cost,
    )
    await session.commit()
    return {"event_id": mfg_entry.id}


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    payload: CancelBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") == "completed":
        raise HTTPException(status_code=409, detail="Cannot cancel completed order")
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
