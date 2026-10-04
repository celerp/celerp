# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Fulfill and un-fulfill execution — emits events, creates JEs.

Used by core for data-integrity reversals (void, revert, unvoid)
and by the fulfillment module's toggle/pick screen.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import LOT_ACCOUNT_FIELD
from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.business_time import business_date_at
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
    allocate_barcodes: Callable[[int], Awaitable[list[str]]] | None = None,
) -> dict[str, Any]:
    """Execute fulfillment: emit item events, doc event, and COGS JE.

    Returns: {fulfillment_status, fulfilled_items, total_cogs}

    A split pick carves a new child parcel off its parent lot; that child is a
    fresh physical item and needs its own barcode. Core must not depend on the
    inventory module, so the calling module injects allocation through
    ``allocate_barcodes(count) -> list[str]`` (celerp_inventory allocates under
    its code-namespace lock). When any split is planned and no allocator is
    supplied, this fails before emitting a single event rather than minting a
    barcodeless child.
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    fulfilled_items: list[dict] = []
    total_cogs = sum(p.pick_qty * p.cost_price for p in pick_result.picks)
    cid = _to_uuid(company_id)
    uid = _to_uuid(user_id)

    # Resolve accounting time only when this fulfillment will actually post COGS.
    # Do it before barcode allocation or event emission so an invalid configured
    # timezone cannot leave any fulfillment side effect behind.
    fulfillment_date = None
    if doc_type not in _NO_COGS_DOC_TYPES and total_cogs > 0:
        company = await session.get(Company, cid)
        company_timezone = (company.settings or {}).get("timezone") if company else None
        fulfillment_date = business_date_at(now_dt, company_timezone)

    # Allocate every split child's barcode up front, under the caller's lock, and
    # fail clearly before any event is emitted if the allocator is missing.
    split_picks = [p for p in pick_result.picks if p.action == "split"]
    if split_picks and allocate_barcodes is None:
        raise ValueError(
            "Fulfillment must split a lot into a new child parcel that needs a "
            "freshly allocated barcode, but no allocate_barcodes callback was "
            "supplied. The calling module must inject inventory code allocation."
        )
    split_barcodes = iter(await allocate_barcodes(len(split_picks))) if split_picks else iter(())
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
            parent = await session.get(Projection, {"company_id": cid, "entity_id": pick.item_id})
            parent_state = parent.state if parent else {}
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
                    "barcode": next(split_barcodes),   # fresh per-lot barcode from the injected allocator
                    # The part carries its share of the lot's cost on the lot's account,
                    # recorded or not, so a return puts it back where its value sits.
                    "cost_total": pick.pick_qty * pick.cost_price,
                    LOT_ACCOUNT_FIELD: parent_state.get(LOT_ACCOUNT_FIELD),
                },
                actor_id=uid,
                location_id=None,
                source="fulfillment",
                idempotency_key=str(_uuid.uuid4()),
                metadata_={"parent_id": pick.item_id, "split_for_fulfillment": True},
            )
            # Reduce parent quantity
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
    lot_costs: dict[str, float] = {}
    for p in pick_result.picks:
        lot_costs[p.item_id] = lot_costs.get(p.item_id, 0.0) + p.pick_qty * p.cost_price
    if je_cogs > 0:
        await auto_je.create_for_doc_fulfilled(
            session, company_id=cid, user_id=uid,
            doc_id=doc_entity_id, lot_costs=lot_costs,
            ts=fulfillment_date,
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


@dataclass(frozen=True)
class OutstandingLine:
    """A physical stock line of a document: what it ordered and what it has received."""
    index: int
    item_id: str
    ordered: float
    fulfilled: float

    @property
    def outstanding(self) -> float:
        return max(0.0, self.ordered - self.fulfilled)


async def outstanding_physical_lines(session: AsyncSession, company_id, docs: list[Projection]) -> dict[str, list[OutstandingLine]]:
    """For each document, its physical stock lines with what each ordered and what has been
    sent against it and not taken back. Service, freight and other non-stock lines, and lines
    naming no item, are left out; document lines are never changed.

    What is sent is read from the fulfillment record, as the invoice's cost of sales reads
    it: each lot whose latest fulfillment event for the document ships it counts at its
    quantity, on the line it belongs to (auto_je.line_of_lot), so a reversal asks for the
    goods again. Goods an invoice bills from a memo were sent under the memo, so the memo's
    record counts for the invoice. A lot no line can claim is put on the document's lines
    of its SKU in order, each taking at most what it ordered."""
    from sqlalchemy import select

    from celerp.models.ledger import LedgerEntry

    docs = [d for d in docs if d is not None]
    if not docs:
        return {}
    cid = _to_uuid(company_id)
    # The document whose deliveries a fulfillment event records: its own, or the invoice a
    # memo was billed on.
    owner: dict[str, str] = {d.entity_id: d.entity_id for d in docs}
    for d in docs:
        memo = (d.state or {}).get("source_memo_id")
        if memo:
            owner[memo] = d.entity_id
    events = (await session.execute(
        select(LedgerEntry).where(
            LedgerEntry.company_id == cid,
            LedgerEntry.entity_type == "item",
            LedgerEntry.event_type.in_(("item.fulfilled", "item.fulfillment_reversed")),
            LedgerEntry.data["source_doc_id"].as_string().in_(list(owner)),
        ).order_by(LedgerEntry.id)
    )).scalars().all()
    latest: dict[tuple[str, str], LedgerEntry] = {}  # (document, lot) -> latest event
    recorded: dict[tuple[str, str], int | None] = {}  # (document, lot) -> line its latest fulfillment named
    for e in events:
        source = (e.data or {}).get("source_doc_id")
        key = (owner[source], e.entity_id)
        latest[key] = e
        if e.event_type == "item.fulfilled":
            # A memo's line numbers are the memo's, not the invoice's.
            recorded[key] = auto_je.recorded_line_index(e) if source == owner[source] else None
    out_lots = {key: e for key, e in latest.items() if e.event_type == "item.fulfilled"}

    wanted = {lot for _doc, lot in out_lots}
    for d in docs:
        for li in (d.state or {}).get("line_items") or []:
            if li.get("entity_id") or li.get("item_id"):
                wanted.add(li.get("entity_id") or li.get("item_id"))
    items = {r.entity_id: (r.state or {}) for r in (await session.execute(
        select(Projection).where(Projection.company_id == cid, Projection.entity_id.in_(list(wanted)))
    )).scalars().all()} if wanted else {}

    result: dict[str, list[OutstandingLine]] = {}
    for d in docs:
        line_items = (d.state or {}).get("line_items") or []
        physical: dict[int, str] = {}
        for idx, li in enumerate(line_items):
            item_id = li.get("entity_id") or li.get("item_id")
            st = items.get(item_id) if item_id else None
            if st is not None and not is_non_stock_line(st.get("inventory_type"), st.get("sell_by")):
                physical[idx] = item_id
        ordered = {idx: float(line_items[idx].get("quantity") or 0) for idx in physical}
        sent = dict.fromkeys(physical, 0.0)
        unclaimed: list[tuple[str, float]] = []
        for (doc_id, lot), e in out_lots.items():
            if doc_id != d.entity_id:
                continue
            lot_state = items.get(lot)
            qty = float((lot_state or {}).get("quantity") or (e.data or {}).get("quantity_fulfilled") or 0)
            idx = auto_je.line_of_lot(line_items, lot, lot_state or {}, recorded.get((doc_id, lot)))
            if idx in sent:
                sent[idx] += qty
            else:
                unclaimed.append((str((lot_state or {}).get("sku") or "").strip(), qty))
        for sku, qty in unclaimed:
            for idx in physical:
                if qty <= 1e-9:
                    break
                if str(line_items[idx].get("sku") or "").strip() != sku:
                    continue
                take = min(qty, max(0.0, ordered[idx] - sent[idx]))
                sent[idx] += take
                qty -= take
        result[d.entity_id] = [
            OutstandingLine(index=idx, item_id=item_id, ordered=ordered[idx], fulfilled=min(sent[idx], ordered[idx]))
            for idx, item_id in physical.items()
        ]
    return result
