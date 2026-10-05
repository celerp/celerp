# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

import functools
import hashlib
import json
import logging
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import LOT_ACCOUNT_FIELD
from celerp.connectors.ownership import PRODUCT_CHANNEL_PLATFORMS
from celerp.constants import ISO_4217_CURRENCIES
from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.importers.results import ImportOutcome
from celerp.importers.schema import IMPORT_ITEM_STATUSES
from celerp.inventory_codes import (
    PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES,
    validate_barcode,
    validate_gtin,
    validate_rfid_epc,
    validate_sku,
)
from celerp.models.company import Company, Location
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.account_roles import lot_account
from celerp.services.business_time import business_date_at
from celerp.services.demo import delete_untouched_demo_items
from celerp.services.cost_visibility import COST_ITEM_KEYS
from celerp.services.money import round_basis
from celerp.services.company_lock import holds_company_lock, lock_company, lock_projections, locked_company
from celerp.services.physical_codes import code_in_use, lock_item_code_namespace
from celerp.services.lot_origin import book_lot_value, recognize_opening_lots, self_booked
from celerp.importers.tabular import CsvImportSpec, cell_error_code, finite_float
from celerp.services.field_schema import AMOUNT_ITEM_KEYS, reject_system_item_fields
from celerp.services.money import to_stored_float, unit_price_from_total
from celerp.services.permissions import role_has_permission
from celerp.services.vertical_presets import category_item_defaults
from celerp.services.pricing import derived_price_keys, get_price_config, is_derived, price_key, price_keys_in
from celerp.services.units import (
    SERVICE_SELL_BY,
    build_unit_map,
    exceeds_precision,
    get_company_units,
    is_pieces_unit,
    is_weight_unit,
    validate_quantity,
)
from celerp_inventory.projections import is_core_item_key

logger = logging.getLogger(__name__)

# Internally assigned SKUs/barcodes are short zero-padded sequences; imported
# EAN-13/GTIN-14 barcodes (13-14 digits) are excluded from the sequence scan so
# they are never re-used as the next internal code.
_MAX_SEQ_DIGITS = 9
_SEQ_WIDTH = 6


# Fields that must NOT be inherited from parent in split/transform (child gets fresh values).
# Everything else in parent.state is inherited automatically (copy-all-then-override).
_CHILD_RESET_FIELDS: frozenset[str] = frozenset({
    # Identity - always overridden explicitly
    "sku",
    "barcode",      # recalculated: new entity needs a new unique barcode
    "rfid_epc",     # physical RFID/EPC tag: bound to one physical unit, never inherited by a new one
    "idempotency_key", # connector identity belongs to the catalog/product anchor
    "external_links",  # external channel identity must never be cloned onto a physical child
    "_catalog_sku_aliases",  # internal catalog-anchor SKU history never belongs on a lot
    # Quantity / cost - set by split math or pricing events
    "quantity",
    "weight",
    "pieces",
    "cost_total",
    "cost_price",
    # Status - children start as available regardless of parent's terminal status
    "status",
    # Timestamps - set fresh
    "created_at",
    "updated_at",
    # Relationship - set by split/transform logic
    "parent_id",
    "parent_sku",
})


def lot_fields(parent_state: dict) -> dict:
    """The fields a new lot of an item inherits from it: everything but identity, quantity,
    cost, status, timestamps and lineage, which each new lot sets for itself. A part of
    a lot keeps the lot's inventory account, recorded or not, so it never takes today's."""
    return {**{k: v for k, v in parent_state.items() if k not in _CHILD_RESET_FIELDS},
            LOT_ACCOUNT_FIELD: parent_state.get(LOT_ACCOUNT_FIELD)}


async def _next_seq(session: AsyncSession, company_id) -> int:
    """Return the next integer in the shared SKU/barcode sequence for a company.

    Scans integer-valued SKUs and barcodes together so the two namespaces never
    collide (a barcode assigned during a split is never re-used as a SKU on the
    next create). Only barcodes with <= _MAX_SEQ_DIGITS digits count, excluding
    imported EAN-13/GTIN-14 barcodes while covering every internally assigned one.
    """
    sku_vals = (await session.execute(
        select(Projection.state["sku"].as_string()).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    barcode_vals = (await session.execute(
        select(Projection.state["barcode"].as_string()).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    all_vals = list(sku_vals) + [v for v in barcode_vals if v and len(v) <= _MAX_SEQ_DIGITS]
    return max((int(v) for v in all_vals if v and str(v).isdigit()), default=0) + 1


async def allocate_internal_codes(session: AsyncSession, company_id, count: int = 1) -> list[str]:
    """Lock the company's code namespace and return ``count`` fresh codes, each free.

    The lock makes the scan-then-mint atomic against concurrent allocators. Codes are
    zero-padded to the standard internal width and are guaranteed distinct within the
    returned batch. Starting from the next sequential value, any candidate already held
    as a barcode OR an rfid_epc is skipped: the two fields share one physical-code
    namespace, and the sequence scan counts integer SKUs and barcodes only, so a numeric
    EPC can equal the next sequential code. Skipping here is the single guard every
    internal-mint path inherits, so no caller re-implements the availability check.
    """
    await lock_item_code_namespace(session, company_id)
    codes: list[str] = []
    candidate = await _next_seq(session, company_id)
    while len(codes) < count:
        code = str(candidate).zfill(_SEQ_WIDTH)
        if not await code_in_use(session, company_id, code):
            codes.append(code)
        candidate += 1
    return codes


class CostRestatementConflict(ValueError):
    """A cost change whose downstream consequences cannot be reconciled exactly."""


# History that moves part of a lot's cost somewhere today's state cannot trace
# exactly, so a correction reaching that lot cannot be allocated automatically.
_UNTRACEABLE_COST_EVENTS = frozenset(("item.split", "item.transform", "item.consumed"))


def goods_basis(state: dict) -> float | None:
    """The lot's goods cost (before landed cost), or None when it has no cost."""
    basis = state.get("cost_base")
    if basis is None:
        basis = state.get("cost_total")
    return None if basis is None else round_basis(basis)


def _lot_label(state: dict, entity_id: str) -> str:
    return str(state.get("sku") or state.get("name") or entity_id)


async def _cost_is_traceable(session: AsyncSession, company_id, entity_id: str, state: dict) -> bool:
    """Whether all of the lot's cost is still on the lot (or went into a merge or sale).

    Replays the lot's history: a split, transform or consumption moves cost elsewhere, and
    so does a quantity adjustment that lowers stock (an audit shortfall, a manual count, a
    supplier return), because the units that left took their share of cost with them. An
    audit that was later undone left nothing behind, so the history is judged by what
    survives: the audit and its undo cancel out. Adjustments that add stock keep every
    cost on the lot. History that cannot be replayed counts as untraceable.
    """
    from celerp_inventory.projections import apply_item_event

    if state.get("children") or state.get("transformed_into"):
        return False
    rows = (await session.execute(
        select(LedgerEntry.event_type, LedgerEntry.data)
        .where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id)
        .order_by(LedgerEntry.id)
    )).all()
    replayed: dict = {}
    audits_lowering: dict[str, bool] = {}   # standing audit list -> whether it lowered stock
    for event_type, data in rows:
        if event_type in _UNTRACEABLE_COST_EVENTS:
            return False
        data = data or {}
        try:
            before = float(replayed.get("quantity") or 0)
            replayed = apply_item_event(replayed, event_type, data)
            after = float(replayed.get("quantity") or 0)
        except (KeyError, TypeError, ValueError):
            return False
        if event_type != "item.quantity.adjusted":
            continue
        list_id = data.get("source_list_id")
        if data.get("reason") == "audit_undo" and list_id in audits_lowering:
            del audits_lowering[list_id]
        elif data.get("reason") == "audit" and list_id:
            audits_lowering[list_id] = before > 0 and after < before
        elif before > 0 and after < before:
            return False
    return not any(audits_lowering.values())


async def cost_can_be_restated(session: AsyncSession, company_id, entity_id: str, state: dict) -> bool:
    """Whether a later change to the lot's cost can still be carried (restate_item_cost): all
    of its cost is on it or went whole into a merge or a sale, it was not written off, and a
    sale is one exact invoice line whose cost of goods sold the change can adjust."""
    status = str(state.get("status") or "").lower()
    return (status != "disposed"
            and await _cost_is_traceable(session, company_id, entity_id, state)
            and (status != "sold" or await _sale_line(session, company_id, entity_id, state) is not None))


async def _sale_line(session: AsyncSession, company_id, entity_id: str, state: dict) -> tuple[str, int, str] | None:
    """(doc_id, line_index, doc_number) of the invoice line that sold this lot, or None.

    Only a sale fulfilled from one line of a finalized invoice whose recognized
    COGS is on record is exact enough to adjust."""
    sale = (await session.execute(
        select(LedgerEntry).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_id == entity_id,
            LedgerEntry.event_type == "item.fulfilled",
        ).order_by(LedgerEntry.id.desc()).limit(1)
    )).scalars().first()
    data = (sale.data or {}) if sale is not None else {}
    doc_id = data.get("source_doc_id")
    line_index = (sale.metadata_ or {}).get("line_index") if sale is not None else None
    if (
        data.get("doc_type") != "invoice"
        or not doc_id
        or state.get("status_doc_id") != doc_id
        or not isinstance(line_index, int)
        or isinstance(line_index, bool)
        or await auto_je.recognized_cogs(session, company_id, doc_id) is None
    ):
        return None
    return doc_id, line_index, data.get("doc_number") or doc_id


async def _invoice_line_of_sale(session: AsyncSession, company_id, entity_id: str, state: dict) -> tuple[str, int, str]:
    """_sale_line, refusing a sale it cannot match to one invoice line."""
    sale = await _sale_line(session, company_id, entity_id, state)
    if sale is None:
        raise CostRestatementConflict(
            f"{_lot_label(state, entity_id)} is sold, but the sale cannot be matched to one invoice "
            "line, so its cost of goods sold cannot be adjusted automatically"
        )
    return sale


@dataclass
class _Restatement:
    label: str
    successors: list[tuple[str, float]]      # (entity_id, new goods basis)
    repriced: dict[str, list[dict]]          # lot id -> per-invoice-line COGS changes
    lots: list[str]                          # the item and every merge result it reads
    docs: list[str]                          # the invoices it adjusts
    sold: list[tuple[str, str, float]]       # (lot id, inventory account, change in its cost of sale)


async def _restatement(session: AsyncSession, company_id, entity_id: str, event_type: str, data: dict,
                       locked: dict[str, Projection]) -> _Restatement:
    """Work out a goods-cost change and its consequences without writing anything.

    Reads each lot from locked when it is there, else as currently committed.
    Raises CostRestatementConflict when the change cannot be carried."""
    from celerp_inventory.projections import apply_item_event

    async def _read(eid: str) -> Projection | None:
        if eid in locked:
            return locked[eid]
        return (await session.execute(
            select(Projection).where(Projection.company_id == company_id, Projection.entity_id == eid)
            .execution_options(populate_existing=True)
        )).scalars().first()

    root = await _read(entity_id)
    if root is None or root.entity_type != "item":
        raise CostRestatementConflict("Item not found")
    old = root.state or {}
    new = apply_item_event(old, event_type, data)
    old_basis, new_basis = goods_basis(old), goods_basis(new)
    label = _lot_label(old, entity_id)
    status = str(old.get("status") or "").lower()
    cost_changed = old_basis != new_basis
    if cost_changed and not await _cost_is_traceable(session, company_id, entity_id, old):
        raise CostRestatementConflict(
            f"{label}'s cost was already split, transformed, or used, so the correction "
            "cannot be carried automatically"
        )

    successors: list[tuple[str, float]] = []
    restated: list[tuple[str, dict, dict]] = [(entity_id, old, new)]   # (entity_id, before, after)
    if status in ("merged", "sold", "disposed") and old_basis != new_basis:
        if old_basis is None or new_basis is None:
            raise CostRestatementConflict(
                f"{label} is {status}; its cost can be corrected but not added or cleared"
            )
        if status == "disposed":
            raise CostRestatementConflict(
                f"{label} was written off, so a cost correction cannot be carried into the write-off automatically"
            )
        delta = round_basis(new_basis - old_basis)
        seen = {entity_id}
        current, next_id = label, old.get("merged_into") if status == "merged" else None
        if status == "merged" and not next_id:
            raise CostRestatementConflict(f"{label} is merged, but its merge lineage is not recorded")
        while next_id:
            if next_id in seen:
                raise CostRestatementConflict(f"{label}: the merge lineage loops back on itself at {next_id}")
            seen.add(next_id)
            row = await _read(next_id)
            if row is None or row.entity_type != "item":
                raise CostRestatementConflict(
                    f"{current} was merged into {next_id}, which is not an item; the merge lineage is broken"
                )
            state = row.state or {}
            current = _lot_label(state, next_id)
            basis = goods_basis(state)
            succ_status = str(state.get("status") or "").lower()
            if not await cost_can_be_restated(session, company_id, next_id, state):
                raise CostRestatementConflict(
                    f"{label}'s cost went into {current}, which was later split, transformed, used, or "
                    "written off, so the correction cannot be carried through it automatically"
                )
            if basis is None or basis + delta < 0:
                raise CostRestatementConflict(
                    f"{label}'s cost went into {current}, whose cost cannot absorb a change of {delta:g}"
                )
            successors.append((next_id, round_basis(basis + delta)))
            restated.append((next_id, state, apply_item_event(state, "item.cost_adjusted", {"cost_total": basis + delta})))
            next_id = state.get("merged_into") if succ_status == "merged" else None
            if succ_status == "merged" and not next_id:
                raise CostRestatementConflict(f"{current} is merged, but its merge lineage is not recorded")

    # What each invoice recognizes for these lots changes with their cost: the invoice
    # that shipped a sold lot by the change in its cost of sale, and every invoice that
    # allocated a lot to a line it has not shipped by the allocated quantity times the
    # change in its unit cost. Two invoices can both allocate one lot before either
    # ships it, so a sold lot can still be allocated on another invoice.
    repriced: dict[str, list[dict]] = {}
    sold: list[tuple[str, str, float]] = []
    if cost_changed:
        for lot_id, before, after in restated:
            lot_status = str(before.get("status") or "").lower()
            if lot_status in ("merged", "memo_out"):
                continue
            records: list[dict] = []
            sold_on = None
            if lot_status == "sold":
                sold_on, line_index, _ = await _invoice_line_of_sale(session, company_id, lot_id, before)
                cycle = (await auto_je.recognized_cogs(session, company_id, sold_on)).cycle
                change = auto_je.lot_cost_of_sale(after) - auto_je.lot_cost_of_sale(before)
                records.append({"doc_id": sold_on, "cycle": cycle, "line": line_index, "amount": change})
                if change:
                    sold.append((lot_id, lot_account(before), change))
            unit_delta = auto_je.lot_unit_cost(after) - auto_je.lot_unit_cost(before)
            for (doc_id, cycle, line_index), qty in sorted(
                    (await auto_je.allocations_naming_lot(session, company_id, lot_id)).items()):
                if doc_id != sold_on:
                    records.append({"doc_id": doc_id, "cycle": cycle, "line": line_index, "amount": qty * unit_delta})
            records = [r for r in records if r["amount"]]
            if records:
                repriced[lot_id] = records
    return _Restatement(
        label=label, successors=successors, repriced=repriced,
        lots=[eid for eid, _, _ in restated],
        docs=sorted({r["doc_id"] for records in repriced.values() for r in records}),
        sold=sold,
    )


async def restate_item_cost(
    session: AsyncSession,
    company_id,
    entity_id: str,
    *,
    event_type: str,
    data: dict,
    actor_id,
    source: str,
    idempotency_key: str,
    day: str | None = None,
) -> LedgerEntry:
    """Apply a goods-cost change to an item and carry its consequences.

    A late correction changes a lot's historical cost by a delta. The delta
    follows the cost into each merge result (merged_into, generation after
    generation), restated with item.cost_adjusted, so later adjustments made
    to a result are kept. Every invoice that recognizes one of these lots - sold
    on one of its lines, or allocated to a line not yet shipped - has the change
    recorded against that line and its COGS trued up by one adjustment JE dated
    ``day``, the business day of the operation making the change, or today,
    leaving the invoice's own entries untouched. A sold lot is no longer in stock, so
    its change in cost is first booked onto its inventory account against stock gains
    or shrinkage (lot_origin.book_lot_value) and the true-up relieves it from there:
    the account nets to nothing and cost of sales moves against the source leg. A
    writer that books the value itself (lot_origin.self_booked, a production run) is
    left to its own entries. Every check runs before
    the first event is written: the change lands with all of its consequences in
    the caller's transaction, or raises CostRestatementConflict.
    """
    replay = await find_event_by_idempotency(session, company_id, idempotency_key)
    if replay is not None:
        if replay.event_type != event_type or replay.entity_id != entity_id:
            raise CostRestatementConflict("Idempotency key was already used for another operation")
        return replay

    # Company, then the invoices the change adjusts, then the lots: the order every
    # invoice lifecycle action takes. The first reading names the invoices and lots;
    # the second repeats it under their locks and must name no others.
    await lock_company(session, company_id)
    unlocked = await _restatement(session, company_id, entity_id, event_type, data, {})
    await lock_projections(session, company_id, unlocked.docs)
    lots = await lock_projections(session, company_id, unlocked.lots)
    plan = await _restatement(session, company_id, entity_id, event_type, data, lots)
    if not set(plan.docs) <= set(unlocked.docs) or not set(plan.lots) <= set(unlocked.lots):
        raise CostRestatementConflict(
            f"{plan.label} changed while the correction was being applied; try again")
    successors, repriced, label = plan.successors, plan.repriced, plan.label

    def _metadata(lot_id: str, base: dict) -> dict:
        return {**base, "cogs_repriced": repriced[lot_id]} if lot_id in repriced else base

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="item",
        event_type=event_type, data=data, actor_id=actor_id, location_id=None,
        source=source, idempotency_key=idempotency_key, metadata_=_metadata(entity_id, {}),
    )
    identity = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
    # A production run's completion entry books the re-cost of every lot it produced,
    # merged or not, so the change carried into a merge result names the run too.
    lineage = {"restated_from": entity_id}
    if data.get("manufacturing_order_id"):
        lineage["manufacturing_order_id"] = data["manufacturing_order_id"]
    for succ_id, basis in successors:
        await emit_event(
            session, company_id=company_id, entity_id=succ_id, entity_type="item",
            event_type="item.cost_adjusted", data={"cost_total": basis},
            actor_id=actor_id, location_id=None, source=source,
            idempotency_key=f"cost-restate:{identity}:{succ_id}",
            metadata_=_metadata(succ_id, lineage),
        )
    if plan.docs:
        if day is None:
            company = await session.get(Company, company_id)
            day = business_date_at(datetime.now(timezone.utc), ((company.settings if company else None) or {}).get("timezone"))
        for lot_id, account, change in ([] if self_booked(entry) else plan.sold):
            await book_lot_value(
                session, company_id, actor_id, account, Decimal(str(change)),
                je_id=f"je:auto:{lot_id}:cost-restated:{identity}", idem=f"cost-restate-value:{identity}:{lot_id}",
                day=day, metadata={"trigger": "item.cost_restated", "item_id": entity_id, "restatement": identity})
        for doc_id in plan.docs:
            try:
                await auto_je.reconcile_doc_cogs(
                    session, company_id=company_id, user_id=actor_id, doc_id=doc_id,
                    cycle_tag=f"restate-{hashlib.sha256(f'{identity}:{doc_id}'.encode()).hexdigest()[:16]}",
                    ts=day, trigger="item.cost_restated",
                    memo=f"COGS adjustment: cost of {label} corrected",
                    context={"item_id": entity_id, "restatement": identity},
                )
            except ValueError as exc:
                raise CostRestatementConflict(str(exc)) from exc
    return entry


_CHANNEL_KEY_PREFIXES = tuple(f"{p}:" for p in PRODUCT_CHANNEL_PLATFORMS)


def _legacy_external_link(platform: str, idem_key: str) -> dict:
    """Decode connector identity stored by releases before external_links existed."""
    parts = (idem_key or "").split(":")
    if parts[0] != platform:
        return {}
    if platform == "shopify" and len(parts) >= 3:
        return {"product_id": parts[1], "variant_id": parts[2], "sync_enabled": True}
    if platform == "woocommerce" and len(parts) >= 2:
        link = {"product_id": parts[1], "sync_enabled": True}
        if len(parts) >= 3 and parts[2]:
            link["variation_id"] = parts[2]
        return link
    return {}


def external_link_for_state(state: dict, platform: str) -> dict:
    """Return one normalized external product link without mutating item state."""
    links = state.get("external_links") or {}
    raw = links.get(platform) if isinstance(links, dict) else None
    if isinstance(raw, dict) and raw.get("detached") is True:
        return {}
    if isinstance(raw, dict) and raw.get("product_id") not in (None, ""):
        return dict(raw)
    return _legacy_external_link(platform, str(state.get("idempotency_key") or ""))


def _external_ids(platform: str, state: dict) -> dict:
    """Flatten one normalized link into connector adapter field names."""
    link = external_link_for_state(state, platform)
    if not link:
        return {}
    if platform == "shopify":
        return {
            "shopify_product_id": str(link.get("product_id") or ""),
            "shopify_variant_id": str(link.get("variant_id") or ""),
        }
    if platform == "woocommerce":
        out = {"woocommerce_product_id": str(link.get("product_id") or "")}
        if link.get("variation_id") not in (None, ""):
            out["woocommerce_variation_id"] = str(link["variation_id"])
        return out
    return {}


def normalize_sku(value) -> str:
    """Canonical SKU comparison key."""
    return str(value or "").strip().casefold()


class ExternalLinkConflictError(ValueError):
    """External product identity changed or is already claimed."""


async def _lock_external_identity_namespace(
    session: AsyncSession, company_id, platform: str
) -> None:
    if session.get_bind().dialect.name == "sqlite":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"external-link:{company_id}:{platform}"},
    )


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _external_identity_candidates(cid: uuid.UUID, platform: str, product_id: str):
    """Rows that may carry one external product identity: an explicit link or
    a legacy connector idempotency key. Callers decide with the exact Python
    identity check; this only keeps the scan off the whole catalog."""
    idem = Projection.state.op("->>")("idempotency_key")
    prefix = f"{platform}:{product_id}"
    return select(Projection).where(
        Projection.company_id == cid,
        Projection.entity_type == "item",
        or_(
            Projection.state.op("->")("external_links").op("->")(platform)
            .op("->>")("product_id") == product_id,
            idem == prefix,
            idem.like(_like_escape(prefix) + ":%", escape="\\"),
        ),
    )


