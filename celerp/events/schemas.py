# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# -----------------
# Items
# -----------------


class ItemCreated(BaseModel):
    sku: str
    name: str
    quantity: float = 0
    category: str | None = None
    location_id: str | None = None
    sell_by: str | None = None
    allow_splitting: bool = True
    weight: float | None = None
    weight_unit: str | None = None
    inventory_type: str = "stocked"
    attributes: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "allow"}


class ItemSnapshot(BaseModel):
    """Snapshot of full item state - used for imports. sku/name optional (absent in some sources)."""

    sku: str | None = None
    name: str | None = None
    quantity: float = 0
    category: str | None = None
    location_id: str | None = None
    sell_by: str | None = None
    allow_splitting: bool = True
    weight: float | None = None
    weight_unit: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    entity_id: str | None = None
    status: str | None = None
    reserved_quantity: float | None = None
    created_at: str | None = None   # ISO date or datetime from source system
    updated_at: str | None = None   # ISO datetime from source system

    model_config = {"extra": "allow"}  # CIF adapters may pass source-specific fields


class ItemUpdated(BaseModel):
    fields_changed: dict[str, dict[str, Any]]


class ItemPricingSet(BaseModel):
    price_type: str
    new_price: float


class ItemStatusSet(BaseModel):
    new_status: str


class ItemTransferred(BaseModel):
    to_location_id: str


class ItemQuantityAdjusted(BaseModel):
    new_qty: float
    # Optional provenance for audit-driven adjustments (Q6): why and from which audit list, plus the
    # pre-adjustment quantity so the audit's "Adjust stock" action can be undone.
    reason: str | None = None
    source_list_id: str | None = None
    prior_qty: float | None = None


class ItemLandedCostApplied(BaseModel):
    # Absolute per-unit landed cost for one (source bill, kind); overwrite-safe so re-running the
    # allocation with changed freight self-corrects. unit_amount=0 clears the contribution.
    source_bill_id: str
    kind: str                       # freight | insurance | duty | import_vat
    unit_amount: float
    currency_rate: float | None = None   # bill conversion rate used (reproducibility)


class ItemFulfilled(BaseModel):
    source_doc_id: str
    quantity_fulfilled: float
    fulfilled_by: str


class ItemFulfillmentReversed(BaseModel):
    source_doc_id: str
    quantity_restored: float
    reversed_by: str
    reason: str


class ItemExpired(BaseModel):
    reason: str | None = None


class ItemSplit(BaseModel):
    # Aggregate marker on the MOTHER. The projection consumes child_ids/child_skus;
    # children_detail drives the per-child history rows (one row per child).
    child_ids: list[str]
    child_skus: list[str] = Field(default_factory=list)
    quantities: list[float]
    parent_sku: str | None = None
    # One entry per child: {child_id, child_sku, origin_event_id, qty_before, qty_after,
    # pieces_before, pieces_after, weight_before, weight_after}. Sequential mother deltas.
    children_detail: list[dict[str, Any]] = Field(default_factory=list)


class ItemSplitFrom(BaseModel):
    """Origin marker on the CHILD created by a split. The child's "first entry"."""
    parent_id: str
    parent_sku: str
    qty: float
    pieces: float | None = None
    weight: float | None = None


class ItemTransform(BaseModel):
    child_id: str
    child_sku: str
    child_category: str
    parent_cost_total: float
    child_cost_total: float
    parent_sku: str | None = None
    child_origin_event_id: int | None = None
    qty_before: float | None = None
    qty_after: float | None = None
    pieces_before: float | None = None
    pieces_after: float | None = None
    weight_before: float | None = None
    weight_after: float | None = None


class ItemTransformedFrom(BaseModel):
    """Origin marker on the CHILD created by a transform. The child's "first entry"."""
    parent_id: str
    parent_sku: str
    qty: float
    category: str
    pieces: float | None = None
    weight: float | None = None


class ItemMerged(BaseModel):
    """Marker event on the NEW item created by a merge. Real state comes from item.created."""
    source_entity_ids: list[str]


