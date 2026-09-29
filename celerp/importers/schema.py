# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Celerp Import Format (CIF) - canonical intermediate format for all data imports.

Two compatibility surfaces, versioned separately:
  1. CIF (CIF_VERSION) - the standalone import formats:
       CIFRecord / CIFBatch - low-level JSONL format (one ledger event per line),
                              used by the streaming importer for large datasets.
       CIFBundleManifest    - typed bundle of items, contacts and documents,
                              read by the bundle importer.
  2. Migration CIF (MIGRATION_CIF_VERSION) - CIFImportBundle / CIFImportManifest,
       produced by a source adapter and read only by the migration runner.

Every migration bundle entity carries its source provenance (`source_system`,
`source_type`, `source_external_id`). References between entities hold the
referenced entity's `source_external_id`. Money, rates and quantities are
Decimal: binary floats, NaN and Infinity are rejected, and dates must be
unambiguous ISO 8601.

Schema versioning: bump the constant of the surface whose fields change in a
breaking way. The migration manifest accepts only the current migration version;
a migration run records it and resumes only under it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from celerp.compat import StrEnum


CIF_VERSION = "1"
MIGRATION_CIF_VERSION = "2"


# ── Shared field types ─────────────────────────────────────────────────────────

def _require_migration_version(value: str) -> str:
    if value != MIGRATION_CIF_VERSION:
        raise ValueError(f"Unsupported migration CIF version {value!r}. "
                         f"This Celerp reads migration CIF version {MIGRATION_CIF_VERSION}.")
    return value