async def _assert_external_identity_available(
    session: AsyncSession,
    company_id,
    platform: str,
    link: dict,
    *,
    exclude_entity_id: str | None = None,
) -> None:
    product_id = str(link.get("product_id") or "")
    if not product_id:
        raise ExternalLinkConflictError("External product identity is missing")
    variation_id = link.get(_external_variant_key(platform))
    rows = (await session.execute(
        _external_identity_candidates(uuid.UUID(str(company_id)), platform, product_id)
    )).scalars().all()
    for candidate in rows:
        if candidate.entity_id == exclude_entity_id:
            continue
        if _same_external_identity(
            platform,
            external_link_for_state(candidate.state or {}, platform),
            product_id,
            str(variation_id) if variation_id not in (None, "") else None,
        ):
            raise ExternalLinkConflictError(
                f"{platform} product identity is already linked to another catalog item"
            )


def _is_structural_product_anchor_state(state: dict) -> bool:
    """True when a row is structurally a product root, independent of physical codes."""
    return (
        str(state.get("status") or "").lower() != "merged"
        and not any((
            state.get("catalog_item_id"),
            state.get("lot"),
            state.get("parent_item_id"),
            state.get("split_from"),
            state.get("transformed_from"),
        ))
    )


def product_of_stock(entity_id: str, state: dict) -> str | None:
    """The product that stock taken from this item is stock of, as the item records it: the
    item itself when it is a product, else the product it links to; None when it records
    none (units split off under another SKU, say). Never inferred from a SKU."""
    if _is_structural_product_anchor_state(state):
        return entity_id
    return state.get("catalog_item_id") or state.get("parent_item_id") or None


def _is_product_anchor_state(state: dict) -> bool:
    """Infer a product root only when historical state is unambiguous."""
    if not _is_structural_product_anchor_state(state):
        return False
    links = state.get("external_links") or {}
    if isinstance(links, dict) and any(
        isinstance(link, dict) and link.get("product_id") not in (None, "")
        for link in links.values()
    ):
        return True
    idem = str(state.get("idempotency_key") or "")
    if idem.startswith(_CHANNEL_KEY_PREFIXES):
        return True
    return not bool(state.get("barcode") or state.get("rfid_epc"))


def _external_variant_key(platform: str) -> str:
    return "variant_id" if platform == "shopify" else "variation_id"


def _same_external_identity(
    platform: str, link: dict, product_id: str, variation_id: str | None
) -> bool:
    if str(link.get("product_id") or "") != str(product_id):
        return False
    actual = link.get(_external_variant_key(platform))
    return (str(actual) if actual not in (None, "") else None) == (
        str(variation_id) if variation_id not in (None, "") else None
    )


def _select_external_anchor(rows: list[Projection], platform: str, product_id: str,
                            variation_id: str | None) -> Projection | None:
    matches = [
        r for r in rows
        if _same_external_identity(
            platform, external_link_for_state(r.state or {}, platform),
            product_id, variation_id,
        )
    ]
    if not matches:
        return None
    roots = [r for r in matches if _is_product_anchor_state(r.state or {})]
    if len(roots) == 1:
        return roots[0]
    if len(roots) > 1:
        explicit = [
            r for r in roots
            if isinstance(((r.state or {}).get("external_links") or {}).get(platform), dict)
        ]
        if len(explicit) == 1:
            return explicit[0]
        raise ValueError(
            f"Multiple catalog items claim {platform} product {product_id}"
            + (f" variation {variation_id}" if variation_id else "")
        )
    if len(matches) == 1:
        return matches[0]
    raise ValueError(
        f"Multiple inventory rows claim {platform} product {product_id}"
        + (f" variation {variation_id}" if variation_id else "")
    )


async def resolve_external_product(
    session: AsyncSession,
    company_id,
    platform: str,
    product_id: str,
    variation_id: str | None = None,
    sku: str | None = None,
) -> Projection | None:
    """Resolve external identity first, then one unambiguous catalog SKU."""
    cid = uuid.UUID(str(company_id))
    product_id = str(product_id)
    linked_rows = (await session.execute(
        _external_identity_candidates(cid, platform, product_id)
    )).scalars().all()
    linked = _select_external_anchor(linked_rows, platform, product_id, variation_id)
    if linked is not None:
        return linked

    norm_sku = normalize_sku(sku)
    if not norm_sku:
        return None
    sku_query = select(Projection).where(
        Projection.company_id == cid,
        Projection.entity_type == "item",
    )
    narrowing = _sku_narrowing({norm_sku})
    if narrowing is not None:
        sku_query = sku_query.where(or_(*narrowing))
    sku_rows = (await session.execute(sku_query)).scalars().all()
    candidates = [
        r for r in sku_rows
        if _is_product_anchor_state(r.state or {})
        and normalize_sku((r.state or {}).get("sku")) == norm_sku
    ]
    if len(candidates) == 1:
        candidate = candidates[0]
        existing_link = external_link_for_state(candidate.state or {}, platform)
        if (
            existing_link
            and not _same_external_identity(
                platform, existing_link, str(product_id), variation_id
            )
            and not deleted_external_link_may_relink(existing_link)
        ):
            raise ValueError(
                f"SKU {sku!r} is already linked to a different {platform} product"
            )
        return candidate
    if len(candidates) > 1:
        raise ValueError(f"SKU {sku!r} matches multiple catalog products")
    return None


def _connector_event_idem(prefix: str, payload: dict) -> str:
    content = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha1(content.encode()).hexdigest()[:16]}"


def deleted_external_link_may_relink(link: dict | None) -> bool:
    """Only remote deletion, never an intentional user disable, permits identity replacement."""
    return bool(link and link.get("remote_deleted") is True)


def external_link_intentionally_disabled(link: dict | None) -> bool:
    """A user-disabled live link blocks product-side inbound mutation."""
    return bool(
        link
        and link.get("sync_enabled") is False
        and link.get("remote_deleted") is not True
    )


def relinked_external_sync_enabled(link: dict | None) -> bool:
    """Replacing a dead remote identity preserves the user's prior sync preference."""
    if not link:
        return True
    return bool(link.get("sync_enabled", True))


