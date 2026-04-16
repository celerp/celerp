# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Celerp Import Format (CIF) - canonical intermediate format for all data imports.

Two layers:
  1. CIFRecord / CIFBatch  — low-level JSONL format (one ledger event per line).
                             Used by the streaming importer for large datasets.
  2. CIFImportBundle / CIFImportManifest — higher-level typed bundle format.
                             Source adapters produce a manifest; the bundle
                             importer reads it. All future adapters (QuickBooks,
                             Shopify, etc.) output CIF bundles.

Schema versioning: bump CIF_VERSION when fields change in a breaking way.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


CIF_VERSION = "1"


# ── Low-level CIF (JSONL / event-per-line) ─────────────────────────────────────

class CIFEntityType(StrEnum):
    ITEM = "item"
    CONTACT = "contact"
    INVOICE = "invoice"
    PURCHASE_ORDER = "purchase_order"
    MEMO = "memo"
    PRODUCTION = "production"
    SHIPPING_DOC = "shipping_doc"
    LOCATION = "location"
    COMPANY = "company"


class CIFRecord(BaseModel):
    """One record in a CIF file. Maps 1:1 to a ledger event."""

    cif_version: str = CIF_VERSION

    # Target entity - must be stable and unique within this import batch
    entity_id: str = Field(..., description="Stable ID for the entity, e.g. 'item:gc:472043'")
    entity_type: CIFEntityType

    # Which Celerp event this record produces
    event_type: str = Field(..., description="Celerp event type, e.g. 'item.snapshot'")

    # The event payload - validated against the Celerp event schema by the importer
    data: dict[str, Any]

    # Provenance
    source: str = Field(..., description="Import source identifier, e.g. 'import:gemcloud'")
    source_id: str | None = Field(None, description="Original ID in the source system")
    idempotency_key: str = Field(..., description="Globally unique key - re-running import is safe")

    # Optional original timestamp from source system
    source_ts: datetime | None = None

    # Human-readable note for dry-run output and reconciliation reports
    note: str | None = None

    @field_validator("event_type")
    @classmethod
    def event_type_must_be_known(cls, v: str) -> str:
        from celerp.events.schemas import EVENT_SCHEMA_MAP  # avoid circular at module load

        if v not in EVENT_SCHEMA_MAP:
            raise ValueError(f"Unknown event_type: {v!r}. Register it in EVENT_SCHEMA_MAP.")
        return v

    @model_validator(mode="after")
    def entity_type_matches_event(self) -> "CIFRecord":
        prefix = self.event_type.split(".")[0]
        entity_prefix = {
            CIFEntityType.ITEM: "item",
            CIFEntityType.CONTACT: "crm",
            CIFEntityType.INVOICE: "doc",
            CIFEntityType.PURCHASE_ORDER: "doc",
            CIFEntityType.MEMO: "crm",
            CIFEntityType.PRODUCTION: "mfg",
            CIFEntityType.SHIPPING_DOC: "doc",
        }
        expected = entity_prefix.get(self.entity_type)
        if expected and prefix != expected:
            raise ValueError(
                f"entity_type={self.entity_type!r} expects event prefix {expected!r}, "
                f"got {prefix!r} from event_type={self.event_type!r}"
            )
        return self


class CIFBatch(BaseModel):
    """A complete import batch. Written/read as JSONL (one CIFRecord per line)."""

    cif_version: str = CIF_VERSION
    source: str
    source_system: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    record_count: int = 0
    notes: str | None = None


# ── High-level CIF Bundle (typed, structured) ──────────────────────────────────

class CIFItem(BaseModel):
    """A single inventory item."""
    external_id: str                          # source system ID (e.g. "gc:472043")
    sku: str | None = None
    name: str
    description: str | None = None
    weight: Decimal | None = None
    weight_unit: str | None = None            # e.g. "kg", "g", "oz", "ct", "lb"
    sell_by: str | None = None                # "piece" or "weight" — how price is quoted
    cost_per_unit: Decimal | None = None      # cost per unit of weight (generic)
    total_cost: Decimal | None = None
    wholesale_price: Decimal | None = None
    retail_price: Decimal | None = None
    status: Literal["available", "memo_out", "production", "sold", "void"]
    category: str | None = None
    parent_external_id: str | None = None     # split lineage
    barcode: str | None = None
    source_ref: str | None = None             # original ref number
    location_name: str | None = None          # resolved to location_id at import time
    created_at: datetime | None = None
    updated_at: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)  # industry-specific fields
    metadata: dict[str, Any] = Field(default_factory=dict)


class CIFContact(BaseModel):
    """A customer or supplier contact."""
    external_id: str
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CIFLineItem(BaseModel):
    """A line item on an invoice."""
    item_external_id: str
    quantity: Decimal
    weight: Decimal | None = None
    weight_unit: str | None = None            # e.g. "kg", "g", "oz", "ct", "lb"
    unit_price: Decimal
    total_price: Decimal
    cost_basis: Decimal | None = None         # cost at time of sale


class CIFDocument(BaseModel):
    """An invoice, PO, or credit note."""
    external_id: str
    doc_type: Literal["invoice", "purchase_order", "credit_note"]
    status: Literal["draft", "awaiting_payment", "paid", "void"]
    contact_external_id: str | None = None
    ref: str | None = None
    total: Decimal
    amount_paid: Decimal
    amount_outstanding: Decimal
    payment_due_date: date | None = None
    created_at: datetime | None = None
    line_items: list[CIFLineItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CIFMemo(BaseModel):
    """A customer memo (consignment out)."""
    external_id: str
    status: Literal["draft", "out", "returned", "invoiced"]
    contact_external_id: str | None = None
    total: Decimal
    created_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CIFImportBundle(BaseModel):
    """Complete set of entities for one import batch."""
    items: list[CIFItem] = Field(default_factory=list)
    contacts: list[CIFContact] = Field(default_factory=list)
    documents: list[CIFDocument] = Field(default_factory=list)
    memos: list[CIFMemo] = Field(default_factory=list)


class CIFImportManifest(BaseModel):
    """Top-level wrapper written to my_company_cif.json."""
    cif_version: str = CIF_VERSION
    source: str                               # e.g. "mycompany_gemcloud_2026"
    exported_at: datetime
    bundle: CIFImportBundle
    stats: dict[str, Any] = Field(default_factory=dict)
