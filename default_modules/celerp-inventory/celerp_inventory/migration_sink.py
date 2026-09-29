# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Migration sink for items and inventory positions.

Items are written by the same writer as the item batch import route; opening
positions and adjustments by the same quantity service as the item adjust route.
Item quantities never post to the ledger: the source's inventory value arrives
through its journals.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select

from celerp.events.engine import find_event_by_idempotency
from celerp.importers.schema import (
    CIFInventoryAdjustment,
    CIFItem,
    CIFSourceRecord,
    ReconciliationExpectations,
    ReconciliationMeasure,
)
from celerp.importers.sinks import DestinationMeasurement, SinkBatchResult, SinkContext
from celerp.models.company import Location
from celerp.models.projections import Projection
from celerp.services.migration_core_sink import (
    RecordOutcome,
    acting_member,
    deterministic_id,
    import_prepared,
    mapped_targets,
    sink_result,
)
from celerp.services.provisioning import ensure_default_location
from celerp_inventory import services
from celerp_inventory.services import BatchImportRequest, ImportRecord

ITEM = "item"
LOCATION = "location"


class InventoryMigrationSink:
    key = "celerp-inventory"
    groups = frozenset({"items", "inventory_adjustments"})
    batch_size = 500

    async def import_batch(self, context: SinkContext, records: Sequence[CIFSourceRecord]) -> SinkBatchResult:
        if all(isinstance(r, CIFItem) for r in records):
            return sink_result(records, await _import_items(context, records), ITEM)
        return sink_result(records, await _import_adjustments(context, records), ITEM)

    async def reconcile(
        self, context: SinkContext, expectations: ReconciliationExpectations
    ) -> list[DestinationMeasurement]:
        wanted = [e for e in expectations.expectations if e.measure == ReconciliationMeasure.INVENTORY_QUANTITY]
        items = await mapped_targets(context, ITEM, [e.key for e in wanted])
        out = []
        for e in wanted:
            if e.key not in items:
                continue
            row = await context.session.get(Projection, (context.company_id, items[e.key]))
            quantity = Decimal(str((row.state or {}).get("quantity") or 0)) if row else Decimal(0)
            out.append(DestinationMeasurement(e.measure, e.key, e.currency, quantity))
        return out


# ── Items ─────────────────────────────────────────────────────────────────────

async def _import_items(context: SinkContext, items: Sequence[CIFItem]) -> list[RecordOutcome]:
    member = await acting_member(context)
    locations = (await context.session.execute(
        select(Location).where(Location.company_id == context.company_id)
    )).scalars().all()
    by_name = {loc.name: str(loc.id) for loc in locations}
    default = None
    if any(not item.location_name for item in items):
        default = str((await ensure_default_location(context.session, context.company_id)).id)
    prepared = [_item_record(context, item, by_name, default) for item in items]

    async def write(ready: list[ImportRecord]):
        outcome, _batch_id = await services.write_import_batch(
            context.session, context.company_id, member.user, member.role, member.settings,
            BatchImportRequest(records=ready, filename="migration"),
        )
        return outcome

    return await import_prepared(prepared, write)


def _item_record(
    context: SinkContext, item: CIFItem, by_name: dict[str, str], default: str | None
) -> ImportRecord | str:
    if item.location_name:
        location_id = by_name.get(item.location_name)
        if location_id is None:
            return f"Item {item.name}: location {item.location_name} does not exist."
    else:
        location_id = default
    sell_by = item.weight_unit if item.sell_by == "weight" else (item.sell_by or item.unit or "piece")
    data = {
        "sku": item.sku,
        "name": item.name,
        "description": item.description,
        "quantity": 0,
        "sell_by": sell_by,
        "weight": float(item.weight) if item.weight is not None else None,
        "weight_unit": item.weight_unit,
        "category": item.category,
        "barcode": item.barcode,
        "location_id": location_id,
        "attributes": dict(item.attributes),
        "retail_price": float(item.retail_price) if item.retail_price is not None else None,
        "wholesale_price": float(item.wholesale_price) if item.wholesale_price is not None else None,
        "cost_price": float(item.cost_per_unit) if item.cost_per_unit is not None else None,
        "cost_total": float(item.total_cost) if item.cost_per_unit is None and item.total_cost is not None else None,
    }
    return ImportRecord(
        entity_id=f"item:{deterministic_id(context, item.source_type, item.source_external_id)}",
        event_type="item.created",
        data={k: v for k, v in data.items() if v not in (None, "", {})},
        source="migration",
        idempotency_key=context.idempotency_key(item, "created"),
    )


# ── Opening positions and adjustments ─────────────────────────────────────────

async def _import_adjustments(context: SinkContext, records: Sequence[CIFSourceRecord]) -> list[RecordOutcome]:
    adjustments = [r for r in records if isinstance(r, CIFInventoryAdjustment)]
    items = await mapped_targets(context, ITEM, [a.item_external_id for a in adjustments])
    locations = await mapped_targets(context, LOCATION, [a.location_external_id for a in adjustments])
    outcomes = []
    for record in records:
        if not isinstance(record, CIFInventoryAdjustment):
            outcomes.append(RecordOutcome("", "rejected", f"Record type {type(record).__name__} is not an inventory adjustment."))
            continue
        outcomes.append(await _adjust(context, record, items, locations))
    return outcomes


async def _adjust(
    context: SinkContext,
    record: CIFInventoryAdjustment,
    items: dict[str, str],
    locations: dict[str, str],
) -> RecordOutcome:
    session = context.session
    item_id = items.get(record.item_external_id)
    if item_id is None:
        return RecordOutcome("", "rejected", f"Item {record.item_external_id} was not imported.")
    key = context.idempotency_key(record, "adjusted")
    if await find_event_by_idempotency(session, context.company_id, key) is not None:
        return RecordOutcome(item_id, "skipped")
    row = await session.get(Projection, (context.company_id, item_id))
    state = row.state or {}
    if record.location_external_id:
        location_id = locations.get(record.location_external_id)
        if location_id is None or location_id != str(state.get("location_id") or ""):
            return RecordOutcome(
                "", "rejected",
                f"Item {record.item_external_id} is kept at one location in Celerp; "
                f"its position at {record.location_external_id} cannot be imported.",
            )
    current = Decimal(str(state.get("quantity") or 0))
    new_qty = record.quantity if record.kind == "opening" else current + record.quantity
    if new_qty < 0:
        return RecordOutcome("", "rejected", f"Item {record.item_external_id} would have a negative quantity.")
    try:
        await services.adjust_item_quantity(
            session, context.company_id, context.user_id, item_id,
            {"new_qty": float(new_qty), "prior_qty": float(current), "reason": f"Migration {record.kind}"},
            source="migration", idempotency_key=key,
        )
    except Exception as exc:
        return RecordOutcome("", "failed", f"Item {record.item_external_id}: {getattr(exc, 'detail', exc)}")
    return RecordOutcome(item_id, "created")


SINK = InventoryMigrationSink()