async def upsert_external_product(
    company_id: str,
    *,
    platform: str,
    product_id: str,
    variation_id: str | None,
    sku: str,
    name: str,
    description: str | None = None,
    sale_price: float | None = None,
    quantity: float | None = None,
    seed_quantity: bool = False,
    link_fields: dict | None = None,
    inventory_type: str | None = None,
    sell_by: str | None = None,
) -> tuple[str, str]:
    """Create or link one external product without making the connector an inventory engine."""
    from celerp.db import SessionLocal as AsyncSessionLocal

    cid = uuid.UUID(str(company_id))
    product_id = str(product_id)
    variation_id = str(variation_id) if variation_id not in (None, "") else None
    identity = f"{platform}:{product_id}" + (f":{variation_id}" if variation_id else "")
    sku_lock = str(sku or "").strip().casefold()
    lock_key = f"external-product:{cid}:{platform}:{sku_lock or identity}"

    async with AsyncSessionLocal() as session:
        await _lock_external_identity_namespace(session, cid, platform)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": lock_key},
        )
        row = await resolve_external_product(
            session, cid, platform, product_id, variation_id, sku=sku
        )
        selected_by_identity = bool(
            row is not None
            and _same_external_identity(
                platform,
                external_link_for_state(row.state or {}, platform),
                product_id,
                variation_id,
            )
        )
        legacy_row = None
        legacy_cleaned = False
        if row is not None and not _is_product_anchor_state(row.state or {}):
            legacy_row = row
            row = await resolve_catalog_anchor_for_item(
                session, cid, legacy_row.entity_id
            )
            selected_by_identity = False
        if row is not None:
            row = await session.get(
                Projection,
                {"company_id": cid, "entity_id": row.entity_id},
                with_for_update=True,
                populate_existing=True,
            )
            if (
                not selected_by_identity
                and normalize_sku((row.state or {}).get("sku")) != normalize_sku(sku)
            ):
                raise ExternalLinkConflictError(
                    "Catalog SKU changed while the external product was being resolved"
                )

        incoming_link = {
            "product_id": product_id,
            "sync_enabled": True,
            "remote_deleted": False,
            **(link_fields or {}),
        }
        if variation_id:
            incoming_link[_external_variant_key(platform)] = variation_id

        if row is None:
            await _assert_external_identity_available(
                session, cid, platform, incoming_link
            )
            entity_id = f"item:{identity}"
            data: dict = {
                "sku": sku,
                "name": name,
                "sell_by": sell_by or "piece",
                "external_links": {platform: incoming_link},
                "idempotency_key": identity,
            }
            if inventory_type is not None:
                data["inventory_type"] = inventory_type
            if description is not None:
                data["description"] = description
            if sale_price is not None:
                data["sale_price"] = sale_price
                data["retail_price"] = sale_price
            if seed_quantity and quantity is not None:
                data["quantity"] = float(quantity)
            event_idem = _connector_event_idem(f"{identity}:create", data)
            entry = await emit_event(
                session, company_id=cid, entity_id=entity_id, entity_type="item",
                event_type="item.created", data=data, actor_id=None, location_id=None,
                source="connector", idempotency_key=event_idem, metadata_={},
            )
            # A store product is stock held here: it records the opening inventory account,
            # and any value it carries is booked as opening stock.
            await recognize_opening_lots(session, cid, [entity_id], None, f"connector:{identity}")
            await session.commit()
            return ("noop" if getattr(entry, "was_deduped", False) else "created", entity_id)

        entity_id = row.entity_id
        state = dict(row.state or {})

        if legacy_row is not None and legacy_row.entity_id != entity_id:
            legacy_state = dict(legacy_row.state or {})
            legacy_changes: dict = {}
            if str(legacy_state.get("idempotency_key") or "") == identity:
                legacy_changes["idempotency_key"] = {
                    "old": legacy_state.get("idempotency_key"), "new": None,
                }
            legacy_links = dict(legacy_state.get("external_links") or {})
            if platform in legacy_links:
                cleaned_links = dict(legacy_links)
                cleaned_links.pop(platform, None)
                legacy_changes["external_links"] = {
                    "old": legacy_links, "new": cleaned_links,
                }
            if legacy_changes:
                cleanup_data = {"fields_changed": legacy_changes}
                await emit_event(
                    session,
                    company_id=cid,
                    entity_id=legacy_row.entity_id,
                    entity_type="item",
                    event_type="item.updated",
                    data=cleanup_data,
                    actor_id=None,
                    location_id=None,
                    source="connector",
                    idempotency_key=_connector_event_idem(
                        f"{identity}:legacy-clean:{legacy_row.entity_id}:v{legacy_row.version}",
                        cleanup_data,
                    ),
                    metadata_={},
                )
                legacy_cleaned = True
        await _assert_external_identity_available(
            session, cid, platform, incoming_link, exclude_entity_id=entity_id
        )
        explicit = ((state.get("external_links") or {}).get(platform)
                    if isinstance(state.get("external_links"), dict) else None)
        if external_link_intentionally_disabled(explicit):
            if legacy_cleaned:
                await session.commit()
            return "disabled", entity_id

        links = dict(state.get("external_links") or {})
        previous_link = external_link_for_state(state, platform)
        links[platform] = {
            **previous_link,
            **incoming_link,
            "sync_enabled": relinked_external_sync_enabled(previous_link),
        }
        identity_only = bool(
            previous_link
            and previous_link.get("remote_deleted") is True
            and previous_link.get("sync_enabled") is False
        )
        desired = {"external_links": links}
        if not identity_only:
            if normalize_sku(state.get("sku")) != normalize_sku(sku):
                await stamp_catalog_family_members(
                    session, cid, entity_id, source="connector"
                )
                state = dict(row.state or {})
            desired.update({"sku": sku, "name": name})
            if inventory_type is not None:
                desired["inventory_type"] = inventory_type
            if sell_by is not None:
                desired["sell_by"] = sell_by
            if description is not None:
                desired["description"] = description
            if sale_price is not None:
                desired["sale_price"] = sale_price
                desired["retail_price"] = sale_price

        fields_changed = {
            key: {"old": state.get(key), "new": value}
            for key, value in desired.items() if state.get(key) != value
        }
        if not fields_changed:
            if legacy_cleaned:
                await session.commit()
            return "noop", entity_id

        event_data = {"fields_changed": fields_changed}
        entry = await emit_event(
            session, company_id=cid, entity_id=entity_id, entity_type="item",
            event_type="item.updated", data=event_data, actor_id=None, location_id=None,
            source="connector",
            idempotency_key=_connector_event_idem(
                f"{identity}:update:{entity_id}:v{row.version}", event_data
            ),
            metadata_={},
        )
        await session.commit()
        if links[platform].get("sync_enabled") is False:
            # Identity repair for a remotely deleted product is allowed even when the
            # user intentionally left product sync disabled, but callers must not
            # continue with product-side mutation such as media pulls.
            return "disabled", entity_id
        return ("noop" if getattr(entry, "was_deduped", False) else "updated", entity_id)


async def set_external_link_state(
    session: AsyncSession, company_id, entity_id: str, platform: str, *,
    sync_enabled: bool | None = None, remote_deleted: bool | None = None,
    link_updates: dict | None = None, expected_identity: tuple[str, str | None] | None = None,
    actor_id=None, source: str = "connector",
) -> dict:
    """Patch one external link while preserving every other channel identity."""
    cid = uuid.UUID(str(company_id))
    await _lock_external_identity_namespace(session, cid, platform)
    row = await session.get(
        Projection, {"company_id": cid, "entity_id": entity_id},
        with_for_update=True, populate_existing=True,
    )
    if row is None or row.entity_type != "item":
        raise ValueError(f"Item {entity_id!r} not found")
    current = external_link_for_state(row.state or {}, platform)
    if not current:
        raise ValueError(f"Item {entity_id!r} is not linked to {platform}")
    if expected_identity is not None and external_identity_key(platform, current) != expected_identity:
        raise ExternalLinkConflictError(
            "External product identity changed while the operation was running"
        )
    updated = dict(current)
    if sync_enabled is not None:
        updated["sync_enabled"] = bool(sync_enabled)
    if remote_deleted is not None:
        updated["remote_deleted"] = bool(remote_deleted)
    if link_updates:
        updated.update(link_updates)
    return await set_external_link(
        session, cid, entity_id, platform, updated,
        actor_id=actor_id, source=source,
    )


async def set_external_link(
    session: AsyncSession, company_id, entity_id: str, platform: str, link: dict,
    *, expected_sku: str | None = None,
    expected_identity: tuple[str, str | None] | None = None,
    require_unlinked: bool = False,
    actor_id=None, source: str = "connector",
) -> dict:
    """Create or replace one channel link without touching any other channel."""
    cid = uuid.UUID(str(company_id))
    await _lock_external_identity_namespace(session, cid, platform)
    row = await session.get(
        Projection, {"company_id": cid, "entity_id": entity_id},
        with_for_update=True, populate_existing=True,
    )
    if row is None or row.entity_type != "item":
        raise ValueError(f"Item {entity_id!r} not found")
    state = dict(row.state or {})
    if expected_sku is not None and normalize_sku(state.get("sku")) != normalize_sku(expected_sku):
        raise ExternalLinkConflictError(
            "Catalog SKU changed while the external product was being resolved"
        )
    current = external_link_for_state(state, platform)
    if require_unlinked and current:
        raise ExternalLinkConflictError(
            "External product identity changed while the operation was running"
        )
    if expected_identity is not None and (
        not current or external_identity_key(platform, current) != expected_identity
    ):
        raise ExternalLinkConflictError(
            "External product identity changed while the operation was running"
        )
    links = dict(state.get("external_links") or {})
    normalized = dict(link)
    normalized["product_id"] = str(normalized["product_id"])
    variant_key = _external_variant_key(platform)
    if normalized.get(variant_key) not in (None, ""):
        normalized[variant_key] = str(normalized[variant_key])
    links[platform] = normalized
    if links == (state.get("external_links") or {}):
        return normalized
    await _assert_external_identity_available(
        session, cid, platform, normalized, exclude_entity_id=entity_id
    )
    data = {
        "fields_changed": {
            "external_links": {
                "old": state.get("external_links") or {},
                "new": links,
            }
        }
    }
    await emit_event(
        session, company_id=cid, entity_id=entity_id, entity_type="item",
        event_type="item.updated", data=data, actor_id=actor_id, location_id=None,
        source=source,
        idempotency_key=_connector_event_idem(
            f"external-link:{platform}:{entity_id}:v{row.version}", data
        ),
        metadata_={},
    )
    return normalized


async def detach_external_link(
    session: AsyncSession, company_id, entity_id: str, platform: str, *,
    actor_id=None, source: str = "connector_ui",
) -> bool:
    """Detach one platform identity while preserving local item and other channels."""
    cid = uuid.UUID(str(company_id))
    await _lock_external_identity_namespace(session, cid, platform)
    row = await session.get(
        Projection, {"company_id": cid, "entity_id": entity_id}, with_for_update=True
    )
    if row is None or row.entity_type != "item":
        return False
    state = dict(row.state or {})
    links = dict(state.get("external_links") or {})
    raw = links.get(platform) if isinstance(links, dict) else None
    if isinstance(raw, dict) and raw.get("detached") is True:
        return False
    if not external_link_for_state(state, platform):
        return False
    links[platform] = {"detached": True}
    data = {
        "fields_changed": {
            "external_links": {
                "old": state.get("external_links") or {},
                "new": links,
            }
        }
    }
    await emit_event(
        session, company_id=cid, entity_id=entity_id, entity_type="item",
        event_type="item.updated", data=data, actor_id=actor_id, location_id=None,
        source=source,
        idempotency_key=_connector_event_idem(
            f"external-detach:{platform}:{entity_id}:v{row.version}", data
        ),
        metadata_={},
    )
    return True


async def detach_external_links_for_platform(
    session: AsyncSession, company_id, platform: str, *, actor_id=None
) -> int:
    """Detach every item identity for one platform in the caller's transaction."""
    cid = uuid.UUID(str(company_id))
    entity_ids = (await session.execute(
        select(Projection.entity_id).where(
            Projection.company_id == cid,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    detached = 0
    for entity_id in entity_ids:
        if await detach_external_link(
            session, cid, entity_id, platform, actor_id=actor_id
        ):
            detached += 1
    return detached


async def resolve_catalog_anchor_for_item(session: AsyncSession, company_id, entity_id: str) -> Projection:
    """Resolve a selected catalog or lot row to one unambiguous product anchor."""
    cid = uuid.UUID(str(company_id))
    row = await session.get(Projection, {"company_id": cid, "entity_id": entity_id})
    if row is None or row.entity_type != "item":
        raise ValueError(f"Item {entity_id!r} not found")
    state = row.state or {}

    catalog_item_id = state.get("catalog_item_id")
    if catalog_item_id:
        parent = await session.get(
            Projection, {"company_id": cid, "entity_id": str(catalog_item_id)}
        )
        if (
            parent is None
            or parent.entity_type != "item"
            or not _is_structural_product_anchor_state(parent.state or {})
        ):
            raise ValueError(f"Item {entity_id!r} references an invalid catalog product anchor")
        return parent

    parent_item_id = state.get("parent_item_id")
    if parent_item_id:
        parent = await session.get(
            Projection, {"company_id": cid, "entity_id": str(parent_item_id)}
        )
        if (
            parent is not None
            and parent.entity_type == "item"
            and _is_structural_product_anchor_state(parent.state or {})
        ):
            return parent

    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid,
        Projection.entity_type == "item",
    ))).scalars().all()
    key = _family_keys(rows).get(row.entity_id)
    if key and key[0] == "anchor":
        anchor = next(
            (candidate for candidate in rows if candidate.entity_id == key[1]),
            None,
        )
        if (
            anchor is not None
            and _is_structural_product_anchor_state(anchor.state or {})
        ):
            return anchor

    sku = normalize_sku(state.get("sku"))
    if not sku:
        raise ValueError(f"Item {entity_id!r} has no catalog SKU to resolve")
    raise ValueError(f"SKU {state.get('sku')!r} does not resolve to one catalog product anchor")


def _is_explicit_catalog_anchor_state(state: dict) -> bool:
    """True for a structural root carrying durable catalog identity/history."""
    if not _is_structural_product_anchor_state(state):
        return False
    links = state.get("external_links") or {}
    if isinstance(links, dict) and any(
        isinstance(link, dict)
        and link.get("detached") is not True
        and link.get("product_id") not in (None, "")
        for link in links.values()
    ):
        return True
    idem = str(state.get("idempotency_key") or "")
    return bool(state.get("_catalog_sku_aliases")) or idem.startswith(
        _CHANNEL_KEY_PREFIXES
    )


def _family_keys(rows: list[Projection]) -> dict[str, tuple[str, str]]:
    """Resolve structural catalog families, using SKU only for legacy inference."""
    roots_by_sku: dict[str, list[Projection]] = {}
    explicit_by_sku: dict[str, list[Projection]] = {}

    for row in rows:
        state = row.state or {}
        sku = normalize_sku(state.get("sku"))
        if _is_product_anchor_state(state) and sku:
            roots_by_sku.setdefault(sku, []).append(row)
        if not _is_explicit_catalog_anchor_state(state):
            continue
        sku_keys = {sku}
        sku_keys.update(
            normalize_sku(value)
            for value in (state.get("_catalog_sku_aliases") or [])
        )
        for key in sku_keys:
            if key:
                explicit_by_sku.setdefault(key, []).append(row)

    keys: dict[str, tuple[str, str]] = {}
    for row in rows:
        state = row.state or {}
        catalog_item_id = state.get("catalog_item_id")
        if catalog_item_id:
            keys[row.entity_id] = ("anchor", str(catalog_item_id))
            continue

        sku = normalize_sku(state.get("sku"))
        if _is_product_anchor_state(state):
            keys[row.entity_id] = ("anchor", row.entity_id)
            continue

        roots = {
            candidate.entity_id: candidate
            for candidate in (roots_by_sku.get(sku, []) if sku else [])
        }
        explicit = {
            candidate.entity_id: candidate
            for candidate in (explicit_by_sku.get(sku, []) if sku else [])
        }
        root_ids = set(roots)
        explicit_ids = set(explicit)
        if len(root_ids) == 1 and len(explicit_ids) <= 1:
            root_id = next(iter(root_ids))
            if not explicit_ids or explicit_ids == {root_id}:
                keys[row.entity_id] = ("anchor", root_id)
                continue
        if not root_ids and len(explicit_ids) == 1:
            keys[row.entity_id] = ("anchor", next(iter(explicit_ids)))
            continue
        keys[row.entity_id] = ("sku", sku)
    return keys


def catalog_family_rows(
    rows: list[Projection], anchor: Projection
) -> list[Projection]:
    """Return rows belonging to an anchor's canonical product family."""
    keys = _family_keys(rows)
    key = keys.get(anchor.entity_id)
    if key is None:
        return []
    return [row for row in rows if keys.get(row.entity_id) == key]


_ASCII_ONLY = r"^[\x01-\x7f]*$"


def _sku_narrowing(keys: set[str]) -> list | None:
    """SQL clauses selecting every item row whose stored SKU or SKU alias can
    normalize to one of the keys, so a caller's exact Python check runs over a
    subset instead of the whole catalog. Rows holding any non-ASCII character
    are always selected: casefold() and lower() agree only on ASCII (GROSS
    with a sharp s folds to gross), so those rows are left to the exact check.
    Returns None when a key holds a quote or backslash, which JSON escapes
    inside the aliases' text; the caller then scans the whole catalog, exactly
    as before the narrowing existed."""
    from sqlalchemy import func

    if any('"' in key or "\\" in key for key in keys):
        return None
    sku_text = Projection.state.op("->>")("sku")
    aliases_text = Projection.state.op("->>")("_catalog_sku_aliases")
    clauses = [sku_text.op("!~")(_ASCII_ONLY), aliases_text.op("!~")(_ASCII_ONLY)]
    for key in sorted(keys):
        pattern = f"%{_like_escape(key)}%"
        clauses.append(func.lower(sku_text).like(pattern, escape="\\"))
        clauses.append(func.lower(aliases_text).like(pattern, escape="\\"))
    return clauses


