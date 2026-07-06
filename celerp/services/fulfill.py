# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Fulfill and un-fulfill execution — emits events, creates JEs.

Used by core for data-integrity reversals (void, revert, unvoid)
and by the fulfillment module's toggle/pick screen.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.pick import PickResult
from celerp.services.units import is_non_stock_line

# Doc types where COGS must NOT be recognized at fulfillment time.
# consignment_in: goods not owned; COGS when vendor bill settled.
# memo: goods still nominally owned; COGS recognized when invoice is issued.
# bill: inbound; parcels created at receive time, fulfill-lines is blocked for these.
_NO_COGS_DOC_TYPES = frozenset({"bill", "consignment_in", "memo"})


def _to_uuid(val) -> _uuid.UUID:
    """Coerce str or UUID to UUID."""
    return val if isinstance(val, _uuid.UUID) else _uuid.UUID(str(val))


async def execute_fulfill(
    session: AsyncSession,
    *,
    doc_entity_id: str,
    doc_state: dict,
    pick_result: PickResult,
    company_id,
    user_id,
    doc_type: str = "",
) -> dict[str, Any]:
    """Execute fulfillment: emit item events, doc event, and COGS JE.

    Returns: {fulfillment_status, fulfilled_items, total_cogs}
    """
    now = datetime.now(UTC).isoformat()
    fulfilled_items: list[dict] = []
    total_cogs = 0.0
    cid = _to_uuid(company_id)
    uid = _to_uuid(user_id)
    # Stable human doc number for the item's history (the entity_id suffix can diverge on
    # finalize/renumber, e.g. PF-... -> INV-...), so capture it from the doc at emit time.
    doc_number = doc_state.get("doc_number") or doc_state.get("ref_id") or ""

    for pick in pick_result.picks:
        if pick.action == "full":
            await emit_event(
                session,
                company_id=cid,
                entity_id=pick.item_id,
                entity_type="item",
                event_type="item.fulfilled",
                data={
                    "source_doc_id": doc_entity_id,
                    "doc_number": doc_number,
                    "quantity_fulfilled": pick.pick_qty,
                    "fulfilled_by": str(uid),
                    "doc_type": doc_type,
                },
                actor_id=uid,
                location_id=None,
                source="fulfillment",
                idempotency_key=str(_uuid.uuid4()),
                metadata_={"doc_id": doc_entity_id},
            )
            fulfilled_items.append({
                "item_id": pick.item_id,
                "sku": pick.sku,
                "quantity": pick.pick_qty,
                "action": "full",
                "fulfilled_at": now,
            })
        elif pick.action == "split":
            child_eid = f"item:{_uuid.uuid4()}"
            await emit_event(
                session,
                company_id=cid,
                entity_id=child_eid,
                entity_type="item",
                event_type="item.created",
                data={
                    "sku": pick.sku,   # child keeps the parent SKU (distinct lot by entity_id)
                    "name": pick.sku,
                    "quantity": pick.pick_qty,
                },
                actor_id=uid,
                location_id=None,
                source="fulfillment",
                idempotency_key=str(_uuid.uuid4()),
                metadata_={"parent_id": pick.item_id, "split_for_fulfillment": True},
            )
            # Reduce parent quantity
            parent = await session.get(Projection, {"company_id": cid, "entity_id": pick.item_id})
            parent_qty = float(parent.state.get("quantity", 0)) if parent else 0
            new_parent_qty = max(0.0, parent_qty - pick.pick_qty)
            await emit_event(
                session,
                company_id=cid,
                entity_id=pick.item_id,
                entity_type="item",
                event_type="item.quantity.adjusted",
                data={"new_qty": new_parent_qty},
                actor_id=uid,
                location_id=None,
                source="fulfillment",
                idempotency_key=str(_uuid.uuid4()),
                metadata_={"split_for_fulfillment": True},
            )
            # Fulfill the child
            await emit_event(
                session,
                company_id=cid,
                entity_id=child_eid,
                entity_type="item",
                event_type="item.fulfilled",
                data={
                    "source_doc_id": doc_entity_id,
                    "doc_number": doc_number,
                    "quantity_fulfilled": pick.pick_qty,
                    "fulfilled_by": str(uid),
                    "doc_type": doc_type,
                },
                actor_id=uid,
                location_id=None,
                source="fulfillment",
                idempotency_key=str(_uuid.uuid4()),
                metadata_={"doc_id": doc_entity_id},
            )
            fulfilled_items.append({
                "item_id": child_eid,
                "sku": pick.sku,
                "quantity": pick.pick_qty,
                "action": "split",
                "split_from": pick.item_id,
                "fulfilled_at": now,
            })

        total_cogs += pick.pick_qty * pick.cost_price

    # Non-stock lines (service or freight charge): auto-mark fulfilled (no physical pick).
    # Detected by sell_by being a service unit OR the referenced item being a non-stock type.
    for line in doc_state.get("line_items", []):
        sell_by = line.get("sell_by") or ""
        item_id = line.get("item_id")
        inv_type = None
        if item_id:
            item_proj = await session.get(Projection, {"company_id": cid, "entity_id": item_id})
            if item_proj:
                inv_type = item_proj.state.get("inventory_type")
        if is_non_stock_line(inv_type, sell_by):
            fulfilled_items.append({
                "item_id": None,
                "sku": line.get("sku", ""),
                "quantity": float(line.get("quantity", 0)),
                "action": "service",
                "fulfilled_at": now,
            })

    # Determine fulfillment status based on pick plan.
    if pick_result.unfulfilled:
        fulfillment_status = "partial"
        await emit_event(
            session,
            company_id=cid,
            entity_id=doc_entity_id,
            entity_type="doc",
            event_type="doc.partially_fulfilled",
            data={
                "fulfilled_items": fulfilled_items,
                "unfulfilled_items": pick_result.unfulfilled,
                "fulfilled_by": str(uid),
                "fulfilled_at": now,
                "strategy": pick_result.strategy,
            },
            actor_id=uid,
            location_id=None,
            source="fulfillment",
            idempotency_key=str(_uuid.uuid4()),
            metadata_={},
        )
    else:
        fulfillment_status = "fulfilled"
        await emit_event(
            session,
            company_id=cid,
            entity_id=doc_entity_id,
            entity_type="doc",
            event_type="doc.fulfilled",
            data={
                "fulfilled_items": fulfilled_items,
                "fulfilled_by": str(uid),
                "fulfilled_at": now,
                "strategy": pick_result.strategy,
                "total_cogs": total_cogs,
            },
            actor_id=uid,
            location_id=None,
            source="fulfillment",
            idempotency_key=str(_uuid.uuid4()),
            metadata_={},
        )

    # COGS journal entry: skip for inbound docs and memos.
    # Memo COGS is recognized when the memo converts to an invoice.
    je_cogs = 0.0 if doc_type in _NO_COGS_DOC_TYPES else total_cogs
    if je_cogs > 0:
        await auto_je.create_for_doc_fulfilled(
            session, company_id=cid, user_id=uid,
            doc_id=doc_entity_id, total_cogs=je_cogs,
        )

    return {
        "fulfillment_status": fulfillment_status,
        "fulfilled_items": fulfilled_items,
        "total_cogs": je_cogs,
    }