class ItemPatched(BaseModel):
    """CSV upsert patch: accepts any item data fields."""
    model_config = {"extra": "allow"}


class ItemSourceDeactivated(BaseModel):
    merged_into: str


class ItemConsumed(BaseModel):
    quantity_consumed: float


class ItemProduced(BaseModel):
    quantity_produced: float


# --- Manufacturing recipe (materials + labor + overhead) attached to an item ---
# The recipe is the single source of truth for how a manufactured item is built.
# Interpretation (cost roll-up, expansion) lives in celerp-manufacturing; the schema
# is shared here and the projection stores it verbatim (full-replace).

class ComponentSpec(BaseModel):
    item_id: str                       # entity_id of the input inventory item
    sku: str | None = None             # denormalized for display; resolved server-side
    quantity: float                    # qty of this component per recipe batch (output_qty)
    unit: str | None = None


class LaborLine(BaseModel):
    operation: str
    kind: str = "hourly"               # "hourly" (hours × rate) | "fixed" (flat amount)
    hours: float = 0
    rate: float = 0                    # per-line rate (no company default)
    amount: float = 0                  # flat cost, used when kind == "fixed"
    source: str = "manual"             # "manual" | "auto:<rule>" (future auto-labor module)


class OverheadLine(BaseModel):
    description: str
    amount: float = 0


class RecipeSpec(BaseModel):
    output_qty: float = 1              # units one batch of this recipe yields
    components: list[ComponentSpec] = Field(default_factory=list)
    labor: list[LaborLine] = Field(default_factory=list)
    overhead: list[OverheadLine] = Field(default_factory=list)
    # Derived, written server-side by the cost roll-up — never trusted from the client:
    unit_cost: float | None = None
    materials_cost: float | None = None
    labor_cost: float | None = None
    overhead_cost: float | None = None


class ItemRecipeSet(BaseModel):
    recipe: RecipeSpec


# --- Production workflow (ordered build steps) attached to an item ---
# Independent of the costing recipe: this is the shop-floor instruction sequence,
# stored verbatim by the inventory projection (full-replace) and rendered on the
# Manufacturing tab + printable worksheet. Time is canonical in MINUTES; time_unit
# is a cosmetic display hint only, so all logic reads a single field.

_WORKFLOW_TIME_UNITS = ("min", "hr", "day")
_WORKFLOW_UNIT_MINUTES = {"min": 1.0, "hr": 60.0, "day": 1440.0}