async def load_catalog_family_rows(
    session: AsyncSession, company_id, anchor: Projection
) -> list[Projection]:
    """catalog_family_rows over only the rows that can belong to the anchor's
    family: the anchor, rows pinned to it, rows sharing its SKU or one of its
    aliases, and rows aliasing any of those. Family inference for that set is
    identical to a whole-catalog scan because every row it consults shares one
    of those SKUs; when the SKUs cannot be narrowed in SQL the whole catalog
    is loaded."""
    return catalog_family_rows(
        await _catalog_family_candidates(session, company_id, anchor), anchor
    )


async def _catalog_family_candidates(
    session: AsyncSession, company_id, anchor: Projection
) -> list[Projection]:
    """Every row load_catalog_family_rows consults: the anchor, rows pinned to
    it, and rows sharing its SKU or an alias of it."""
    cid = uuid.UUID(str(company_id))
    state = anchor.state or {}
    skus = {normalize_sku(state.get("sku"))}
    skus.update(normalize_sku(alias) for alias in (state.get("_catalog_sku_aliases") or []))
    skus.discard("")
    query = select(Projection).where(
        Projection.company_id == cid,
        Projection.entity_type == "item",
    )
    narrowing = _sku_narrowing(skus)
    if narrowing is not None:
        query = query.where(or_(
            Projection.entity_id == anchor.entity_id,
            Projection.state.op("->>")("catalog_item_id") == anchor.entity_id,
            *narrowing,
        ))
    return list((await session.execute(query)).scalars().all())


async def stamp_catalog_family_members(
    session: AsyncSession, company_id, anchor_entity_id: str, *,
    actor_id=None, source: str = "api",
) -> int:
    """Persist currently unambiguous family membership before anchor identity changes."""
    cid = uuid.UUID(str(company_id))
    anchor = await session.get(
        Projection, {"company_id": cid, "entity_id": anchor_entity_id},
        with_for_update=True, populate_existing=True,
    )
    if anchor is None or not _is_product_anchor_state(anchor.state or {}):
        return 0
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "item"
        )
    )).scalars().all()
    keys = _family_keys(rows)
    family_key = ("anchor", anchor.entity_id)
    stamped = 0
    for member in rows:
        if (
            member.entity_id == anchor.entity_id
            or keys.get(member.entity_id) != family_key
            or (member.state or {}).get("catalog_item_id")
        ):
            continue
        locked = await session.get(
            Projection, {"company_id": cid, "entity_id": member.entity_id},
            with_for_update=True, populate_existing=True,
        )
        if locked is None or (locked.state or {}).get("catalog_item_id"):
            continue
        state = dict(locked.state or {})
        data = {
            "fields_changed": {
                "catalog_item_id": {
                    "old": state.get("catalog_item_id"),
                    "new": anchor.entity_id,
                }
            }
        }
        await emit_event(
            session, company_id=cid, entity_id=locked.entity_id, entity_type="item",
            event_type="item.updated", data=data, actor_id=actor_id, location_id=None,
            source=source,
            idempotency_key=_connector_event_idem(
                f"catalog-family:{anchor.entity_id}:{locked.entity_id}:v{locked.version}",
                data,
            ),
            metadata_={},
        )
        stamped += 1
    return stamped


async def aggregate_sellable_quantity_for_anchor(
    session: AsyncSession, company_id, anchor: Projection
) -> float:
    """Aggregate currently sellable stock for one catalog product family."""
    from celerp_inventory.projections import is_item_available

    cid = uuid.UUID(str(company_id))
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid,
        Projection.entity_type == "item",
    ))).scalars().all()
    return sum(
        float((row.state or {}).get("quantity") or 0)
        for row in catalog_family_rows(rows, anchor)
        if is_item_available(row.state or {})
    )


async def aggregate_sellable_quantity_for_sku(
    session: AsyncSession, company_id, sku: str
) -> float:
    """Legacy SKU-family aggregate retained for callers without an anchor."""
    from celerp_inventory.projections import is_item_available

    cid = uuid.UUID(str(company_id))
    norm = normalize_sku(sku)
    if not norm:
        return 0.0
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid,
        Projection.entity_type == "item",
    ))).scalars().all()
    return sum(
        float((row.state or {}).get("quantity") or 0)
        for row in rows
        if normalize_sku((row.state or {}).get("sku")) == norm
        and is_item_available(row.state or {})
    )


def build_channel_states(
    rows: list[Projection], *, connected_platforms: set[str]
) -> dict[str, dict[str, dict]]:
    """Derive product-family channel state from canonical family identity.

    A link to a store this company is not connected to is history only: it is
    shown as historical and never as linked."""
    keys = _family_keys(rows)
    by_family: dict[tuple[str, str], list[Projection]] = {}
    for row in rows:
        key = keys.get(row.entity_id)
        if key and key[1]:
            by_family.setdefault(key, []).append(row)

    result: dict[str, dict[str, dict]] = {row.entity_id: {} for row in rows}
    for family_rows in by_family.values():
        explicit_roots = [
            row
            for row in family_rows
            if _is_explicit_catalog_anchor_state(row.state or {})
        ]
        product_roots = explicit_roots or [
            row for row in family_rows if _is_product_anchor_state(row.state or {})
        ]
        for platform in PRODUCT_CHANNEL_PLATFORMS:
            linked = [
                row for row in family_rows
                if external_link_for_state(row.state or {}, platform)
            ]
            if not linked:
                continue
            if platform not in connected_platforms:
                for row in family_rows:
                    result[row.entity_id][platform] = {
                        "linked": False, "enabled": False, "historical": True,
                    }
                continue
            if len(product_roots) > 1:
                for row in family_rows:
                    result[row.entity_id][platform] = {
                        "linked": True, "enabled": False, "ambiguous": True,
                    }
                continue
            roots = [
                row for row in linked if _is_product_anchor_state(row.state or {})
            ]
            candidates = roots or linked
            identities = {
                external_identity_key(
                    platform,
                    external_link_for_state(row.state or {}, platform),
                )
                for row in candidates
            }
            if len(identities) != 1:
                for row in family_rows:
                    result[row.entity_id][platform] = {
                        "linked": True, "enabled": False, "ambiguous": True,
                    }
                continue
            try:
                anchor = _choose_outbound_anchor(candidates, platform)
            except ValueError:
                for row in family_rows:
                    result[row.entity_id][platform] = {
                        "linked": True, "enabled": False, "ambiguous": True,
                    }
                continue
            link = external_link_for_state(anchor.state or {}, platform)
            enabled = (
                anchor.is_sync_to_shopify is True
                if platform == "shopify"
                else link.get("sync_enabled") is not False
            )
            state = {
                "linked": True,
                "enabled": bool(enabled),
                "anchor_id": anchor.entity_id,
                "remote_deleted": bool(link.get("remote_deleted")),
            }
            for row in family_rows:
                result[row.entity_id][platform] = dict(state)
    return result

def external_identity_key(platform: str, link: dict) -> tuple[str, str | None]:
    """Canonical external product identity for one platform."""
    product_id = str(link.get("product_id") or "")
    variant_key = _external_variant_key(platform)
    variant = link.get(variant_key)
    return product_id, (str(variant) if variant not in (None, "") else None)


def _choose_outbound_anchor(candidates: list[Projection], platform: str) -> Projection:
    if len(candidates) == 1:
        return candidates[0]
    roots = [r for r in candidates if _is_product_anchor_state(r.state or {})]
    explicit_roots = [
        r for r in roots
        if isinstance(((r.state or {}).get("external_links") or {}).get(platform), dict)
    ]
    if len(explicit_roots) == 1:
        return explicit_roots[0]
    if len(roots) == 1:
        return roots[0]
    raise ValueError(f"Multiple item rows claim the same {platform} product identity")


def _outbound_link(
    row: Projection, platform: str, *, require_sync_flag: bool, inventory_only: bool
) -> dict | None:
    """The row's external link when the row takes part in outbound sync."""
    link = external_link_for_state(row.state or {}, platform)
    if not link or link.get("product_id") in (None, "") or link.get("remote_deleted") is True:
        return None
    if inventory_only and link.get("inventory_sync_paused") is True:
        return None
    if platform == "shopify":
        if require_sync_flag and row.is_sync_to_shopify is not True:
            return None
    elif link.get("sync_enabled") is False:
        return None
    return link


def _outbound_row(
    anchor: Projection, platform: str, quantity: float, sku_anchor_count: int
) -> dict:
    st = anchor.state or {}
    if normalize_sku(st.get("sku")) and sku_anchor_count > 1:
        raise ValueError(
            f"SKU {st.get('sku')!r} matches multiple catalog product anchors"
        )
    return {
        "entity_id": anchor.entity_id,
        "sku": st.get("sku"),
        "name": st.get("name"),
        "description": st.get("description"),
        "sale_price": st.get("sale_price", st.get("retail_price")),
        "quantity": quantity,
        "files": st.get("files") or [],
        "inventory_type": st.get("inventory_type", "stocked"),
        "sell_by": st.get("sell_by"),
        "external_link": external_link_for_state(st, platform),
        **_external_ids(platform, st),
    }


def _outbound_totals(
    rows: list[Projection],
) -> tuple[dict[str, tuple[str, str]], dict[tuple[str, str], float], dict[str, int]]:
    """Family keys, sellable quantity per family and product anchors per SKU."""
    from celerp_inventory.projections import is_item_available

    roots_by_sku: dict[str, int] = {}
    family_keys = _family_keys(rows)
    sellable_by_family: dict[tuple[str, str], float] = {}
    for r in rows:
        st = r.state or {}
        sku_key = normalize_sku(st.get("sku"))
        if sku_key and _is_product_anchor_state(st):
            roots_by_sku[sku_key] = roots_by_sku.get(sku_key, 0) + 1
        family_key = family_keys.get(r.entity_id)
        if family_key and is_item_available(st):
            sellable_by_family[family_key] = (
                sellable_by_family.get(family_key, 0.0)
                + float(st.get("quantity") or 0)
            )
    return family_keys, sellable_by_family, roots_by_sku


def _outbound_rows(
    rows: list[Projection], anchors: list[Projection], platform: str
) -> list[dict]:
    family_keys, sellable_by_family, roots_by_sku = _outbound_totals(rows)
    return [
        _outbound_row(
            r, platform,
            sellable_by_family.get(family_keys.get(r.entity_id), 0.0),
            roots_by_sku.get(normalize_sku((r.state or {}).get("sku")), 0),
        )
        for r in anchors
    ]


async def _items_with_external_id(
    company_id: str,
    platform: str,
    require_sync_flag: bool = False,
    *,
    inventory_only: bool = False,
) -> list[dict]:
    """Return one outbound row per linked external product identity."""
    from celerp.db import SessionLocal as AsyncSessionLocal

    cid = uuid.UUID(str(company_id))
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Projection).where(
                Projection.company_id == cid,
                Projection.entity_type == "item",
            )
        )).scalars().all()

    grouped: dict[tuple[str, str | None], list[Projection]] = {}
    for r in rows:
        link = _outbound_link(
            r, platform,
            require_sync_flag=require_sync_flag, inventory_only=inventory_only,
        )
        if link is not None:
            grouped.setdefault(external_identity_key(platform, link), []).append(r)
    anchors = [
        _choose_outbound_anchor(candidates, platform)
        for candidates in grouped.values()
    ]
    return _outbound_rows(list(rows), anchors, platform)


async def list_item_for_external_identity(
    company_id: str, platform: str, product_id: str, variation_id: str | None
) -> list[dict]:
    """The inventory outbound row for one external product identity, loading
    only the rows that carry the identity and the anchor's catalog family."""
    from celerp.db import SessionLocal as AsyncSessionLocal

    cid = uuid.UUID(str(company_id))
    async with AsyncSessionLocal() as session:
        candidates = [
            r for r in (await session.execute(
                _external_identity_candidates(cid, platform, str(product_id))
            )).scalars().all()
            if _outbound_link(
                r, platform,
                require_sync_flag=(platform == "shopify"), inventory_only=True,
            ) is not None
            and _same_external_identity(
                platform, external_link_for_state(r.state or {}, platform),
                str(product_id), variation_id,
            )
        ]
        if not candidates:
            return []
        anchor = _choose_outbound_anchor(candidates, platform)
        rows = await _catalog_family_candidates(session, cid, anchor)
    return _outbound_rows(rows, [anchor], platform)


async def list_items_with_external_id(company_id: str, platform: str) -> list[dict]:
    """Items currently enabled for outbound synchronization with platform."""
    return await _items_with_external_id(
        company_id, platform, require_sync_flag=(platform == "shopify"),
        inventory_only=True,
    )


async def list_items_modified_since_last_sync(company_id: str, platform: str) -> list[dict]:
    """Outbound product rows; failed idempotent writes retry on reconciliation."""
    return await _items_with_external_id(
        company_id, platform, require_sync_flag=(platform == "shopify")
    )