async def execute_unfulfill(
    session: AsyncSession,
    *,
    doc_entity_id: str,
    doc_state: dict,
    company_id,
    user_id,
    reason: str = "manual",
    doc_type: str = "",
) -> dict[str, Any]:
    """Reverse fulfillment: restore item quantities, emit reversal events, reverse COGS JE.

    Returns: {success: bool, reversed_items: [...]}
    """
    fulfilled_items = doc_state.get("fulfilled_items", [])
    cid = _to_uuid(company_id)
    uid = _to_uuid(user_id)
    doc_number = doc_state.get("doc_number") or doc_state.get("ref_id") or ""
    reversed_items: list[dict] = []

    if not fulfilled_items:
        # No items to reverse - still emit the doc event to clear fulfillment_status
        await emit_event(
            session,
            company_id=cid,
            entity_id=doc_entity_id,
            entity_type="doc",
            event_type="doc.fulfillment_reversed",
            data={
                "reversed_items": [],
                "reversed_by": str(uid),
                "reason": reason,
            },
            actor_id=uid,
            location_id=None,
            source="fulfillment",
            idempotency_key=str(_uuid.uuid4()),
            metadata_={},
        )
        return {"success": True, "reversed_items": []}

    for fi in fulfilled_items:
        item_id = fi.get("item_id")
        action = fi.get("action", "full")

        # Inbound and service items have no physical stock to restore.
        if not item_id or action in ("service", "inbound"):
            reversed_items.append({
                "item_id": None,
                "sku": fi.get("sku", ""),
                "quantity": fi.get("quantity", 0),
                "action": action,
            })
            continue

        qty = float(fi.get("quantity", 0))
        await emit_event(
            session,
            company_id=cid,
            entity_id=item_id,
            entity_type="item",
            event_type="item.fulfillment_reversed",
            data={
                "source_doc_id": doc_entity_id,
                "doc_number": doc_number,
                "quantity_restored": qty,
                "reversed_by": str(uid),
                "reason": reason,
                "doc_type": doc_type,
            },
            actor_id=uid,
            location_id=None,
            source="fulfillment",
            idempotency_key=str(_uuid.uuid4()),
            metadata_={"doc_id": doc_entity_id},
        )
        reversed_items.append({
            "item_id": item_id,
            "sku": fi.get("sku", ""),
            "quantity": qty,
            "action": action,
        })

    # Emit doc.fulfillment_reversed
    await emit_event(
        session,
        company_id=cid,
        entity_id=doc_entity_id,
        entity_type="doc",
        event_type="doc.fulfillment_reversed",
        data={
            "reversed_items": reversed_items,
            "reversed_by": str(uid),
            "reason": reason,
        },
        actor_id=uid,
        location_id=None,
        source="fulfillment",
        idempotency_key=str(_uuid.uuid4()),
        metadata_={},
    )

    # Reverse COGS JE (only if there were outbound items; inbound has no COGS)
    has_outbound = any(fi.get("action") not in ("service", "inbound") for fi in fulfilled_items if fi.get("item_id"))
    if has_outbound:
        await auto_je.void_for_doc_fulfilled(
            session, company_id=cid, user_id=uid, doc_id=doc_entity_id,
        )

    return {"success": True, "reversed_items": reversed_items}