def workflow_step_minutes(time_value, time_unit: str) -> float:
    """Canonical elapsed minutes for a workflow step — the single conversion point.

    time_value is the number as the user typed it, in time_unit; the result is the
    one value every calculation (active-time totals, scheduling) reads.
    """
    try:
        value = float(time_value or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value * _WORKFLOW_UNIT_MINUTES.get(time_unit, 1.0)


class WorkflowStep(BaseModel):
    id: str = ""                       # stable uuid; the server assigns one if blank
    station: str = ""                  # work center / location (links to a WorkCenter name)
    instructions: str = ""             # free-text detail for the worker (multi-line)
    time_value: float = 0              # elapsed time as typed, in time_unit
    time_unit: str = "min"             # min | hr | day
    ref_file_id: str | None = None     # optional reference image/doc (an item file id)
    # Derived, written server-side from (time_value, time_unit) — never trusted from the client.
    # CANONICAL: every calculation reads time_minutes, never the typed value/unit.
    time_minutes: float | None = None


class WorkflowSpec(BaseModel):
    steps: list[WorkflowStep] = Field(default_factory=list)


class ItemWorkflowSet(BaseModel):
    workflow: WorkflowSpec


class ItemReserved(BaseModel):
    quantity: float
    reserved_for: str | None = None  # source doc entity_id


class ItemUnreserved(BaseModel):
    quantity: float
    released_from: str | None = None  # source doc entity_id


# -----------------
# CRM
# -----------------


class CrmContactCreated(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}  # CIF adapters may pass address and other fields


class CrmContactUpdated(BaseModel):
    fields_changed: dict[str, dict[str, Any]]


class CrmContactMerged(BaseModel):
    source_contact_ids: list[str]
    merged_people: list[dict] = Field(default_factory=list)
    merged_addresses: list[dict] = Field(default_factory=list)
    merged_tags: list[str] = Field(default_factory=list)


class CrmContactTagged(BaseModel):
    tags: list[str]


class CrmDealCreated(BaseModel):
    name: str
    stage: str | None = None
    value: float | None = None
    contact_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class CrmDealStageChanged(BaseModel):
    new_stage: str


class CrmDealWon(BaseModel):
    notes: str | None = None


class CrmDealLost(BaseModel):
    reason: str | None = None


class CrmDealUpdated(BaseModel):
    fields_changed: dict[str, Any] = Field(default_factory=dict)


class CrmDealDeleted(BaseModel):
    model_config = {"extra": "allow"}


class CrmDealReopened(BaseModel):
    model_config = {"extra": "allow"}


class CrmContactNoteAdded(BaseModel):
    contact_id: str
    note_id: str
    note: str
    author_id: str | None = None
    author_name: str | None = None
    created_at: str | None = None


class CrmContactNoteUpdated(BaseModel):
    contact_id: str
    note_id: str
    note: str
    updated_at: str | None = None


class CrmContactNoteRemoved(BaseModel):
    contact_id: str
    note_id: str


class CrmContactPersonAdded(BaseModel):
    person_id: str
    name: str
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    is_primary: bool = False


class CrmContactPersonUpdated(BaseModel):
    person_id: str
    name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    is_primary: bool | None = None


class CrmContactPersonRemoved(BaseModel):
    person_id: str


class CrmContactAddressAdded(BaseModel):
    address_id: str
    address_type: str = "billing"
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    is_default: bool = False


class CrmContactAddressUpdated(BaseModel):
    address_id: str
    address_type: str | None = None
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    is_default: bool | None = None


class CrmContactAddressRemoved(BaseModel):
    address_id: str


# -----------------
# Documents
# -----------------


class DocCreated(BaseModel):
    title: str | None = None
    doc_type: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}  # CIF adapters pass invoice fields directly


class DocUpdated(BaseModel):
    fields_changed: dict[str, dict[str, Any]]


class DocRenumbered(BaseModel):
    fields_changed: dict[str, dict[str, Any]]


class DocPatched(BaseModel):
    """CSV upsert patch: accepts any doc data fields."""
    model_config = {"extra": "allow"}


class DocLinked(BaseModel):
    entity_id: str
    entity_type: str


class DocFinalized(BaseModel):
    notes: str | None = None


class DocVoided(BaseModel):
    reason: str | None = None
    pre_void_status: str | None = None
    pre_void_fulfillment: str | None = None


class DocRevertedToDraft(BaseModel):
    reverted_by: str
    previous_status: str
    reason: str | None = None
    doc_type: str | None = None  # for PO->bill revert: restored doc_type
    ref_id: str | None = None    # for PO->bill revert: restored ref_id


class DocUnvoided(BaseModel):
    unvoided_by: str
    restored_status: str


class DocSent(BaseModel):
    sent_via: str | None = None
    sent_to: str | None = None


class DocPaymentReceived(BaseModel):
    amount: float
    currency: str | None = None
    method: str | None = None
    reference: str | None = None
    remaining_balance: float | None = None
    payment_date: str
    bank_account: str | None = None
    conversion_rate: float | None = None  # pass-through for premium multicurrency module
    source_doc_id: str | None = None
    target_doc_id: str | None = None


class DocPaymentRefunded(BaseModel):
    amount: float
    reason: str | None = None
    method: str | None = None


class DocPaymentVoided(BaseModel):
    payment_index: int
    void_reason: str | None = None
    refund_date: str | None = None  # ISO date for the reversal JE; defaults to today if absent


class DocPaymentDeleted(BaseModel):
    payment_index: int
    delete_reason: str | None = None


class DocConverted(BaseModel):
    target_doc_id: str
    target_doc_type: str