async def update_item_from_connector(session: AsyncSession, entity_id: str, data: dict, idempotency_key: str,
                                 *, company_id) -> bool:
    """Apply a changed connector re-import to the item it created, as an edit: only the
    fields that differ, a new SKU handled like a SKU edit, and a cost change restated
    with its consequences. An item deleted meanwhile is not recreated (404). Returns
    False when nothing differs."""
    cid = uuid.UUID(str(company_id))
    row = await session.get(Projection, {"company_id": cid, "entity_id": entity_id},
                            with_for_update=True, populate_existing=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    state = row.state or {}
    fields_changed = {key: {"old": state.get(key), "new": value}
                      for key, value in data.items() if state.get(key) != value}
    if not fields_changed:
        return False
    if "sku" in fields_changed and normalize_sku(state.get("sku")) != normalize_sku(data["sku"]):
        await stamp_catalog_family_members(session, cid, entity_id, source="connector")
    event = dict(event_type="item.updated", data={"fields_changed": fields_changed}, actor_id=None,
                 source="connector", idempotency_key=idempotency_key)
    if COST_ITEM_KEYS & set(fields_changed):
        await restate_item_cost(session, cid, entity_id, **event)
    else:
        await emit_event(session, preserve_external_code_conflicts=True, company_id=cid, entity_id=entity_id,
                         entity_type="item", location_id=None, metadata_={}, **event)
    return True


async def upsert_from_connector(company_id: str, item) -> str:
    """
    Create or update an item from a connector payload. Returns the write outcome:
    "created", "updated", or "noop" (this exact content was already applied).

    `item` must have: sku, name, idempotency_key (stable per external item).
    Optional: sale_price, quantity, cost_price, description.

    Uses a fresh DB session so the connector does not need to manage
    session lifecycle. Idempotency is enforced at the ledger level.
    """
    from celerp.db import SessionLocal as AsyncSessionLocal
    from celerp.events.engine import connector_upsert

    idem_key = item.idempotency_key
    if not idem_key:
        raise ValueError("idempotency_key required for connector upserts")

    data = {
        "sku": item.sku,
        "name": item.name,
    }
    if item.sale_price is not None:
        data["sale_price"] = item.sale_price
        data["retail_price"] = item.sale_price   # canonical selling-price field
    if item.quantity:
        data["quantity"] = item.quantity
    if getattr(item, "cost_price", None) is not None:
        data["cost_price"] = item.cost_price     # else margin/COGS/valuation read zero cost
    if getattr(item, "description", None):
        data["description"] = item.description

    async with AsyncSessionLocal() as session:
        # Derived price lists are computed from the base at read time; a store-synced price
        # must not be stored under a derived key (it would be masked on every read, then
        # resurface as a stale manual price if the factor is ever removed).
        from celerp.services.pricing import derived_price_keys, get_price_config
        derived = derived_price_keys((await get_price_config(session, company_id))[0])
        for key in derived:
            data.pop(key, None)
        # Stock from an accounting system is held on that system's books, so it arrives as
        # a draft: the user makes it available once it is stock held here.
        outcome = await connector_upsert(
            session, company_id=company_id, entity_type="item",
            event_type="item.created", idem_key=idem_key, data=data, on_create={"status": "draft"},
            update=functools.partial(update_item_from_connector, company_id=company_id),
        )
        await session.commit()
        return outcome


# ---------------------------------------------------------------------------
# Semantic catalog import
# ---------------------------------------------------------------------------
#
# One writer, three transports: the browser CSV importer (POST /import/rows),
# the agent commit (POST /import/commit), and the raw event batch
# (POST /import/batch) all converge on write_import_batch below. The business
# transformation (location resolution, unit canonicalization, quantity/rate
# derivation, dynamic attributes, idempotency) lives in build_import_records so
# it is applied identically no matter which transport delivered the rows.


class ImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


class BatchImportResult(BaseModel):
    created: int
    skipped: int
    updated: int = 0
    errors: list[str]
    batch_id: str | None = None


class BatchImportRequest(BaseModel):
    records: list[ImportRecord] = Field(..., max_length=500)
    filename: str | None = None
    upsert: bool = False


# Row keys the importer reads as item fields although the item model does not
# keep them top-level: ``qty`` is read as the quantity, a carat weight becomes
# the weight, and the pieces count is written through the item's own pieces handling.
_IMPORT_ROW_KEYS: frozenset[str] = frozenset({"qty", "weight_ct", "pieces"})


def is_item_field_key(key: str) -> bool:
    """True when an import row key names an item field rather than a custom attribute.

    Exactly the keys the importer reads as item fields, so it is also the set a
    custom attribute name may not take on any import transport.
    """
    return is_core_item_key(key) or key in _IMPORT_ROW_KEYS or key.endswith("_price" + PRICE_BASIS_SUFFIX)

# Max distinct values before an attribute column is treated as free-text instead
# of a select field when a schema is inferred from the import.
_DROPDOWN_THRESHOLD = 30


def resolve_import_category(value: str, category_keys, display_names: dict) -> tuple[str, str | None]:
    """Resolve a source category to the company's canonical category key.

    Returns ``(category, error)``. An exact key wins, then a unique case-insensitive
    key, then a unique case-insensitive display label. Several candidates are an
    error rather than a guess; an unknown value is kept as a custom category.
    """
    value = str(value or "").strip()
    if not value:
        return "", None
    keys = set(category_keys) | set(display_names)
    if value in keys:
        return value, None
    folded = value.casefold()
    for candidates in (
        sorted(k for k in keys if k.casefold() == folded),
        sorted(k for k in keys if str(display_names.get(k) or "").strip().casefold() == folded),
    ):
        if len(candidates) == 1:
            return candidates[0], None
        if candidates:
            return value, f"Category '{value}' matches several categories: {', '.join(candidates)}"
    return value, None


# Source header words that name a weight unit, and the canonical unit they mean.
# Recognised only when the header says nothing else, so an unfamiliar header
# never invents a unit.
_WEIGHT_UNIT_WORDS: dict[str, str] = {
    "ct": "carat", "cts": "carat", "carat": "carat", "carats": "carat",
    "g": "gram", "gr": "gram", "gram": "gram", "grams": "gram",
    "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
}
_BASIS_UNIT_WORDS: dict[str, str] = {
    **_WEIGHT_UNIT_WORDS,
    "pc": "piece", "pcs": "piece", "piece": "piece", "pieces": "piece", "each": "piece", "ea": "piece",
}
# Row key carrying the unit a mapped source price is quoted per, e.g.
# ``retail_price_basis``; the importer accepts the price only for items sold by it.
PRICE_BASIS_SUFFIX = "_basis"


def weight_unit_from_header(header: str, target: str) -> str | None:
    """The canonical weight unit a source header names for a weight ``target``.

    ``weight_ct`` and ``Grams`` name units for ``weight``; ``Gross weight g`` names
    one for ``gross_weight``. A header with any other word names no unit.
    """
    skip = {"weight", "wt"} | ({"gross"} if target == "gross_weight" else set())
    words = [w for w in re.findall(r"[a-z]+", header.lower()) if w not in skip]
    return _WEIGHT_UNIT_WORDS.get(words[0]) if len(words) == 1 else None


# ISO codes that are also ordinary English words. In a header they mean a
# currency only when written as a code: in brackets, joined by an underscore, or
# right after price/cost. "Top price" names no currency.
_PLAIN_WORD_CURRENCIES = frozenset({
    "all", "bob", "cop", "cup", "gel", "mad", "mop", "pen", "rub", "sos", "top", "try",
})
# Currency symbols that name exactly one currency. Any other currency symbol
# ($, ¥, £, ...) is shared by several currencies and must be stated as a code.
_CURRENCY_SYMBOLS: dict[str, str] = {
    "€": "EUR", "฿": "THB", "₹": "INR", "₩": "KRW", "₫": "VND", "₱": "PHP",
    "₺": "TRY", "₽": "RUB", "₪": "ILS", "₦": "NGN", "₴": "UAH",
}
_PRICE_WORDS = frozenset({"price", "cost"})


def _header_currencies(header: str) -> list[str]:
    """Every ISO currency code a header states, in any case and anywhere in it."""
    words = re.findall(r"[A-Za-z]+", header)
    codes: list[str] = []
    for i, word in enumerate(words):
        code = word.upper()
        if len(word) != 3 or code not in ISO_4217_CURRENCIES:
            continue
        if word.lower() in _PLAIN_WORD_CURRENCIES and not (
            re.search(rf"[(\[]\s*{word}\s*[)\]]", header)
            or re.search(rf"(?<=_){word}(?![A-Za-z])|(?<![A-Za-z]){word}(?=_)", header)
            or (i > 0 and words[i - 1].lower() in _PRICE_WORDS)
        ):
            continue
        codes.append(code)
    return codes


def _header_currency_error(header: str, currency: str) -> tuple[str, str] | None:
    """``(code, message)`` when a price header states a currency the import cannot honour.

    An ISO code other than the company currency is a mismatch. Without a code, a
    symbol naming one currency is read as that currency, and a symbol several
    currencies share is ambiguous. The importer never converts a price.
    """
    codes = _header_currencies(header)
    if not codes:
        symbols = [ch for ch in header if unicodedata.category(ch) == "Sc"]
        shared = [ch for ch in symbols if ch not in _CURRENCY_SYMBOLS]
        if shared:
            return "price_currency_ambiguous", (
                f"Column '{header}' uses the symbol {shared[0]}, which several currencies share; "
                f"state the currency code in the header, for example ({currency})"
            )
        codes = [_CURRENCY_SYMBOLS[ch] for ch in symbols]
    foreign = next((code for code in codes if code != currency), None)
    if foreign:
        return "price_currency_mismatch", (
            f"Column '{header}' is priced in {foreign} but the company currency is {currency}"
        )
    return None


def _header_basis(header: str) -> str | None:
    """The unit a price header is quoted per, written as ``/basis`` or ``per basis``.

    Returns the canonical unit, the raw word for a basis Celerp has no unit for
    (``box``, ``dozen``, ``100g``), or None when the header states no basis
    (``per unit`` is the normal meaning). Underscores and hyphens separate words
    like spaces, so ``Price_per_box`` and ``price-per-box`` read as ``price per box``.
    """
    lower = re.sub(r"[_-]+", " ", header.lower())
    match = re.search(r"/\s*([a-z0-9.]+)", lower) or re.search(r"\bper\s+([a-z0-9.]+)", lower)
    if not match or match.group(1) == "unit":
        return None
    return _BASIS_UNIT_WORDS.get(match.group(1), match.group(1))


_WEIGHT_TARGETS = ("weight", "gross_weight")


@dataclass
class SourceSemantics:
    errors: list[dict]              # {"row": 0, "field": source column, "code", "message"}
    weight_units: dict[str, str]    # weight target -> unit its header names, when no unit column is mapped
    price_basis: dict[str, str]     # price target -> unit its source column is quoted per


def source_header_semantics(mapping: dict[str, str], currency: str) -> SourceSemantics:
    """Read what source headers say that their values alone do not.

    Shared by the browser mapping step and the file preview so a header means the
    same thing on every transport. A foreign or ambiguous currency, a total mapped
    as a unit price, or a basis Celerp cannot check is an error: the importer
    never strips the annotation and imports the bare number. ``mapping`` is
    ``{column: target}``.
    """
    errors: list[dict] = []
    price_basis: dict[str, str] = {}
    targets = set(mapping.values())

    def _error(col: str, code: str, message: str) -> None:
        errors.append({"row": 0, "field": col, "code": code, "message": message})

    for col, target in mapping.items():
        is_total = target.endswith("_price_total")
        if not (is_total or target.endswith("_price")):
            continue
        currency_error = _header_currency_error(col, currency)
        if currency_error:
            _error(col, *currency_error)
            continue
        basis = _header_basis(col)
        if basis and (is_total or basis not in _BASIS_UNIT_WORDS.values()):
            _error(col, "price_basis_unsupported",
                   f"Column '{col}' is priced per {basis}, which cannot be imported as {target}")
        elif basis:
            price_basis[target] = basis
        elif not is_total and "total" in re.findall(r"[a-z]+", col.lower()):
            _error(col, "price_total_as_unit",
                   f"Column '{col}' is a total; map it to {target}_total instead of {target}")

    weight_units: dict[str, str] = {}
    for target in _WEIGHT_TARGETS:
        sources = [col for col, mapped in mapping.items() if mapped == target]
        unit = weight_unit_from_header(sources[0], target) if len(sources) == 1 else None
        if unit and f"{target}_unit" not in targets:
            weight_units[target] = unit
    return SourceSemantics(errors=errors, weight_units=weight_units, price_basis=price_basis)


def apply_source_semantics(rows: list[dict], semantics: SourceSemantics) -> list[dict]:
    """Carry header meaning onto mapped rows: each weight unit and each price basis."""
    out: list[dict] = []
    for row in rows:
        row = dict(row)
        for target, unit in semantics.weight_units.items():
            if str(row.get(target) or "").strip():
                row[f"{target}_unit"] = unit
        for target, basis in semantics.price_basis.items():
            row[target + PRICE_BASIS_SUFFIX] = basis
        out.append(row)
    return out


def _to_float(val) -> float | None:
    """The finite number a cell holds, or None when it is blank or holds none.

    The semantic preflight has already reported every typed cell that is not a
    finite number, so None here only ever stands for a blank cell on a clean row.
    """
    s = str(val).strip() if val is not None else ""
    if not s:
        return None
    try:
        return finite_float(s)
    except ValueError:
        return None


def _source_weight(row: dict, unit_canonical: dict[str, str]) -> tuple[float | None, str]:
    """The row's weight and its unit as written (canonical when known).

    ``weight_ct`` is a carat weight by name, so it carries its unit.
    """
    raw_unit = str(row.get("weight_unit", "") or "").strip()
    unit = unit_canonical.get(raw_unit.lower()) or raw_unit
    weight = _to_float(row.get("weight"))
    if weight is None and _to_float(row.get("weight_ct")) is not None:
        return _to_float(row.get("weight_ct")), unit or "carat"
    return weight, unit


def _derive_import_qty(
    row: dict, sell_by: str, unit_map: dict[str, dict], unit_canonical: dict[str, str],
) -> tuple[float, dict | None]:
    """Derive the stock quantity from an import row.

    Returns ``(quantity, error)``. Priority:
    1. An explicit ``quantity`` or ``qty`` column is trusted unconditionally.
    2. Otherwise fall back to the semantic field for the unit type:
       - pieces-type (e.g. ``piece``) -> ``pieces`` column
       - weight-type (e.g. ``carat``, ``gram``) -> the weight, only when its unit
         is known and is the selling unit; weights are never converted
       - other (service, volume, length, unknown) -> 0.0
    """
    explicit = _to_float(row.get("quantity")) if "quantity" in row else _to_float(row.get("qty"))
    if explicit is not None:
        return explicit, None
    if is_pieces_unit(sell_by, unit_map):
        return _to_float(row.get("pieces")) or 0.0, None
    if is_weight_unit(sell_by, unit_map):
        weight, unit = _source_weight(row, unit_canonical)
        if weight is None:
            return 0.0, None
        if unit not in unit_map:
            return 0.0, {
                "field": "weight", "code": "weight_unit_unknown",
                "message": (f"Weight unit '{unit}' is not one of the company's units" if unit
                            else "Weight has no unit") + f"; add a quantity in {sell_by}",
            }
        if unit != sell_by:
            return 0.0, {
                "field": "weight", "code": "weight_unit_mismatch",
                "message": f"Weight is in {unit} but the item sells by {sell_by}; add a quantity in {sell_by}",
            }
        return weight, None
    return 0.0, None


# Row columns an item's amount is read from, in the order _derive_import_qty reads them.
_AMOUNT_SOURCE_KEYS = ("quantity", "qty", "pieces", "weight", "weight_ct")


def _amount_errors(row: dict, qty: float | None, sell_by: str, unit_map: dict[str, dict]) -> list[dict]:
    """The amount rules the writer enforces, as row errors: no negative amount,
    and a written quantity (``qty``) no finer than its selling unit allows."""
    errors = [
        {"field": key, "code": "negative_value", "message": f"{key} cannot be negative"}
        for key in (*_AMOUNT_SOURCE_KEYS, "gross_weight")
        if (value := _to_float(row.get(key))) is not None and value < 0
    ]
    if (
        errors or qty is None or not math.isfinite(qty)
        or sell_by in SERVICE_SELL_BY or sell_by not in unit_map
    ):
        return errors
    decimals = unit_map[sell_by]["decimals"]
    if exceeds_precision(qty, decimals):
        field = next((k for k in _AMOUNT_SOURCE_KEYS if _to_float(row.get(k)) is not None), "quantity")
        errors.append({
            "field": field, "code": "quantity_precision",
            "message": f"{field} {qty:g} allows {decimals} decimal places for {sell_by}",
        })
    return errors


def _collect_category_attributes(rows: list[dict]) -> dict[str, dict[str, list[str]]]:
    """Return {category: {col: [distinct_values]}} for all attribute columns."""
    result: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        cat = str(row.get("category", "") or "").strip() or "_uncategorized"
        if cat not in result:
            result[cat] = {}
        for k, v in row.items():
            if is_item_field_key(k):
                continue
            v_str = str(v).strip() if v is not None else ""
            if not v_str:
                continue
            if k not in result[cat]:
                result[cat][k] = []
            if v_str not in result[cat][k]:
                result[cat][k].append(v_str)
    return result


def _infer_category_schemas(cat_attr_values: dict[str, dict[str, list[str]]]) -> dict[str, list[dict]]:
    """Convert collected attribute values into schema field definitions."""
    schemas: dict[str, list[dict]] = {}
    for cat, cols in cat_attr_values.items():
        if cat == "_uncategorized":
            continue
        fields = []
        for key, distinct_vals in cols.items():
            if len(distinct_vals) <= _DROPDOWN_THRESHOLD:
                ftype = "select"
                options = sorted(distinct_vals)
            else:
                ftype = "text"
                options = []
            fields.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "type": ftype,
                "options": options,
            })
        if fields:
            schemas[cat] = fields
    return schemas


# Item import columns, single-sourced here for every transport (the browser
# mapping UI and the agent preview/commit routes). Price columns are dynamic:
# one unit-price column per importable price list, plus a virtual "_total"
# column that back-calculates the unit price at confirm time.
ITEM_IMPORT_BASE_COLS = ["sku", "name", "sell_by", "category", "quantity"]
ITEM_IMPORT_TAIL_COLS = [
    "weight", "weight_unit", "gross_weight", "gross_weight_unit", "pieces",
    "barcode", "hs_code", "purchase_sku", "purchase_name", "purchase_unit",
    "purchase_conversion_factor", "inventory_type", "gtin", "rfid_epc",
    "short_description", "description", "notes", "location_name",
]
# Numeric row keys: each must hold a finite number. ``qty`` and ``weight_ct`` are
# not offered as mapping targets but are read as the quantity and the weight.
_ITEM_IMPORT_NUMBER_KEYS = ("quantity", "qty", "weight", "weight_ct", "gross_weight", "pieces", "purchase_conversion_factor")

