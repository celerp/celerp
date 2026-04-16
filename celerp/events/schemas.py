# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

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
    weight: float | None = None
    weight_unit: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class ItemSnapshot(BaseModel):
    """Snapshot of full item state - used for imports. sku/name optional (absent in some sources)."""

    sku: str | None = None
    name: str | None = None
    quantity: float = 0
    category: str | None = None
    location_id: str | None = None
    sell_by: str | None = None
    weight: float | None = None
    weight_unit: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    entity_id: str | None = None
    is_available: bool | None = None
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


class ItemDisposed(BaseModel):
    reason: str | None = None


class ItemSplit(BaseModel):
    child_ids: list[str]
    child_skus: list[str] = Field(default_factory=list)
    quantities: list[float]


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


class CrmMemoCreated(BaseModel):
    contact_id: str | None = None
    notes: str | None = None

    model_config = {"extra": "allow"}  # CIF adapters pass total, status, etc.


class CrmMemoItemAdded(BaseModel):
    item_id: str
    quantity: float | None = None


class CrmMemoItemRemoved(BaseModel):
    item_id: str


class CrmMemoApproved(BaseModel):
    notes: str | None = None


class CrmMemoCancelled(BaseModel):
    reason: str | None = None


class CrmMemoInvoiced(BaseModel):
    doc_id: str
    items_invoiced: list[str] = Field(default_factory=list)


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


class CrmMemoReturned(BaseModel):
    items_returned: list[dict[str, Any]] = Field(default_factory=list)


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
    payment_date: str | None = None
    bank_account: str | None = None
    source_doc_id: str | None = None
    target_doc_id: str | None = None


class DocPaymentRefunded(BaseModel):
    amount: float
    reason: str | None = None
    method: str | None = None


class DocPaymentVoided(BaseModel):
    payment_index: int
    void_reason: str | None = None


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
    text: str


class DocFulfilled(BaseModel):
    fulfilled_items: list[dict[str, Any]]
    fulfilled_by: str
    fulfilled_at: str = ""
    strategy: str
    total_cogs: float


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


class MfgStepCompleted(BaseModel):
    step_id: str
    notes: str | None = None


# -----------------
# BOM
# -----------------

class BOMCreated(BaseModel):
    name: str
    output_item_id: str | None = None
    output_qty: float = 1.0
    components: list[dict[str, Any]] = Field(default_factory=list)


class BOMUpdated(BaseModel):
    model_config = {"extra": "allow"}


class BOMDeleted(BaseModel):
    pass


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


class ListSent(BaseModel):
    sent_via: str | None = None
    sent_to: str | None = None


class ListAccepted(BaseModel):
    notes: str | None = None


class ListCompleted(BaseModel):
    notes: str | None = None


class ListVoided(BaseModel):
    reason: str | None = None


class ListConverted(BaseModel):
    target_doc_id: str
    target_doc_type: str


EVENT_SCHEMA_MAP: dict[str, type[BaseModel]] = {
    # Items
    "item.created": ItemCreated,
    "item.snapshot": ItemSnapshot,
    "item.updated": ItemUpdated,
    "item.pricing.set": ItemPricingSet,
    "item.status.set": ItemStatusSet,
    "item.transferred": ItemTransferred,
    "item.quantity.adjusted": ItemQuantityAdjusted,
    "item.fulfilled": ItemFulfilled,
    "item.fulfillment_reversed": ItemFulfillmentReversed,
    "item.expired": ItemExpired,
    "item.disposed": ItemDisposed,
    "item.split": ItemSplit,
    "item.merged": ItemMerged,
    "item.source_deactivated": ItemSourceDeactivated,
    "item.patched": ItemPatched,
    "item.consumed": ItemConsumed,
    "item.produced": ItemProduced,
    "item.reserved": ItemReserved,
    "item.unreserved": ItemUnreserved,

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
    "crm.deal.created": CrmDealCreated,
    "crm.deal.stage_changed": CrmDealStageChanged,
    "crm.deal.won": CrmDealWon,
    "crm.deal.lost": CrmDealLost,
    "crm.deal.updated": CrmDealUpdated,
    "crm.deal.deleted": CrmDealDeleted,
    "crm.deal.reopened": CrmDealReopened,
    "crm.memo.created": CrmMemoCreated,
    "crm.memo.item_added": CrmMemoItemAdded,
    "crm.memo.item_removed": CrmMemoItemRemoved,
    "crm.memo.approved": CrmMemoApproved,
    "crm.memo.cancelled": CrmMemoCancelled,
    "crm.memo.invoiced": CrmMemoInvoiced,
    "crm.memo.returned": CrmMemoReturned,

    # Documents
    "doc.created": DocCreated,
    "doc.updated": DocUpdated,
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
    "doc.converted": DocConverted,
    "doc.received": DocReceived,
    "doc.shared_import": DocSharedImport,
    "doc.note_added": DocNoteAdded,
    "doc.fulfilled": DocFulfilled,
    "doc.partially_fulfilled": DocPartiallyFulfilled,
    "doc.fulfillment_reversed": DocFulfillmentReversed,

    # Manufacturing
    "mfg.order.created": MfgOrderCreated,
    "mfg.order.started": MfgOrderStarted,
    "mfg.order.completed": MfgOrderCompleted,
    "mfg.order.cancelled": MfgOrderCancelled,
    "mfg.step.completed": MfgStepCompleted,

    # BOM
    "bom.created": BOMCreated,
    "bom.updated": BOMUpdated,
    "bom.deleted": BOMDeleted,

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
    "list.sent": ListSent,
    "list.accepted": ListAccepted,
    "list.completed": ListCompleted,
    "list.voided": ListVoided,
    "list.converted": ListConverted,

    # Subscriptions
    "sub.created": SubCreated,
    "sub.updated": SubUpdated,
    "sub.paused": SubPaused,
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