class DocConvertedToBill(BaseModel):
    ref_id: str | None = None
    source_po_ref: str | None = None
    doc_type: str | None = None


class DocReceived(BaseModel):
    received_items: list[dict[str, Any]] = Field(default_factory=list)
    location_id: str
    received_by: str | None = None
    notes: str | None = None


class DocSharedImport(BaseModel):
    """Inbound document received via p2p share link or .celerp bundle upload."""
    source_share_token: str | None = None
    source_origin: str | None = None

    model_config = {"extra": "allow"}  # carries full sender doc state


class DocNoteAdded(BaseModel):
    doc_id: str
    note_id: str
    note: str
    author_id: str
    author_name: str
    created_at: str


class DocNoteUpdated(BaseModel):
    doc_id: str
    note_id: str
    note: str
    updated_at: str


class DocNoteRemoved(BaseModel):
    doc_id: str
    note_id: str


class ListNoteAdded(BaseModel):
    list_id: str
    note_id: str
    note: str
    author_id: str
    author_name: str
    created_at: str


class ListNoteUpdated(BaseModel):
    list_id: str
    note_id: str
    note: str
    updated_at: str


class ListNoteRemoved(BaseModel):
    list_id: str
    note_id: str


class DocFulfilled(BaseModel):
    fulfilled_items: list[dict[str, Any]]
    fulfilled_by: str
    fulfilled_at: str = ""
    strategy: str
    total_cogs: float


class DocReturnReceived(BaseModel):
    items: list[dict[str, Any]]
    received_by: str
    received_at: str = ""
    notes: str | None = None


class DocReturnUndone(BaseModel):
    undone_by: str
    undone_at: str = ""
    item_ids: list[str] = []
    notes: str | None = None


class DocReceiveUndone(BaseModel):
    undone_by: str
    undone_at: str = ""
    item_ids: list[str] = []



class DocPartiallyFulfilled(BaseModel):
    fulfilled_items: list[dict[str, Any]]
    unfulfilled_items: list[dict[str, Any]]
    fulfilled_by: str
    fulfilled_at: str = ""
    strategy: str


class DocFulfillmentReversed(BaseModel):
    reversed_items: list[dict[str, Any]]
    reversed_by: str
    reason: str


# -----------------
# Manufacturing
# -----------------


class MfgOrderCreated(BaseModel):
    product_sku: str | None = None
    quantity: float | None = None
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class MfgOrderStarted(BaseModel):
    started_by: str | None = None


class MfgOrderCompleted(BaseModel):
    completed_by: str | None = None


class MfgOrderCancelled(BaseModel):
    reason: str | None = None


class MfgOrderOnHold(BaseModel):
    reason: str | None = None


class MfgOrderResumed(BaseModel):
    resumed_by: str | None = None


class MfgOrderIssued(BaseModel):
    # Components issued from stock into a run (decrements the components). Partial issues allowed.
    items: list[dict[str, Any]] = Field(default_factory=list)
    issued_by: str | None = None


class MfgOrderReceived(BaseModel):
    # Finished goods received from a run. quantity restocks the output (per allow_splitting);
    # lot_item_id is set when a non-splittable output created a new lot under the product.
    quantity: float
    lot_item_id: str | None = None
    received_by: str | None = None


class MfgOrderScheduled(BaseModel):
    # Scheduling fields for a run (Phase A). All optional; only provided keys are applied.
    due_date: str | None = None
    planned_start: str | None = None
    priority: str | None = None


# The standalone BOM entity was retired (recipes live on the inventory item). Its bom.* event
# schemas are gone too: nothing emits them, and historical bom.* events replay through the
# projection engine's default merge handler, which does not validate against EVENT_SCHEMA_MAP.

# -----------------
# Scanning
# -----------------


class ScanBarcode(BaseModel):
    code: str
    location_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ScanRfid(ScanBarcode):
    pass


class ScanNfc(ScanBarcode):
    pass


class ScanResolved(BaseModel):
    code: str
    entity_id: str
    entity_type: str


# -----------------
# Marketplace
# -----------------