VALID_INVENTORY_TYPES: frozenset[str] = frozenset({"stocked", "component", "non_stocked", "service", "freight"})


def _unsupported_item_field_errors(spec: CsvImportSpec, row: dict) -> list[dict]:
    """An error for each item field with a value on the row that import cannot set.

    Import sets the spec's columns, the row keys it reads (``qty``, ``weight_ct``,
    ``pieces``) and the price basis of each price column. A price key naming no
    importable price list is an unknown target; any other item field is refused
    by name. Neither is written or stored as a custom attribute.
    """
    importable = {*spec.cols, *_IMPORT_ROW_KEYS, *(col + PRICE_BASIS_SUFFIX for col in spec.cols if col.endswith("_price"))}
    errors = []
    for key, value in row.items():
        if not is_item_field_key(key) or key in importable or not str(value if value is not None else "").strip():
            continue
        if key.endswith(("_price", "_price_total", "_price" + PRICE_BASIS_SUFFIX)):
            errors.append({"field": key, "code": "unknown_target",
                           "message": f"{key} is not a price list of this company; remove the column"})
        else:
            errors.append({"field": key, "code": "reserved_field_unsupported",
                           "message": f"{key} is an item field that import cannot set; remove the column"})
    return errors


_ITEM_CODE_CHECKS = (
    ("sku", validate_sku, "invalid_sku"),
    ("barcode", validate_barcode, "invalid_barcode"),
    ("gtin", validate_gtin, "invalid_value"),
    ("rfid_epc", validate_rfid_epc, "invalid_value"),
)


def _item_code_error(row: dict) -> dict | None:
    """The first invalid inventory type or identifier code on the row, as a row error."""
    inventory_type = str(row.get("inventory_type") or "").strip()
    if inventory_type and inventory_type not in VALID_INVENTORY_TYPES:
        return {"field": "inventory_type", "code": "invalid_value",
                "message": f"inventory_type must be one of {sorted(VALID_INVENTORY_TYPES)}"}
    for field, validate, code in _ITEM_CODE_CHECKS:
        try:
            validate(str(row.get(field) or "").strip())
        except ValueError as exc:
            return {"field": field, "code": code, "message": str(exc)}
    return None


def importable_price_lists(price_lists: list[dict]) -> list[dict]:
    """Price lists whose values can be imported. Derived lists are computed from
    the base price list at read time, so the mapper never offers their columns."""
    return [pl for pl in price_lists if pl.get("name") and not is_derived(pl)]


def build_item_import_spec(price_lists: list[dict]) -> CsvImportSpec:
    """Build the item import spec with dynamic price columns from the company's
    price lists. Shared by the browser mapper and the agent preview/commit."""
    price_cols = [price_key(pl["name"]) for pl in importable_price_lists(price_lists)]
    price_total_cols = [f"{col}_total" for col in price_cols]
    type_map = {col: finite_float for col in (*_ITEM_IMPORT_NUMBER_KEYS, *price_cols, *price_total_cols)}
    return CsvImportSpec(
        cols=ITEM_IMPORT_BASE_COLS + price_cols + price_total_cols + ITEM_IMPORT_TAIL_COLS,
        # sell_by may come from the category's default unit, so it is checked
        # after resolution by build_import_records, not as a mapped column.
        required={"name"},
        type_map=type_map,
    )


def item_price_mutex_groups(price_lists: list[dict]) -> list[list[str]]:
    """Target groups a mapping may use at most one of: a price list's unit price
    and its total, which would otherwise both claim the same stored price."""
    return [[key, f"{key}_total"] for key in (price_key(pl["name"]) for pl in importable_price_lists(price_lists))]


@dataclass
class ImportBuild:
    records: list[dict]              # ImportRecord-shaped dicts ready for the committer
    record_rows: list[int]           # the 1-based input row of each record
    errors: list[dict]              # {"row", "field", "code", "message"}
    locations_to_create: list[str]
    rows: list[dict]                # input rows with each category resolved to its canonical key


def import_created_item_id(company_id, key: str) -> str:
    """Deterministic entity id of the item a create row makes under its retry key."""
    return f"item:{uuid.uuid5(uuid.NAMESPACE_URL, f'{company_id}:{key}')}"


async def build_import_records(
    session: AsyncSession,
    company_id,
    rows: list[dict],
    *,
    upsert: bool,
    create_missing_locations: bool = False,
    create_key: str | None = None,
) -> ImportBuild:
    """Transform mapped business rows into semantic item import records.

    Nothing is written. Retry identity and item identity are deliberately
    separate. Create rows get a per-import row key; upserts first resolve one
    existing item by physical barcode or an unambiguous SKU, then key the patch
    by its target and canonical content.

    Missing named locations are reported in ``locations_to_create`` and are
    accepted only when the caller is authorised to create company locations; a
    row naming one keeps ``location_id`` empty until import_items creates it.
    An upsert that omits ``location_name`` preserves the target location.

    ``create_key`` is the import's operation key. Items this same import created
    are never upsert targets, so an exact retry plans every row against the state
    before the import, exactly as the first attempt did.

    Under the company lock (an import commit) the rows the plan depends on are
    pinned until that commit, in a stable order: the company's locations FOR
    SHARE and the upsert targets FOR UPDATE. A concurrent item edit or location
    change then waits for the import and lands on top of what it wrote, instead
    of slipping in between the plan and the write.
    """
    pin = holds_company_lock(session, company_id)
    loc_query = select(Location).where(Location.company_id == company_id).order_by(Location.id)
    if pin:
        loc_query = loc_query.with_for_update(read=True).execution_options(populate_existing=True)
    loc_rows = (await session.execute(loc_query)).scalars().all()
    location_map: dict[str, str] = {loc.name: str(loc.id) for loc in loc_rows}

    default_location_id: str | None = None
    if len(loc_rows) == 1:
        default_location_id = str(loc_rows[0].id)
    else:
        for loc in loc_rows:
            if loc.is_default:
                default_location_id = str(loc.id)
                break

    loc_names_needed: list[str] = []
    for row in rows:
        name = str(row.get("location_name", "") or "").strip()
        if name and name not in location_map and name not in loc_names_needed:
            loc_names_needed.append(name)

    company = await session.get(Company, company_id)
    company_settings = (company.settings or {}) if company else {}
    currency = company_settings.get("currency") or "USD"
    category_keys = list(company_settings.get("category_schemas") or {})
    category_names = dict(company_settings.get("category_display_names") or {})

    units = await get_company_units(session, company_id)
    unit_canonical = {u["name"].lower(): u["name"] for u in units}
    unit_map = build_unit_map(units)

    # Resolve upsert targets once for the batch. Barcode is a physical-lot
    # identity, but older data and imports can leave one barcode on several items,
    # so every resolvable holder is kept and a shared barcode never picks one
    # arbitrarily. SKU is intentionally non-unique and is usable only when exactly
    # one current item has it.
    by_barcode: dict[str, list[Projection]] = {}
    by_sku: dict[str, list[Projection]] = {}
    own_ids = {
        import_created_item_id(company_id, f"{create_key}:row:{n}") for n in range(1, len(rows) + 1)
    } if create_key else set()
    if upsert:
        barcodes = {str(r.get("barcode") or "").strip() for r in rows} - {""}
        skus = {str(r.get("sku") or "").strip() for r in rows} - {""}
        predicates = []
        if barcodes:
            predicates.append(Projection.state["barcode"].as_string().in_(barcodes))
        if skus:
            predicates.append(Projection.state["sku"].as_string().in_(skus))
        if predicates:
            match_query = select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
                or_(*predicates),
            ).order_by(Projection.entity_id)
            if pin:
                match_query = match_query.with_for_update().execution_options(populate_existing=True)
            matches = (await session.execute(match_query)).scalars().all()
            for proj in matches:
                if proj.entity_id in own_ids:
                    continue
                state = proj.state or {}
                barcode = str(state.get("barcode") or "").strip()
                sku = str(state.get("sku") or "").strip()
                status = str(state.get("status") or "").lower()
                if barcode and status not in PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES:
                    by_barcode.setdefault(barcode, []).append(proj)
                if sku:
                    by_sku.setdefault(sku, []).append(proj)

    def _has_value(row: dict, key: str) -> bool:
        return key in row and str(row.get(key) or "").strip() != ""

    records: list[dict] = []
    record_rows: list[int] = []
    errors: list[dict] = []
    resolved_rows: list[dict] = []
    for i, row in enumerate(rows):
        category, category_error = resolve_import_category(row.get("category"), category_keys, category_names)
        row = {**row, "category": category} if category else row
        resolved_rows.append(row)
        if category_error:
            errors.append({"row": i + 1, "field": "category", "code": "category_ambiguous", "message": category_error})
            continue
        code_error = _item_code_error(row)
        if code_error:
            errors.append({"row": i + 1, **code_error})
            continue
        sku =str(row.get("sku", "") or "").strip()
        name = str(row.get("name", "") or "").strip()
        barcode = str(row.get("barcode", "") or "").strip()
        loc_name = str(row.get("location_name", "") or "").strip()

        target: Projection | None = None
        if upsert:
            barcode_holders = by_barcode.get(barcode, []) if barcode else []
            sku_matches = by_sku.get(sku, []) if sku else []
            if len(barcode_holders) > 1:
                # A shared barcode is usable only when the row's SKU picks out exactly
                # one of its holders.
                narrowed = [
                    p for p in barcode_holders
                    if sku and str((p.state or {}).get("sku") or "").strip() == sku
                ]
                if len(narrowed) != 1:
                    errors.append({
                        "row": i + 1,
                        "field": "barcode",
                        "code": "barcode_ambiguous",
                        "message": (
                            f"Barcode '{barcode}' is shared by {len(barcode_holders)} items; "
                            "include a SKU that identifies one of them"
                        ),
                    })
                    continue
                target = narrowed[0]
            elif barcode_holders:
                barcode_target = barcode_holders[0]
                if len(sku_matches) == 1 and sku_matches[0].entity_id != barcode_target.entity_id:
                    errors.append({
                        "row": i + 1,
                        "field": "sku",
                        "code": "sku_barcode_conflict",
                        "message": "SKU and barcode resolve to different existing items",
                    })
                    continue
                target = barcode_target
            elif len(sku_matches) == 1:
                target = sku_matches[0]
            elif len(sku_matches) > 1:
                errors.append({
                    "row": i + 1,
                    "field": "sku",
                    "code": "sku_ambiguous",
                    "message": f"SKU '{sku}' matches multiple lots; include a barcode to choose one",
                })
                continue

        # Location is required for a new item. Upsert without an explicit location
        # preserves the target's current location rather than inventing a default.
        if loc_name:
            location_id = location_map.get(loc_name)
            missing_named_location = loc_name in loc_names_needed
            if not location_id and not (missing_named_location and create_missing_locations):
                errors.append({
                    "row": i + 1,
                    "field": "location_name",
                    "code": "location_create_denied",
                    "message": f"Location '{loc_name}' does not exist and your role cannot create locations",
                })
                continue
        elif target is not None:
            location_id = str(target.location_id) if target.location_id else None
        else:
            location_id = default_location_id
            if not location_id:
                errors.append({
                    "row": i + 1,
                    "field": "location_name",
                    "code": "location_unresolved",
                    "message": "No location resolved: add a location_name column or set a default location",
                })
                continue

        # A new item takes its category's defaults for the fields the row leaves
        # blank, as ordinary item creation does. A default weight unit names the
        # unit of a written weight and is never a conversion. Upserts keep the
        # target's own values for blank cells.
        defaults = category_item_defaults(category)
        if target is None:
            row = {**row, **{
                field: value for field, value in defaults.items()
                if not _has_value(row, field)
                and (field != "weight_unit" or _to_float(row.get("weight")) is not None)
            }}
        sell_by = (
            unit_canonical.get(str(row.get("sell_by", "") or "").strip().lower())
            or str(row.get("sell_by", "") or "").strip()
            or defaults.get("sell_by")
            or ""
        )
        # The committer rejects these too; reporting them here keeps preview and
        # commit in agreement on every row.
        if target is None and not sell_by:
            errors.append({
                "row": i + 1,
                "field": "sell_by",
                "code": "sell_by_unresolved",
                "message": "No selling unit: add a sell_by value or use a category that has a default unit",
            })
            continue
        if sell_by and unit_canonical and sell_by not in unit_canonical.values():
            errors.append({
                "row": i + 1,
                "field": "sell_by",
                "code": "sell_by_invalid",
                "message": f"sell_by '{sell_by}' is not one of the company's units",
            })
            continue
        qty, qty_error = _derive_import_qty(row, sell_by, unit_map, unit_canonical)
        if qty_error:
            errors.append({"row": i + 1, **qty_error})
            continue
        # A price quoted per some unit is the item's unit price only when the item
        # sells by that unit; anything else would need a conversion Celerp does not make.
        item_sell_by = sell_by or str(((target.state or {}) if target is not None else {}).get("sell_by") or "")
        basis_error = next((
            {
                "row": i + 1, "field": key[: -len(PRICE_BASIS_SUFFIX)], "code": "price_basis_mismatch",
                "message": (
                    f"{key[: -len(PRICE_BASIS_SUFFIX)]} is priced per {basis} "
                    f"but the item sells by {item_sell_by or 'no unit'}"
                ),
            }
            for key, basis in row.items()
            if key.endswith("_price" + PRICE_BASIS_SUFFIX)
            and _to_float(row.get(key[: -len(PRICE_BASIS_SUFFIX)])) is not None
            and (unit_canonical.get(str(basis or "").strip().lower()) or str(basis or "").strip()) != item_sell_by
        ), None)
        if basis_error:
            errors.append(basis_error)
            continue
        amount_source = any(_has_value(row, k) for k in _AMOUNT_SOURCE_KEYS)
        amount_errors = _amount_errors(row, qty if amount_source else None, item_sell_by, unit_map)
        if amount_errors:
            errors.extend({"row": i + 1, **e} for e in amount_errors)
            continue

        def _flt(key: str, _row: dict = row) -> float | None:
            return _to_float(_row.get(key))

        attrs: dict = {}
        for key, value in row.items():
            if is_item_field_key(key):
                continue
            value_s = str(value).strip() if value is not None else ""
            if value_s:
                attrs[key] = value_s

        weight, weight_unit = _source_weight(row, unit_canonical)

        def _text(key: str, _row: dict = row) -> str | None:
            return str(_row.get(key, "") or "").strip() or None

        def _unit(key: str, _row: dict = row) -> str | None:
            raw = _text(key, _row)
            return unit_canonical.get(raw.lower(), raw) if raw else None

        data = {
            "sku": sku,
            "name": name,
            "quantity": qty,
            "category": category or None,
            "weight": weight,
            "weight_unit": weight_unit or None,
            "gross_weight": _flt("gross_weight"),
            "gross_weight_unit": _unit("gross_weight_unit"),
            "pieces": _flt("pieces"),
            "sell_by": sell_by or None,
            "barcode": barcode or None,
            "hs_code": _text("hs_code"),
            "short_description": _text("short_description"),
            "description": _text("description"),
            "notes": _text("notes"),
            "location_id": location_id,
            "attributes": attrs,
        }
        # Written only when the row gives them, so the item model's own defaults
        # (purchase unit from the selling unit, a factor of 1, a stocked item) apply.
        data.update({key: value for key, value in (
            ("purchase_sku", _text("purchase_sku")),
            ("purchase_name", _text("purchase_name")),
            ("purchase_unit", _unit("purchase_unit")),
            ("purchase_conversion_factor", _flt("purchase_conversion_factor")),
            ("inventory_type", _text("inventory_type")),
            ("gtin", _text("gtin")),
            ("rfid_epc", validate_rfid_epc(_text("rfid_epc"))),
        ) if value is not None})

        # A total is divided by the quantity it covers: the row's own quantity, or
        # the target's on an upsert that does not change quantity. A total with no
        # positive quantity, or next to its own unit price, has no one meaning.
        price_qty = qty
        if target is not None and not amount_source:
            price_qty = _to_float((target.state or {}).get("quantity")) or 0.0

        price_errors: list[dict] = []
        for col_key in row:
            if col_key.endswith("_price") and _flt(col_key) is not None:
                data[col_key] = _flt(col_key)
            elif col_key.endswith("_price_total") and _flt(col_key) is not None:
                unit_key = col_key[: -len("_total")]
                total_val = _flt(col_key)
                if _flt(unit_key) is not None:
                    price_errors.append({
                        "row": i + 1, "field": col_key, "code": "price_unit_total_conflict",
                        "message": f"{unit_key} and {col_key} are both given; keep one of them",
                    })
                elif not price_qty > 0:
                    price_errors.append({
                        "row": i + 1, "field": col_key, "code": "price_total_needs_quantity",
                        "message": f"{col_key} needs a quantity above zero to divide by",
                    })
                elif unit_key == "cost_price":
                    data["cost_total"] = total_val
                else:
                    data[unit_key] = to_stored_float(unit_price_from_total(total_val, price_qty, currency))
        if price_errors:
            errors.extend(price_errors)
            continue

        if target is None:
            idem = f"row:{i + 1}"
            data["idempotency_key"] = idem
            records.append({
                "entity_id": f"item:{uuid.uuid4()}",
                "event_type": "item.created",
                "data": data,
                "source": "csv_import",
                "idempotency_key": idem,
            })
            record_rows.append(i + 1)
            continue

        current = target.state or {}
        if _has_value(row, "sell_by") and sell_by != str(current.get("sell_by") or "") and not amount_source:
            errors.append({
                "row": i + 1,
                "field": "sell_by",
                "code": "sell_by_change_needs_quantity",
                "message": "Changing sell_by during upsert requires quantity, pieces, or weight",
            })
            continue

        # Blank cells are non-destructive during upsert. This keeps a narrow CSV
        # from clearing fields it never intended to manage. Explicit clearing stays
        # on the normal item edit API where validation and audit semantics exist.
        patch: dict = {"name": name}
        if sku:
            patch["sku"] = sku
        if _has_value(row, "sell_by"):
            patch["sell_by"] = sell_by
        if amount_source:
            patch["quantity"] = qty
        for key in (
            "category", "weight", "weight_unit", "gross_weight", "gross_weight_unit",
            "pieces", "barcode", "hs_code", "short_description", "description", "notes",
            "purchase_sku", "purchase_name", "purchase_unit", "purchase_conversion_factor",
            "inventory_type", "gtin", "rfid_epc",
        ):
            if _has_value(row, key):
                value = data.get(key)
                if value is not None:
                    patch[key] = value
        if loc_name:
            patch["location_id"] = location_id
        if attrs:
            merged_attrs = dict(current.get("attributes") or {})
            merged_attrs.update(attrs)
            patch["attributes"] = merged_attrs
        for key, value in data.items():
            if (key.endswith("_price") or key == "cost_total") and value is not None:
                patch[key] = value

        # A named location is keyed by its name, so the key is the same before
        # and after the import creates that location.
        keyed_patch = {**patch, "location_id": {"name": loc_name}} if loc_name else patch
        canonical_patch = json.dumps(keyed_patch, sort_keys=True, separators=(",", ":"), default=str)
        idem = f"csv:item:{target.entity_id}:patch:{hashlib.sha256(canonical_patch.encode()).hexdigest()}"
        records.append({
            "entity_id": target.entity_id,
            "event_type": "item.patched",
            "data": patch,
            "source": "csv_import",
            "idempotency_key": idem,
        })
        record_rows.append(i + 1)

    return ImportBuild(
        records=records, record_rows=record_rows, errors=errors,
        locations_to_create=loc_names_needed, rows=resolved_rows,
    )