def _reject_float(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("Expected a decimal amount, got a boolean.")
    if isinstance(value, float):
        raise ValueError("Amounts must be Decimal or decimal strings, never binary floats.")
    return value


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _strict_date(value: Any) -> Any:
    if isinstance(value, datetime):
        raise ValueError("Expected a date, got a date and time.")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE.match(value):
        return date.fromisoformat(value)
    raise ValueError(f"Ambiguous or invalid date {value!r}. Use ISO 8601 (YYYY-MM-DD).")


def _strict_datetime(value: Any) -> Any:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and _ISO_DATETIME.match(value):
        return datetime.fromisoformat(value)
    raise ValueError(f"Ambiguous or invalid date and time {value!r}. Use ISO 8601 (YYYY-MM-DDTHH:MM).")


MigrationCIFVersion = Annotated[str, AfterValidator(_require_migration_version)]
CIFDecimal = Annotated[Decimal, BeforeValidator(_reject_float), Field(allow_inf_nan=False)]
CIFDate = Annotated[date, BeforeValidator(_strict_date)]
CIFDateTime = Annotated[datetime, BeforeValidator(_strict_datetime)]
NonEmptyStr = Annotated[str, Field(min_length=1)]


# ── Low-level CIF (JSONL / event-per-line) ─────────────────────────────────────

class CIFEntityType(StrEnum):
    ITEM = "item"
    CONTACT = "contact"
    INVOICE = "invoice"
    PURCHASE_ORDER = "purchase_order"
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
    source: str = Field(..., description="Import source identifier, e.g. 'import:old_system'")
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


# ── CIF bundle (standalone bundle importer) ────────────────────────────────────

class CIFBundleItem(BaseModel):
    """A single inventory item."""
    external_id: str                          # source system ID (e.g. "gc:472043")
    sku: str | None = None
    name: str
    description: str | None = None
    weight: Decimal | None = None
    weight_unit: str | None = None            # e.g. "kg", "g", "oz", "ct", "lb"
    sell_by: str | None = None                # "piece" or "weight" - how price is quoted
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
    attributes: dict[str, Any] = Field(default_factory=dict)  # industry-specific fields
    metadata: dict[str, Any] = Field(default_factory=dict)


class CIFBundleContact(BaseModel):
    """A customer or supplier contact."""
    external_id: str
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CIFBundleLineItem(BaseModel):
    """A line item on an invoice."""
    item_external_id: str
    quantity: Decimal
    weight: Decimal | None = None
    weight_unit: str | None = None            # e.g. "kg", "g", "oz", "ct", "lb"
    unit_price: Decimal
    total_price: Decimal
    cost_basis: Decimal | None = None         # cost at time of sale


class CIFBundleDocument(BaseModel):
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
    line_items: list[CIFBundleLineItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CIFBundle(BaseModel):
    """Complete set of entities for one bundle import."""
    items: list[CIFBundleItem] = Field(default_factory=list)
    contacts: list[CIFBundleContact] = Field(default_factory=list)
    documents: list[CIFBundleDocument] = Field(default_factory=list)


class CIFBundleManifest(BaseModel):
    """Top-level wrapper written to my_company_cif.json."""
    cif_version: str = CIF_VERSION
    source: str                               # e.g. "mycompany_2026"
    exported_at: datetime
    bundle: CIFBundle
    stats: dict[str, Any] = Field(default_factory=dict)


# ── Migration bundle entities: provenance base ─────────────────────────────────

class CIFSourceRecord(BaseModel):
    """Provenance shared by every bundle entity."""

    source_system: NonEmptyStr                # e.g. "manager_io"
    source_type: NonEmptyStr                  # source object type, e.g. "SalesInvoice"
    source_external_id: NonEmptyStr           # stable ID of the object in the source system
    source_ref: str | None = None             # human reference in the source (number, code)
    source_timestamp: CIFDateTime | None = None
    source_fingerprint: str | None = None     # deterministic content hash where useful


# ── Company, currencies, accounts, taxes, locations ────────────────────────────

class CIFCompanyProfile(CIFSourceRecord):
    """The migrated company's identity and settings where Celerp has an equivalent."""
    name: NonEmptyStr
    base_currency: Annotated[str, Field(min_length=3, max_length=3)]
    fiscal_year_start_month: Annotated[int, Field(ge=1, le=12)] | None = None
    address: str | None = None
    tax_id: str | None = None
    money_precision: Annotated[int, Field(ge=0, le=8)] | None = None


class CIFCurrency(CIFSourceRecord):
    code: Annotated[str, Field(min_length=3, max_length=3)]
    name: str | None = None
    precision: Annotated[int, Field(ge=0, le=8)] = 2


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"
    COGS = "cogs"


class AccountControl(StrEnum):
    """Control-account semantics that a display name alone cannot carry."""
    RECEIVABLE = "receivable"
    PAYABLE = "payable"
    BANK = "bank"
    CASH = "cash"
    TAX = "tax"
    INVENTORY = "inventory"
    RETAINED_EARNINGS = "retained_earnings"


class CIFAccount(CIFSourceRecord):
    code: str | None = None
    name: NonEmptyStr
    account_type: AccountType
    control: AccountControl | None = None
    parent_external_id: str | None = None
    currency: str | None = None               # set for foreign-currency bank/cash accounts
    is_active: bool = True


class CIFTaxCode(CIFSourceRecord):
    name: NonEmptyStr
    rate_percent: Annotated[CIFDecimal, Field(ge=0)]
    inclusive: bool = False
    account_external_id: str | None = None    # tax control account


class CIFLocation(CIFSourceRecord):
    name: NonEmptyStr
    address: str | None = None


# ── Contacts and inventory masters ─────────────────────────────────────────────

class ContactRole(StrEnum):
    CUSTOMER = "customer"
    SUPPLIER = "supplier"


class CIFContact(CIFSourceRecord):
    """A customer or supplier contact. Distinct source IDs are never merged."""
    name: str
    roles: list[ContactRole] = Field(default_factory=list)
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    tax_id: str | None = None
    currency: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# Statuses an imported item may start in. Every other status (sold, merged, reserved,
# memo_out...) is reached only through the events that move stock, so an import
# never manufactures one. The inventory item writer enforces the same list.
ImportItemStatus = Literal["available", "draft", "archived"]
IMPORT_ITEM_STATUSES: tuple[str, ...] = get_args(ImportItemStatus)


class CIFItem(CIFSourceRecord):
    """A single inventory item master."""
    sku: str | None = None
    name: str
    description: str | None = None
    unit: str | None = None                   # counting unit, e.g. "each", "box"
    weight: CIFDecimal | None = None
    weight_unit: str | None = None            # e.g. "kg", "g", "oz", "ct", "lb"
    sell_by: str | None = None                # "piece" or "weight" - how price is quoted
    cost_per_unit: CIFDecimal | None = None   # cost per unit of weight (generic)
    total_cost: CIFDecimal | None = None
    wholesale_price: CIFDecimal | None = None
    retail_price: CIFDecimal | None = None
    status: ImportItemStatus
    category: str | None = None
    parent_external_id: str | None = None     # split lineage
    barcode: str | None = None
    location_name: str | None = None          # resolved to location_id at import time
    attributes: dict[str, Any] = Field(default_factory=dict)  # industry-specific fields
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Operational documents ──────────────────────────────────────────────────────

class DocumentType(StrEnum):
    QUOTATION = "quotation"
    SALES_ORDER = "sales_order"
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    PURCHASE_ORDER = "purchase_order"
    BILL = "bill"
    DEBIT_NOTE = "debit_note"


class CIFLineItem(BaseModel):
    """A document line, posting its net to `account_external_id` and its tax to
    `tax_account_external_id`."""
    item_external_id: str | None = None
    description: str | None = None
    account_external_id: str | None = None
    tax_code_external_id: str | None = None
    tax_account_external_id: str | None = None
    quantity: CIFDecimal
    weight: CIFDecimal | None = None
    weight_unit: str | None = None            # e.g. "kg", "g", "oz", "ct", "lb"
    unit_price: CIFDecimal
    discount_percent: CIFDecimal | None = None   # a line discount as a percentage of quantity x unit price
    tax_amount: CIFDecimal | None = None
    total_price: CIFDecimal
    cost_basis: CIFDecimal | None = None      # cost at time of sale


class CIFDocument(CIFSourceRecord):
    """A sales or purchase document."""
    doc_type: DocumentType
    status: Literal["draft", "awaiting_payment", "paid", "void"]
    contact_external_id: str | None = None
    ref: str | None = None
    issue_date: CIFDate | None = None
    payment_due_date: CIFDate | None = None
    currency: str | None = None
    total: CIFDecimal
    tax_total: CIFDecimal | None = None
    amount_paid: CIFDecimal
    amount_outstanding: CIFDecimal
    line_items: list[CIFLineItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Settlements, transfers, journals, inventory positions, attachments ────────

class SettlementType(StrEnum):
    RECEIPT = "receipt"
    PAYMENT = "payment"


class CIFAllocation(BaseModel):
    document_external_id: NonEmptyStr
    amount: Annotated[CIFDecimal, Field(gt=0)]


class CIFSettlement(CIFSourceRecord):
    """A receipt or payment with its allocations to documents."""
    settlement_type: SettlementType
    settlement_date: CIFDate
    contact_external_id: str | None = None
    bank_account_external_id: NonEmptyStr
    currency: str | None = None
    amount: Annotated[CIFDecimal, Field(gt=0)]
    allocations: list[CIFAllocation] = Field(default_factory=list)

    @model_validator(mode="after")
    def allocations_within_amount(self) -> "CIFSettlement":
        allocated = sum((a.amount for a in self.allocations), Decimal(0))
        if allocated > self.amount:
            raise ValueError(f"Allocations total {allocated} exceeds settlement amount {self.amount}.")
        return self


class CIFBankTransfer(CIFSourceRecord):
    """One transfer between two bank/cash accounts, preserved once."""
    transfer_date: CIFDate
    from_account_external_id: NonEmptyStr
    to_account_external_id: NonEmptyStr
    amount: Annotated[CIFDecimal, Field(gt=0)]
    to_amount: Annotated[CIFDecimal, Field(gt=0)] | None = None  # set when currencies differ


class CIFJournalLine(BaseModel):
    account_external_id: NonEmptyStr
    debit: Annotated[CIFDecimal, Field(ge=0)] = Decimal(0)
    credit: Annotated[CIFDecimal, Field(ge=0)] = Decimal(0)
    description: str | None = None
    contact_external_id: str | None = None

    @model_validator(mode="after")
    def one_side_only(self) -> "CIFJournalLine":
        if (self.debit == 0) == (self.credit == 0):
            raise ValueError("A journal line needs exactly one of debit or credit.")
        return self


class CIFJournalEntry(CIFSourceRecord):
    entry_date: CIFDate
    narration: str | None = None
    currency: str | None = None
    lines: Annotated[list[CIFJournalLine], Field(min_length=2)]

    @model_validator(mode="after")
    def balanced(self) -> "CIFJournalEntry":
        debits = sum((ln.debit for ln in self.lines), Decimal(0))
        credits = sum((ln.credit for ln in self.lines), Decimal(0))
        if debits != credits:
            raise ValueError(f"Journal is unbalanced: debits {debits} != credits {credits}.")
        return self


class CIFInventoryAdjustment(CIFSourceRecord):
    """An opening position or a quantity/value adjustment for one item at one location."""
    kind: Literal["opening", "adjustment"]
    adjustment_date: CIFDate
    item_external_id: NonEmptyStr
    location_external_id: str | None = None
    quantity: CIFDecimal                      # signed
    value: CIFDecimal | None = None           # signed, base currency


class CIFAttachment(CIFSourceRecord):
    """Descriptor of a file attached to another entity. The declared type is untrusted."""
    file_name: NonEmptyStr
    declared_content_type: str | None = None
    size_bytes: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(min_length=64, max_length=64)]
    target_source_type: NonEmptyStr
    target_source_external_id: NonEmptyStr


# ── Coverage and reconciliation expectations ───────────────────────────────────

class CIFMode(StrEnum):
    FULL_HISTORY = "full_history"
    CUTOVER = "cutover"


class CoverageClass(StrEnum):
    MAPPED = "mapped"
    MAPPED_WITH_LOSS = "mapped_with_loss"
    IGNORED_NON_BUSINESS = "ignored_non_business"
    UNSUPPORTED_NONFINANCIAL = "unsupported_nonfinancial"
    UNSUPPORTED_FINANCIAL_BLOCKER = "unsupported_financial_blocker"
    UNCLASSIFIED = "unclassified"


class CIFCoverageEntry(BaseModel):
    """How one source object type is carried into Celerp."""
    source_type: NonEmptyStr
    count: Annotated[int, Field(ge=0)]
    coverage_class: CoverageClass
    target: str | None = None                 # Celerp entity type it maps to
    note: str | None = None


class ToleranceKind(StrEnum):
    EXACT = "exact"
    CURRENCY_ROUNDING = "currency_rounding"


class CIFTolerance(BaseModel):
    """An explicit, precision-aware tolerance. Percentage tolerances do not exist."""
    model_config = ConfigDict(extra="forbid")

    kind: ToleranceKind
    currency: str | None = None
    precision: Annotated[int, Field(ge=0, le=8)] | None = None
    max_units: Annotated[int, Field(ge=0)] = 0  # allowed difference in units of 10**-precision

    @model_validator(mode="after")
    def rule_is_complete(self) -> "CIFTolerance":
        if self.kind == ToleranceKind.EXACT:
            if self.currency is not None or self.precision is not None or self.max_units:
                raise ValueError("An exact tolerance takes no currency, precision or allowance.")
        elif self.currency is None or self.precision is None:
            raise ValueError("A currency rounding tolerance names its currency and precision.")
        return self


class ReconciliationMeasure(StrEnum):
    DEBITS_EQUAL_CREDITS = "debits_equal_credits"
    TRIAL_BALANCE = "trial_balance"
    AR_CONTROL = "ar_control"
    AR_BY_CUSTOMER = "ar_by_customer"
    AP_CONTROL = "ap_control"
    AP_BY_SUPPLIER = "ap_by_supplier"
    BANK_CASH = "bank_cash"
    INVENTORY_QUANTITY = "inventory_quantity"
    INVENTORY_VALUE = "inventory_value"
    TAX_CONTROL = "tax_control"
    DOCUMENT_COUNT = "document_count"
    DOCUMENT_TOTAL = "document_total"
    DOCUMENT_STATUS = "document_status"
    SETTLEMENT_ALLOCATION = "settlement_allocation"


class ReconciliationExpectation(BaseModel):
    """One source-side figure the destination must reproduce."""
    measure: ReconciliationMeasure
    key: str = ""                             # account, contact, item/location, doc type, ...
    currency: str | None = None
    expected: CIFDecimal
    tolerance: CIFTolerance


class ReconciliationExpectations(BaseModel):
    """Source-side expectations computed directly from the source, independent of CIF conversion."""
    expectations: list[ReconciliationExpectation] = Field(default_factory=list)


# ── Migration bundle and manifest ──────────────────────────────────────────────

class CIFImportBundle(BaseModel):
    """Complete set of entities for one import."""
    company: CIFCompanyProfile | None = None
    currencies: list[CIFCurrency] = Field(default_factory=list)
    accounts: list[CIFAccount] = Field(default_factory=list)
    tax_codes: list[CIFTaxCode] = Field(default_factory=list)
    locations: list[CIFLocation] = Field(default_factory=list)
    contacts: list[CIFContact] = Field(default_factory=list)
    items: list[CIFItem] = Field(default_factory=list)
    documents: list[CIFDocument] = Field(default_factory=list)
    settlements: list[CIFSettlement] = Field(default_factory=list)
    bank_transfers: list[CIFBankTransfer] = Field(default_factory=list)
    journals: list[CIFJournalEntry] = Field(default_factory=list)
    inventory_adjustments: list[CIFInventoryAdjustment] = Field(default_factory=list)
    attachments: list[CIFAttachment] = Field(default_factory=list)

    def source_records(self) -> list[CIFSourceRecord]:
        """Every entity in the bundle, in import dependency order."""
        records: list[CIFSourceRecord] = [self.company] if self.company else []
        for group in (
            self.currencies, self.accounts, self.tax_codes,
            self.locations, self.contacts, self.items, self.documents,
            self.settlements, self.bank_transfers, self.journals,
            self.inventory_adjustments, self.attachments,
        ):
            records.extend(group)
        return records


class CIFImportManifest(BaseModel):
    """Top-level wrapper produced by a source adapter for the migration runner."""
    cif_version: MigrationCIFVersion = MIGRATION_CIF_VERSION
    source: str                               # human label, e.g. "mycompany_2026"
    source_system: NonEmptyStr                # adapter key, e.g. "manager_io"
    source_schema_version: str | None = None
    adapter_version: NonEmptyStr
    mode: CIFMode = CIFMode.FULL_HISTORY
    cutover_date: CIFDate | None = None
    exported_at: CIFDateTime
    bundle: CIFImportBundle
    coverage: list[CIFCoverageEntry] = Field(default_factory=list)
    source_summary: dict[str, Any] = Field(default_factory=dict)
    reconciliation_expectations: ReconciliationExpectations = Field(default_factory=ReconciliationExpectations)

    @model_validator(mode="after")
    def consistent(self) -> "CIFImportManifest":
        if (self.mode == CIFMode.CUTOVER) != (self.cutover_date is not None):
            raise ValueError("A cutover date is required for cutover mode and only for cutover mode.")
        foreign = {r.source_system for r in self.bundle.source_records()} - {self.source_system}
        if foreign:
            raise ValueError(f"Bundle entities from {sorted(foreign)} do not match source_system {self.source_system!r}.")
        return self