class MpListingCreated(BaseModel):
    marketplace: str | None = None
    sku: str | None = None
    price: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class MpListingUpdated(BaseModel):
    fields_changed: dict[str, dict[str, Any]]


class MpListingPublished(BaseModel):
    published_at: str | None = None


class MpListingUnpublished(BaseModel):
    reason: str | None = None


class MpOrderReceived(BaseModel):
    order_ref: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class MpOrderFulfilled(BaseModel):
    fulfillment_ref: str | None = None


class MpOrderCancelled(BaseModel):
    reason: str | None = None


# -----------------
# Accounting
# -----------------


class AccJournalEntryCreated(BaseModel):
    memo: str | None = None
    lines: list[dict[str, Any]] = Field(default_factory=list)


class AccJournalEntryPosted(BaseModel):
    posted_at: str | None = None


class AccJournalEntryVoided(BaseModel):
    reason: str | None = None


class AccPeriodClosed(BaseModel):
    period: str


class AccPeriodReopened(BaseModel):
    period: str


# -----------------
# System
# -----------------


class SysCompanyCreated(BaseModel):
    name: str
    slug: str


class SysUserCreated(BaseModel):
    email: str
    role: str | None = None


class SysUserDeactivated(BaseModel):
    reason: str | None = None


class SysApiKeyCreated(BaseModel):
    api_key_id: str | None = None


class SysApiKeyRevoked(BaseModel):
    api_key_id: str | None = None


class SysBackupCreated(BaseModel):
    backup_id: str | None = None


class SysMigrationApplied(BaseModel):
    revision: str


# Subscription schemas - open/permissive since subscription data is user-defined
class SubCreated(BaseModel):
    model_config = {"extra": "allow"}
    name: str
    doc_type: str
    frequency: str
    start_date: str


class SubUpdated(BaseModel):
    model_config = {"extra": "allow"}
    fields_changed: dict = {}


class SubPaused(BaseModel):
    model_config = {"extra": "allow"}


class SubCancelled(BaseModel):
    model_config = {"extra": "allow"}


class SubResumed(BaseModel):
    model_config = {"extra": "allow"}
    next_run: str | None = None


class SubGenerated(BaseModel):
    model_config = {"extra": "allow"}
    doc_id: str
    generated_at: str
    next_run: str | None = None


class SubExpired(BaseModel):
    model_config = {"extra": "allow"}


# -----------------
# Lists
# -----------------


class ListCreated(BaseModel):
    model_config = {"extra": "allow"}
    list_type: str | None = None
    ref_id: str | None = None
    customer_id: str | None = None
    status: str = "draft"


class ListUpdated(BaseModel):
    fields_changed: dict[str, dict[str, Any]]


class ListPatched(BaseModel):
    """CSV upsert patch: accepts any list data fields."""
    model_config = {"extra": "allow"}


class ListFinalized(BaseModel):
    """draft -> finalized. Carries status, finalize milestone(s), and (audit) the frozen snapshot."""
    model_config = {"extra": "allow"}


class ListReverted(BaseModel):
    """finalized -> draft (go back before a terminal action)."""
    model_config = {"extra": "allow"}


class ListClosed(BaseModel):
    """finalized -> closed. `result` is the terminal outcome; terminal-specific fields ride along."""
    result: str
    model_config = {"extra": "allow"}


class ListReopened(BaseModel):
    """closed -> finalized (undo a terminal action)."""
    model_config = {"extra": "allow"}


class ListVoided(BaseModel):
    reason: str | None = None


# ── Entity File schemas (shared by contacts, docs) ───────────────────────────


class EntityFileAttached(BaseModel):
    entity_id: str
    entity_type: str  # "contact", "doc", "item"
    file_id: str
    filename: str
    mime: str
    size: int
    document_tag: str | None = None
    description: str | None = None


class EntityFileTagged(BaseModel):
    entity_id: str
    entity_type: str
    file_id: str
    document_tag: str


class EntityFileDescriptionUpdated(BaseModel):
    entity_id: str
    entity_type: str
    file_id: str
    description: str