def import_preview_hash(inputs: dict) -> str:
    """Stable hash of every input that can change an import's result.

    One canonical serialization for every preview and the commit that echoes it,
    so key order and whitespace never make an unchanged import look stale.
    """
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


_CELL_ERROR_MESSAGES = {
    "required": "Missing {col}",
    "not_finite": "{col} must be a finite number",
    "invalid_value": "Invalid {col}",
}


@dataclass
class SemanticImportPlan:
    """What an item import writes, decided before anything is written."""
    rows: list[dict]                 # the rows as the importer resolved them
    records: list[dict]              # ImportRecord-shaped dicts for the committer
    record_rows: list[int]           # the 1-based input row of each record
    errors: list[dict]               # {"row", "field", "code", "message"}; the writer's rejections
    locations_to_create: list[str]
    semantic_fingerprint: str        # changes whenever what the rows would write changes


class ImportRejected(Exception):
    """The preflight rejected rows; nothing was written."""

    def __init__(self, errors: list[dict]):
        super().__init__(f"{len(errors)} import row errors")
        self.errors = errors


def import_operation_key(idempotency_key: str | None, rows: list[dict], upsert: bool) -> str:
    """The retry identity of one import: the caller's key together with the content.

    An exact re-submit of the same mapped rows under the same key (or with no
    key) is the same import: a no-op, or a resume where an interrupted attempt
    stopped. The same key sent with different rows names a different import, so
    those rows are never mistaken for a retry and dropped. Creation identity
    belongs to this key and the row ordinal, not SKU or barcode, so same-SKU and
    no-SKU rows stay distinct lots.
    """
    canonical = json.dumps(
        {"key": idempotency_key, "upsert": upsert, "rows": rows},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return f"import:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _source_field(row: dict, key: str) -> str:
    """The row column a record field was read from."""
    if key in row:
        return key
    if key == "cost_total":
        return "cost_price_total"
    if key.endswith("_price"):
        return f"{key}_total"
    return next((k for k in _AMOUNT_SOURCE_KEYS if _to_float(row.get(k)) is not None), key)


def _permission_errors(
    record: dict, row: dict, price_lists: list[dict], *, can_set_prices: bool, can_edit_amounts: bool,
) -> list[dict]:
    """The permission rules the writer enforces on a record, as row errors."""
    data = record["data"]
    gated: list[tuple[str, str]] = []
    if not can_set_prices:
        gated += [
            (_source_field(row, key), "set_inventory_prices") for key in sorted(price_keys_in(data, price_lists))
        ]
    if record["event_type"] == "item.patched" and not can_edit_amounts:
        gated += [(_source_field(row, key), "edit_inventory_amounts") for key in sorted(AMOUNT_ITEM_KEYS & set(data))]
    return [
        {"field": field, "code": "permission_denied", "message": f"Setting {field} requires the {permission} permission"}
        for field, permission in dict(gated).items()
    ]


def _pop_cost_change(data: dict) -> dict | None:
    """Take the goods cost out of patch data as the item page's cost edit.

    Returns ``fields_changed`` for an item.updated restatement, or None when the
    row sets no cost. Raises TypeError or ValueError for a cost that is not a number.
    """
    cost_total, cost_price = data.pop("cost_total", None), data.pop("cost_price", None)
    field, value = ("cost_total", cost_total) if cost_total not in (None, "") else ("cost_price", cost_price)
    if value in (None, ""):
        return None
    return {field: {"new": float(value)}}


async def _cost_restatement_error(session: AsyncSession, company_id, record: dict, row: dict) -> dict | None:
    """The writer's cost-restatement refusal for a planned patch, found without writing.

    The writer applies the patch and then restates the cost against the patched
    item; this runs the same restatement check against the item as the patch
    would leave it.
    """
    if record["event_type"] != "item.patched" or not COST_ITEM_KEYS & set(record["data"]):
        return None
    from celerp_inventory.projections import apply_item_event

    data = dict(record["data"])
    try:
        cost_change = _pop_cost_change(data)
    except (TypeError, ValueError):
        return None  # the cell checks report a cost that is not a number
    if cost_change is None:
        return None
    entity_id = record["entity_id"]
    stored = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_id == entity_id)
    )).scalars().first()
    if stored is None:
        return None
    patched = SimpleNamespace(entity_type=stored.entity_type,
                              state=apply_item_event(stored.state or {}, "item.patched", data))
    try:
        await _restatement(session, company_id, entity_id, "item.updated",
                           {"fields_changed": cost_change}, {entity_id: patched})
    except CostRestatementConflict as exc:
        return {"field": _source_field(row, next(iter(cost_change))), "code": "cost_not_carried", "message": str(exc)}
    return None


def _semantic_fingerprint(build: ImportBuild, errors: list[dict]) -> str:
    """Hash of what the rows would write: each record's target and data, and the errors.

    Per-attempt identity (idempotency keys, generated ids of new items) is left
    out. A location the import will create is identified by its name, so the
    fingerprint is the same before and after the import creates it.
    """
    canonical = []
    for rec, row_no in zip(build.records, build.record_rows):
        data = {k: v for k, v in rec["data"].items() if k != "idempotency_key"}
        location_name = str(build.rows[row_no - 1].get("location_name") or "").strip()
        if location_name:
            data["location_id"] = {"name": location_name}
        canonical.append({
            "row": row_no,
            "event_type": rec["event_type"],
            "entity_id": rec["entity_id"] if rec["event_type"] == "item.patched" else None,
            "data": data,
        })
    return import_preview_hash({"records": canonical, "errors": errors})


async def preflight_import_rows(
    session: AsyncSession,
    company_id,
    role: str,
    settings: dict,
    rows: list[dict],
    *,
    upsert: bool,
    operation_key: str,
) -> SemanticImportPlan:
    """The one semantic check of mapped item rows, with nothing written.

    Checks each mapped cell against the item spec, builds the records, and
    applies the writer's own rules (amounts, codes, permissions) to them, so a
    row the writer would refuse is reported here with its field and code. Every
    preview and every commit runs this over the same rows, so they cannot
    disagree about a row.
    """
    price_lists, _default_list, _currency = await get_price_config(session, company_id)
    spec = build_item_import_spec(price_lists)
    errors: list[dict] = []
    for i, row in enumerate(rows):
        for col in dict.fromkeys([*spec.cols, *spec.type_map]):
            code = cell_error_code(spec, col, str(row.get(col, "") or ""))
            if code:
                errors.append({"row": i + 1, "field": col, "code": code, "message": _CELL_ERROR_MESSAGES[code].format(col=col)})
        errors.extend({"row": i + 1, **e} for e in _unsupported_item_field_errors(spec, row))
    build = await build_import_records(
        session, company_id, rows, upsert=upsert,
        create_missing_locations=role_has_permission(settings, role, "manage_company_settings"),
        create_key=operation_key,
    )
    errors.extend(build.errors)
    can_set_prices = role_has_permission(settings, role, "set_inventory_prices")
    can_edit_amounts = role_has_permission(settings, role, "edit_inventory_amounts")
    for rec, row_no in zip(build.records, build.record_rows):
        errors.extend(
            {"row": row_no, **e} for e in _permission_errors(
                rec, rows[row_no - 1], price_lists, can_set_prices=can_set_prices, can_edit_amounts=can_edit_amounts,
            )
        )
        cost_error = await _cost_restatement_error(session, company_id, rec, rows[row_no - 1])
        if cost_error:
            errors.append({"row": row_no, **cost_error})
    errors.sort(key=lambda e: e["row"])
    return SemanticImportPlan(
        rows=build.rows, records=build.records, record_rows=build.record_rows, errors=errors,
        locations_to_create=build.locations_to_create,
        semantic_fingerprint=_semantic_fingerprint(build, errors),
    )


async def preview_import_rows(
    session: AsyncSession,
    company_id,
    role: str,
    settings: dict,
    rows: list[dict],
    *,
    upsert: bool,
    idempotency_key: str | None,
) -> SemanticImportPlan:
    """Preview mapped item rows under the operation key their commit will use."""
    return await preflight_import_rows(
        session, company_id, role, settings, rows,
        upsert=upsert, operation_key=import_operation_key(idempotency_key, rows, upsert),
    )


async def _create_missing_locations(session: AsyncSession, company_id, names: list[str]) -> dict[str, str]:
    """Create the named locations the company does not have yet; return name -> id.

    Runs under the company lock and re-reads the names after taking it, so two
    imports naming the same new location create it once and both get its id.
    """
    await lock_company(session, company_id)
    ids = {
        loc.name: str(loc.id) for loc in (await session.execute(
            select(Location).where(Location.company_id == company_id, Location.name.in_(names))
        )).scalars().all()
    }
    for name in names:
        if name not in ids:
            location = Location(id=uuid.uuid4(), company_id=company_id, name=name, type="warehouse")
            session.add(location)
            ids[name] = str(location.id)
    await session.flush()
    return ids


# Records per write_import_batch call; one import still commits once.
IMPORT_CHUNK = 500


async def import_items(
    session: AsyncSession,
    company_id,
    actor_id,
    role: str,
    settings: dict,
    rows: list[dict],
    *,
    upsert: bool,
    filename: str | None,
    idempotency_key: str | None,
    plan: SemanticImportPlan | None = None,
) -> BatchImportResult:
    """Import mapped business rows through the canonical committer, in one transaction.

    Writes from exactly one semantic plan of the rows: ``plan`` when a bound
    commit has already made and checked it against its preview, otherwise the
    preflight run here. Any row error raises ImportRejected and nothing is
    written. A clean import then creates any missing named locations and fills
    their ids into the planned records, writes the records in chunks of IMPORT_CHUNK into
    one Import History entry named by the operation key, merges newly discovered
    attribute columns into the company's category schemas (gated on
    manage_company_settings), and commits once. Every item import transport ends
    here.

    The company lock is taken before the plan is made (the bound transports take
    it through locked_authority before they re-preview) and held until that
    one commit, so the state the plan read cannot change before it is written,
    and a failure anywhere leaves nothing written for a retry to duplicate.

    Creates use ``import-attempt + row ordinal`` identity, so equal SKUs and rows
    without SKUs remain distinct lots while an exact retry of the same attempt is a
    no-op. Upserts resolve a concrete existing entity first and use a hash of the
    resulting patch, so the same patch dedupes but a later changed patch still applies.
    """
    await locked_company(session, company_id)
    batch_key = import_operation_key(idempotency_key, rows, upsert)
    if plan is None:
        plan = await preflight_import_rows(
            session, company_id, role, settings, rows, upsert=upsert, operation_key=batch_key,
        )
    if plan.errors:
        raise ImportRejected(plan.errors)

    if plan.locations_to_create:
        location_ids = await _create_missing_locations(session, company_id, plan.locations_to_create)
        for rec, row_no in zip(plan.records, plan.record_rows):
            name = str(plan.rows[row_no - 1].get("location_name") or "").strip()
            if name in location_ids:
                rec["data"]["location_id"] = location_ids[name]

    for rec in plan.records:
        if rec["event_type"] == "item.created":
            key = f"{batch_key}:{rec['idempotency_key']}"
            rec["idempotency_key"] = rec["data"]["idempotency_key"] = key
            rec["entity_id"] = import_created_item_id(company_id, key)

    user = SimpleNamespace(id=actor_id)
    outcome = ImportOutcome()
    batch_id: str | None = None
    for i in range(0, len(plan.records), IMPORT_CHUNK):
        body = BatchImportRequest(
            records=[ImportRecord(**r) for r in plan.records[i : i + IMPORT_CHUNK]],
            filename=filename,
            upsert=upsert,
        )
        chunk_outcome, chunk_batch_id = await write_import_batch(
            session, company_id, user, role, settings, body, operation_key=batch_key,
        )
        outcome.records.extend(chunk_outcome.records)
        batch_id = chunk_batch_id or batch_id
    await recognize_opening_lots(
        session, company_id, [r.entity_id for r in outcome.records if r.status == "created"], actor_id, batch_id)

    # Mutating category schemas is a settings change, so the caller's role must
    # carry manage_company_settings; without it the merge is skipped.
    if plan.records and role_has_permission(settings, role, "manage_company_settings"):
        inferred = _infer_category_schemas(_collect_category_attributes(plan.rows))
        if inferred:
            await _merge_category_schemas(session, company_id, inferred)
    await session.commit()

    return BatchImportResult(**outcome.route_counts(cap_rejections=False), batch_id=batch_id)