class EntityFileDeleted(BaseModel):
    entity_id: str
    entity_type: str
    file_id: str


class EntityFileHeroSet(BaseModel):
    entity_id: str
    entity_type: str  # always "item"
    file_id: str  # the file to mark as hero (must be an image MIME)


EVENT_SCHEMA_MAP: dict[str, type[BaseModel]] = {
    # Items
    "item.created": ItemCreated,
    "item.snapshot": ItemSnapshot,
    "item.updated": ItemUpdated,
    "item.pricing.set": ItemPricingSet,
    "item.status.set": ItemStatusSet,
    "item.transferred": ItemTransferred,
    "item.quantity.adjusted": ItemQuantityAdjusted,
    "item.landed_cost.applied": ItemLandedCostApplied,
    "item.fulfilled": ItemFulfilled,
    "item.fulfillment_reversed": ItemFulfillmentReversed,
    "item.expired": ItemExpired,
    "item.split": ItemSplit,
    "item.split_from": ItemSplitFrom,
    "item.transform": ItemTransform,
    "item.transformed_from": ItemTransformedFrom,
    "item.merged": ItemMerged,
    "item.source_deactivated": ItemSourceDeactivated,
    "item.patched": ItemPatched,
    "item.consumed": ItemConsumed,
    "item.produced": ItemProduced,
    "item.recipe.set": ItemRecipeSet,
    "item.workflow.set": ItemWorkflowSet,
    "item.reserved": ItemReserved,
    "item.unreserved": ItemUnreserved,
    "item.file.attached": EntityFileAttached,
    "item.file.tagged": EntityFileTagged,
    "item.file.deleted": EntityFileDeleted,
    "item.file.description_updated": EntityFileDescriptionUpdated,
    "item.file.hero_set": EntityFileHeroSet,

    # CRM
    "crm.contact.created": CrmContactCreated,
    "crm.contact.updated": CrmContactUpdated,
    "crm.contact.merged": CrmContactMerged,
    "crm.contact.tagged": CrmContactTagged,
    "crm.contact.note_added": CrmContactNoteAdded,
    "crm.contact.note_updated": CrmContactNoteUpdated,
    "crm.contact.note_removed": CrmContactNoteRemoved,
    "crm.contact.person_added": CrmContactPersonAdded,
    "crm.contact.person_updated": CrmContactPersonUpdated,
    "crm.contact.person_removed": CrmContactPersonRemoved,
    "crm.contact.address_added": CrmContactAddressAdded,
    "crm.contact.address_updated": CrmContactAddressUpdated,
    "crm.contact.address_removed": CrmContactAddressRemoved,
    "crm.contact.file_attached": EntityFileAttached,
    "crm.contact.file_tagged": EntityFileTagged,
    "crm.contact.file_deleted": EntityFileDeleted,
    "crm.contact.file_description_updated": EntityFileDescriptionUpdated,
    "crm.deal.created": CrmDealCreated,
    "crm.deal.stage_changed": CrmDealStageChanged,
    "crm.deal.won": CrmDealWon,
    "crm.deal.lost": CrmDealLost,
    "crm.deal.updated": CrmDealUpdated,
    "crm.deal.deleted": CrmDealDeleted,
    "crm.deal.reopened": CrmDealReopened,

    # Documents
    "doc.created": DocCreated,
    "doc.updated": DocUpdated,
    "doc.renumbered": DocRenumbered,
    "doc.patched": DocPatched,
    "doc.linked": DocLinked,
    "doc.finalized": DocFinalized,
    "doc.voided": DocVoided,
    "doc.reverted_to_draft": DocRevertedToDraft,
    "doc.unvoided": DocUnvoided,
    "doc.converted_to_bill": DocConvertedToBill,
    "doc.sent": DocSent,
    "doc.payment.received": DocPaymentReceived,
    "doc.payment.refunded": DocPaymentRefunded,
    "doc.payment.voided": DocPaymentVoided,
    "doc.payment.deleted": DocPaymentDeleted,
    "doc.converted": DocConverted,
    "doc.received": DocReceived,
    "doc.shared_import": DocSharedImport,
    "doc.note_added": DocNoteAdded,
    "doc.note_updated": DocNoteUpdated,
    "doc.note_removed": DocNoteRemoved,
    "doc.file_attached": EntityFileAttached,
    "doc.file_tagged": EntityFileTagged,
    "doc.file_deleted": EntityFileDeleted,
    "doc.file_description_updated": EntityFileDescriptionUpdated,
    "list.note_added": ListNoteAdded,
    "list.note_updated": ListNoteUpdated,
    "list.note_removed": ListNoteRemoved,
    "doc.fulfilled": DocFulfilled,
    "doc.partially_fulfilled": DocPartiallyFulfilled,
    "doc.return_received": DocReturnReceived,
    "doc.return_undone": DocReturnUndone,
    "doc.receive_undone": DocReceiveUndone,
    "doc.fulfillment_reversed": DocFulfillmentReversed,

    # Manufacturing
    "mfg.order.created": MfgOrderCreated,
    "mfg.order.started": MfgOrderStarted,
    "mfg.order.completed": MfgOrderCompleted,
    "mfg.order.cancelled": MfgOrderCancelled,
    "mfg.order.on_hold": MfgOrderOnHold,
    "mfg.order.resumed": MfgOrderResumed,
    "mfg.order.issued": MfgOrderIssued,
    "mfg.order.received": MfgOrderReceived,
    "mfg.order.scheduled": MfgOrderScheduled,

    # Scanning
    "scan.barcode": ScanBarcode,
    "scan.rfid": ScanRfid,
    "scan.nfc": ScanNfc,
    "scan.resolved": ScanResolved,

    # Marketplace
    "mp.listing.created": MpListingCreated,
    "mp.listing.updated": MpListingUpdated,
    "mp.listing.published": MpListingPublished,
    "mp.listing.unpublished": MpListingUnpublished,
    "mp.order.received": MpOrderReceived,
    "mp.order.fulfilled": MpOrderFulfilled,
    "mp.order.cancelled": MpOrderCancelled,

    # Accounting
    "acc.journal_entry.created": AccJournalEntryCreated,
    "acc.journal_entry.posted": AccJournalEntryPosted,
    "acc.journal_entry.voided": AccJournalEntryVoided,
    "acc.period.closed": AccPeriodClosed,
    "acc.period.reopened": AccPeriodReopened,

    # System
    "sys.company.created": SysCompanyCreated,
    "sys.user.created": SysUserCreated,
    "sys.user.deactivated": SysUserDeactivated,
    "sys.api_key.created": SysApiKeyCreated,
    "sys.api_key.revoked": SysApiKeyRevoked,
    "sys.backup.created": SysBackupCreated,
    "sys.migration.applied": SysMigrationApplied,

    # Lists
    "list.created": ListCreated,
    "list.updated": ListUpdated,
    "list.patched": ListPatched,
    "list.finalized": ListFinalized,
    "list.reverted": ListReverted,
    "list.closed": ListClosed,
    "list.reopened": ListReopened,
    "list.voided": ListVoided,

    # Subscriptions
    "sub.created": SubCreated,
    "sub.updated": SubUpdated,
    "sub.paused": SubPaused,
    "sub.cancelled": SubCancelled,
    "sub.resumed": SubResumed,
    "sub.generated": SubGenerated,
    "sub.expired": SubExpired,
}


class ModuleEvent(BaseModel):
    """Generic schema for module-defined event types.

    Modules register custom event types at load time via register_event_type().
    Events validated against this schema accept any dict payload.
    """
    model_config = {"extra": "allow"}


def register_event_type(event_type: str, schema: type[BaseModel] | None = None) -> None:
    """Register a custom event type (typically called by premium modules at load time).

    Args:
        event_type: The event type string (e.g. "rate.set", "tax_group.set")
        schema: Optional Pydantic model for validation. Defaults to ModuleEvent (accepts anything).
    """
    if event_type not in EVENT_SCHEMA_MAP:
        EVENT_SCHEMA_MAP[event_type] = schema or ModuleEvent