async def _merge_category_schemas(session: AsyncSession, company_id, incoming: dict[str, list[dict]]) -> None:
    """Append newly discovered attribute keys to the company's category schemas.

    Never overwrites an existing key (user customizations are preserved). This is
    the sole path that grows category schemas from imported attribute columns; it
    stages the change on the company row and import_items commits it with the items.
    """
    company = await locked_company(session, company_id)
    if company is None:
        return
    settings = dict(company.settings)
    cat_schemas: dict[str, list[dict]] = dict(settings.get("category_schemas") or {})
    added = False
    for cat, new_fields in incoming.items():
        existing = cat_schemas.get(cat) or []
        existing_keys = {f["key"] for f in existing}
        max_pos = max((f.get("position", 0) for f in existing), default=-1)
        appended = []
        for nf in new_fields:
            if nf["key"] not in existing_keys:
                max_pos += 1
                appended.append({**nf, "position": max_pos, "editable": True, "required": False, "visible_to_roles": [], "show_in_table": True})
                existing_keys.add(nf["key"])
        if appended:
            cat_schemas[cat] = existing + appended
            added = True
    if added:
        settings["category_schemas"] = cat_schemas
        company.settings = settings


async def adjust_item_quantity(
    session: AsyncSession,
    company_id,
    actor_id,
    entity_id: str,
    data: dict,
    *,
    source: str,
    idempotency_key: str,
):
    """Set an item's quantity on hand, checked against its selling unit's decimals. The caller commits."""
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row:
        current_sell_by = row.state.get("sell_by")
        unit_map = {u["name"]: u for u in await get_company_units(session, company_id)}
        if current_sell_by and current_sell_by in unit_map:
            validate_quantity(data["new_qty"], unit_map[current_sell_by]["decimals"])
    return await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.quantity.adjusted",
        data=data,
        actor_id=actor_id,
        location_id=None,
        source=source,
        idempotency_key=idempotency_key,
        metadata_={},
    )


async def write_import_batch(
    session: AsyncSession,
    company_id,
    user,
    role: str,
    settings: dict,
    body: BatchImportRequest,
    *,
    operation_key: str | None = None,
) -> tuple[ImportOutcome, str | None]:
    """Write item import records through one bounded, company-scoped writer.

    Reports one outcome per record and the import batch id, and leaves the
    commit to the caller: `import_items` and `commit_import_batch` for the
    HTTP and agent transports, the migration runner for the migration sink.

    Exact retries resolve through the ledger before any allocation or uniqueness
    check. With ``body.upsert`` a record whose key created an item updates that
    item, and only when it names the same entity id. Semantic upserts arrive as
    ``item.patched`` records whose idempotency key is already target+content aware.

    Created items are recorded in Import History in the same transaction as
    their writes. With ``operation_key`` (one logical import written in several
    chunks) every chunk adds to the company's active entry for that key, and the
    entry's id is returned even when this chunk created nothing. Without it each
    call that creates items records its own entry.
    """
    from celerp_inventory.models_import_batch import ImportBatch
    from celerp.models.ledger import LedgerEntry

    keys = list(dict.fromkeys(r.idempotency_key for r in body.records))
    existing_rows = []
    if keys:
        existing_rows = (await session.execute(
            select(LedgerEntry).where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.idempotency_key.in_(keys),
            )
        )).scalars().all()
    existing: dict[str, LedgerEntry] = {row.idempotency_key: row for row in existing_rows}

    units = await get_company_units(session, company_id)
    valid_units: frozenset[str] = frozenset(u["name"] for u in units)
    price_lists = (await get_price_config(session, company_id))[0]
    derived_keys = derived_price_keys(price_lists)

    outcome = ImportOutcome()
    created_entity_ids: list[str] = []
    created_keys: list[str] = []

    # One bounded import transaction can touch many item rows and later allocate
    # or preserve a physical code. Take the company namespace before the first
    # projection write so every item lock in this chunk follows Company -> Projection.
    # This also prevents two imports from locking the same item set in opposite orders.
    if body.records:
        await lock_item_code_namespace(session, company_id)

    for rec in body.records:
        data = dict(rec.data)
        status = str(data.pop("status", None) or "").strip().lower()
        data.pop("created_at", None)
        data.pop("updated_at", None)
        data.pop("idempotency_key", None)
        for key in derived_keys:
            data.pop(key, None)
        if "allow_splitting" in data and not isinstance(data["allow_splitting"], bool):
            data["allow_splitting"] = str(data["allow_splitting"]).strip().lower() in (
                "true", "yes", "1", "y", "t",
            )

        event_type = rec.event_type
        entity_id = rec.entity_id
        idem_key = rec.idempotency_key
        primary = existing.get(idem_key)

        try:
            reject_system_item_fields(data)
        except HTTPException as exc:
            outcome.add(entity_id, "rejected", f"Row (SKU={data.get('sku', '?')}): {exc.detail}")
            continue

        if event_type == "item.patched":
            if primary is not None:
                if primary.event_type == "item.patched" and primary.entity_id == entity_id:
                    outcome.add(entity_id, "skipped")
                else:
                    outcome.add(entity_id, "rejected", f"{entity_id}: idempotency key was already used for another operation")
                continue
        elif event_type == "item.created":
            if primary is not None:
                if primary.event_type != "item.created" or primary.entity_id != entity_id:
                    outcome.add(entity_id, "rejected", f"{entity_id}: idempotency key was already used for another operation")
                    continue
                if not body.upsert:
                    outcome.add(primary.entity_id, "skipped")
                    continue
                current = await session.get(
                    Projection, {"company_id": company_id, "entity_id": entity_id}
                )
                if current is not None and all((current.state or {}).get(k) == v for k, v in data.items()):
                    # An exact retry: the item already holds this content.
                    outcome.add(entity_id, "skipped")
                    continue
                event_type = "item.patched"
                canonical_patch = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
                idem_key = (
                    f"{rec.idempotency_key}:upsert:"
                    f"{hashlib.sha256(canonical_patch.encode()).hexdigest()}"
                )
                replay = await find_event_by_idempotency(session, company_id, idem_key)
                if replay is not None:
                    if replay.event_type == "item.patched" and replay.entity_id == entity_id:
                        outcome.add(entity_id, "skipped")
                    else:
                        outcome.add(entity_id, "rejected", f"{entity_id}: idempotency key was already used for another operation")
                    continue
        elif event_type == "item.snapshot":
            if primary is not None:
                if primary.event_type == "item.snapshot" and primary.entity_id == entity_id:
                    outcome.add(entity_id, "skipped")
                else:
                    outcome.add(entity_id, "rejected", f"{entity_id}: idempotency key was already used for another operation")
                continue
        else:
            outcome.add(entity_id, "rejected", f"{entity_id}: event type {event_type!r} is not import-safe")
            continue

        # A status changes through the status action, never through an upsert patch.
        if status and event_type != "item.patched":
            if status not in IMPORT_ITEM_STATUSES:
                outcome.add(entity_id, "rejected",
                    f"Row (SKU={data.get('sku', '?')}): an imported item cannot start as {status}; "
                    f"use {', '.join(IMPORT_ITEM_STATUSES[:-1])} or {IMPORT_ITEM_STATUSES[-1]}"
                )
                continue
            data["status"] = status

        stored_proj: Projection | None = None
        if event_type == "item.patched":
            stored_proj = await session.get(
                Projection, {"company_id": company_id, "entity_id": entity_id}
            )
            if stored_proj is None or stored_proj.entity_type != "item":
                outcome.add(entity_id, "rejected", f"{entity_id}: upsert target was not found")
                continue
        else:
            existing_projection = await session.get(
                Projection, {"company_id": company_id, "entity_id": entity_id}
            )
            if existing_projection is not None:
                outcome.add(entity_id, "rejected", f"{entity_id}: entity already exists")
                continue

        # Imported price values modify the same protected business data as the
        # interactive pricing surfaces. Import/export authority does not imply
        # permission to set prices.
        price_keys = price_keys_in(data, price_lists)
        if price_keys and not role_has_permission(settings, role, "set_inventory_prices"):
            outcome.add(entity_id, "rejected",
                f"Row (SKU={data.get('sku', '?')}): editing {sorted(price_keys)} "
                "requires the set_inventory_prices permission"
            )
            continue

        sell_by = str(data.get("sell_by") or "").strip()
        if event_type != "item.patched" and not sell_by:
            outcome.add(entity_id, "rejected", f"Row (SKU={data.get('sku', '?')}): sell_by is required")
            continue
        if sell_by and valid_units and sell_by not in valid_units:
            outcome.add(entity_id, "rejected",
                f"Row (SKU={data.get('sku', '?')}): sell_by '{sell_by}' is not a valid unit"
            )
            continue

        if event_type == "item.patched" and stored_proj is not None:
            if not role_has_permission(settings, role, "edit_inventory_amounts"):
                gated = set(AMOUNT_ITEM_KEYS & set(data))
                stored_sell_by = str((stored_proj.state or {}).get("sell_by") or "").strip()
                if sell_by and sell_by != stored_sell_by:
                    gated.add("sell_by")
                if gated:
                    outcome.add(entity_id, "rejected",
                        f"Row (SKU={data.get('sku', '?')}): editing {sorted(gated)} "
                        "requires the edit_inventory_amounts permission"
                    )
                    continue

        negative_amount = None
        for key in AMOUNT_ITEM_KEYS & set(data):
            value = data.get(key)
            if value in (None, ""):
                continue
            try:
                if float(value) < 0:
                    negative_amount = key
                    break
            except (TypeError, ValueError):
                pass
        if negative_amount is not None:
            outcome.add(entity_id, "rejected",
                f"Row (SKU={data.get('sku', '?')}): {negative_amount} cannot be negative"
            )
            continue

        # Creation follows the ordinary internal-code primitive, after replay
        # detection, so a retry cannot consume a new SKU/barcode.
        if event_type == "item.created":
            if not str(data.get("sku") or "").strip():
                data["sku"] = (await allocate_internal_codes(session, company_id))[0]
            sku = str(data.get("sku") or "")
            if not data.get("barcode") and sku.isdigit():
                if await code_in_use(session, company_id, sku):
                    data["barcode"] = (await allocate_internal_codes(session, company_id))[0]
                else:
                    data["barcode"] = sku

        # Explicit codes are format-checked but recorded as the source file holds them,
        # even when another item already carries the same code: the resolver reports the
        # ambiguity at scan time and Doctor lists it, so an import never drops rows.
        try:
            validate_sku(data.get("sku"))
            validate_barcode(data.get("barcode"))
            validate_rfid_epc(data.get("rfid_epc"))
        except ValueError as exc:
            outcome.add(entity_id, "rejected", f"Row (SKU={data.get('sku', '?')}): {exc}")
            continue

        if event_type != "item.patched":
            data["idempotency_key"] = idem_key

        loc_id: uuid.UUID | None = None
        raw_loc = data.get("location_id")
        if raw_loc:
            try:
                loc_id = uuid.UUID(str(raw_loc))
            except ValueError:
                outcome.add(entity_id, "rejected", f"Row (SKU={data.get('sku', '?')}): invalid location_id")
                continue

        # A patched goods cost is restated like an edit on the item page (merge and
        # COGS consequences included); the row applies whole or not at all.
        # The cost is applied after the patch, as the same item.updated cost edit the item
        # page makes, so it normalizes against the row's resulting quantity.
        cost_change = None
        if event_type == "item.patched" and stored_proj is not None and COST_ITEM_KEYS & set(data):
            try:
                cost_change = _pop_cost_change(data)
            except (TypeError, ValueError):
                outcome.add(entity_id, "rejected", f"Row (SKU={data.get('sku', '?')}): cost must be a number")
                continue

        try:
            async with session.begin_nested():
                entry = await emit_event(
                    session,
                    preserve_external_code_conflicts=True,
                    company_id=company_id,
                    entity_id=entity_id,
                    entity_type="item",
                    event_type=event_type,
                    data=data,
                    actor_id=user.id,
                    location_id=loc_id,
                    source=rec.source,
                    idempotency_key=idem_key,
                    metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
                )
                if cost_change is not None and not getattr(entry, "was_deduped", False):
                    await restate_item_cost(
                        session, company_id, entity_id,
                        event_type="item.updated", data={"fields_changed": cost_change},
                        actor_id=user.id, source=rec.source, idempotency_key=f"{idem_key}:cost",
                    )
        except CostRestatementConflict as exc:
            outcome.add(entity_id, "rejected", f"Row (SKU={data.get('sku', '?')}): {exc}")
            continue
        except Exception:
            # The cause stays in the server log; the caller gets a plain row error.
            logger.exception("Item import could not write %s", entity_id)
            outcome.add(entity_id, "failed", f"Row (SKU={data.get('sku', '?')}): the item could not be written")
            continue

        existing[idem_key] = entry
        if getattr(entry, "was_deduped", False):
            outcome.add(entity_id, "skipped")
            continue

        if event_type == "item.patched":
            outcome.add(entity_id, "updated")
        else:
            created_entity_ids.append(entity_id)
            created_keys.append(idem_key)
            outcome.add(entity_id, "created")

    created = len(created_entity_ids)
    # The company lock taken above serialises the lookup and the insert, so two
    # chunks of one operation never race to create its entry.
    batch = None
    if operation_key is not None and body.records:
        batch = (await session.execute(
            select(ImportBatch).where(
                ImportBatch.company_id == company_id, ImportBatch.operation_key == operation_key,
            )
        )).scalar_one_or_none()
    if created > 0:
        if batch is None:
            batch = ImportBatch(
                id=uuid.uuid4(),
                company_id=company_id,
                entity_type="item",
                filename=body.filename,
                row_count=0,
                entity_ids=[],
                idempotency_keys=[],
                status="active",
                operation_key=operation_key,
            )
            session.add(batch)
        batch.row_count += created
        batch.entity_ids = [*batch.entity_ids, *created_entity_ids]
        batch.idempotency_keys = [*batch.idempotency_keys, *created_keys]

        # The first real import clears the demo items the user never edited or used.
        await delete_untouched_demo_items(session, company_id)

    return outcome, (str(batch.id) if batch is not None else None)


async def commit_import_batch(
    session: AsyncSession,
    company_id,
    user,
    role: str,
    settings: dict,
    body: BatchImportRequest,
    *,
    operation_key: str | None = None,
) -> BatchImportResult:
    """Write an item import batch and commit it; the route and agent transports call this.
    The stock its local creates bring in is booked as opening stock; a snapshot of another
    system's item, or a migrated one, keeps the books it came with."""
    outcome, batch_id = await write_import_batch(
        session, company_id, user, role, settings, body, operation_key=operation_key,
    )
    local = {r.entity_id for r in body.records if r.event_type == "item.created" and r.source != "migration"}
    await recognize_opening_lots(
        session, company_id, [r.entity_id for r in outcome.records if r.status == "created" and r.entity_id in local],
        user.id, batch_id)
    await session.commit()
    return BatchImportResult(**outcome.route_counts(cap_rejections=False), batch_id=batch_id)
