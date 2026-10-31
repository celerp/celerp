# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Connector base interface.

Each connector (Shopify, QuickBooks, Xero, ...) implements ConnectorBase.
Connectors do NOT hold OAuth tokens directly - tokens are brokered by the
CelERP relay service and injected at call time via ConnectorContext.

Sync direction is set per kind of record (see EntityFlow):
  - INBOUND: external platform -> Celerp (pull products, orders, contacts)
  - OUTBOUND: Celerp -> external platform (push items, invoices, inventory)
  - BOTH: bidirectional
  - OFF: not synced
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SyncDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    BOTH = "both"
    OFF = "off"


class SyncEntity(str, Enum):
    PRODUCTS = "products"
    ORDERS = "orders"
    CONTACTS = "contacts"
    INVENTORY = "inventory"
    INVOICES = "invoices"
    DOCUMENTS = "documents"
    ATTACHMENTS = "attachments"
    JOURNALS = "journals"
    PERIOD_LOCK = "period_lock"


class ConnectorCategory(str, Enum):
    WEBSITE = "website"
    ACCOUNTING = "accounting"


class SyncFrequency(str, Enum):
    REALTIME = "realtime"   # webhook-driven (e-commerce only)
    MANUAL = "manual"       # user clicks Sync Now
    DAILY = "daily"         # once per day at configured hour


# The one list of sync entities. Inbound entities are pulled from the platform; the
# *_out entities are pushed from Celerp to the platform, in dispatch order (documents
# before the stock counts and files that refer to them). Each entity is run by the
# connector method named sync_<entity>.
INBOUND_ENTITIES = ("products", "orders", "contacts", "inventory", "journals", "period_lock")
OUTBOUND_ENTITIES = (
    "products_out", "contacts_out", "journals_out", "invoices_out", "documents_out",
    "inventory_out", "attachments_out",
)


@dataclass(frozen=True)
class EntityFlow:
    """One kind of record a person sets a direction for, and the syncs that carry it
    each way: ``inbound`` names INBOUND_ENTITIES pulled from the platform,
    ``outbound`` names OUTBOUND_ENTITIES pushed to it."""
    inbound: tuple[str, ...] = ()
    outbound: tuple[str, ...] = ()

    def allowed(self) -> tuple[SyncDirection, ...]:
        """The directions this record can be set to: the ways the connector can carry
        it, both when it carries it each way, and always Off."""
        values = []
        if self.inbound:
            values.append(SyncDirection.INBOUND)
        if self.outbound:
            values.append(SyncDirection.OUTBOUND)
        if self.inbound and self.outbound:
            values.append(SyncDirection.BOTH)
        return (*values, SyncDirection.OFF)

    def widest(self) -> SyncDirection:
        """The direction carrying the record every way the connector can."""
        return self.allowed()[-2] if len(self.allowed()) > 1 else SyncDirection.OFF

    def runs(self, direction: SyncDirection) -> tuple[str, ...]:
        """The syncs this record runs when set to ``direction``."""
        pulls = direction in (SyncDirection.INBOUND, SyncDirection.BOTH)
        pushes = direction in (SyncDirection.OUTBOUND, SyncDirection.BOTH)
        return (*(self.inbound if pulls else ()), *(self.outbound if pushes else ()))


class DirectionError(ValueError):
    """A record was set to a direction its connector cannot sync it in."""


def _direction_of(directions: dict | None, key: str) -> SyncDirection:
    try:
        return SyncDirection((directions or {}).get(key, SyncDirection.OFF.value))
    except ValueError:
        return SyncDirection.OFF


def entity_allowed(connector: "ConnectorBase", entity: str, directions: dict | None) -> bool:
    """Whether the sync ``entity`` runs under these per-record directions."""
    return any(
        entity in flow.runs(_direction_of(directions, key))
        for key, flow in connector.entity_flows.items()
    )


def direction_label(connector: "ConnectorBase", direction: SyncDirection, lang: str | None = None) -> str:
    """How a direction reads to a person, naming the platform."""
    from ui.i18n import t

    return t(f"connectors.flow_dir_{direction.value}", lang, service=connector.display_name)


def entity_flow_label(key: str, lang: str | None = None) -> str:
    """The name a person knows one kind of record by."""
    from ui.i18n import t

    return t(f"connectors.flow_{key}", lang)


def validate_directions(connector: "ConnectorBase", directions: dict) -> dict[str, str]:
    """Return a full per-record direction map for ``connector``, every record named.
    Raises DirectionError naming the record and the direction it cannot take."""
    from ui.i18n import t

    unknown = set(directions) - set(connector.entity_flows)
    if unknown:
        raise DirectionError(t("connectors.err_unknown_flow", entity=", ".join(sorted(unknown)),
                               service=connector.display_name))
    out: dict[str, str] = {}
    for key, flow in connector.entity_flows.items():
        raw = directions.get(key, SyncDirection.OFF.value)
        try:
            value = SyncDirection(raw)
        except ValueError:
            value = None
        if value not in flow.allowed():
            raise DirectionError(t(
                "connectors.err_flow_direction", entity=entity_flow_label(key),
                direction=direction_label(connector, value) if value else str(raw),
                service=connector.display_name,
            ))
        out[key] = value.value
    return out


def restored_directions(connector: "ConnectorBase") -> dict[str, str]:
    """A company restored from a backup pushes nothing outward until its owner says so:
    each record that can come in does, the rest are Off."""
    return {
        key: (SyncDirection.INBOUND if flow.inbound else SyncDirection.OFF).value
        for key, flow in connector.entity_flows.items()
    }


@dataclass
class ConnectorContext:
    """Runtime context injected per sync call. Never stored on the connector."""
    company_id: str
    access_token: str          # short-lived token from relay service; empty for connectors whose calls go through the relay
    store_handle: str | None = None   # e.g. Shopify myshopify domain
    extra: dict[str, Any] | None = None


@dataclass
class RemoteProduct:
    """One sellable product as a platform holds it (a variation counts as its own),
    read the same way for a sync and for the first sync's preview."""
    key: str                    # the platform identity: product id, or "product:variation"
    product_id: str
    variation_id: str | None
    sku: str                    # "" when the platform has none
    name: str
    description: str | None
    price: float | None
    modified: datetime | None   # when the platform last changed it
    extra: dict = field(default_factory=dict)  # platform fields only its sync reads


@dataclass
class RemoteOrder:
    """One order (or invoice) as a platform holds it, for the first sync's preview.
    ``posts`` is whether importing it issues an invoice that posts revenue and stock."""
    id: str
    number: str
    status: str
    created: datetime | None
    total: float | None
    currency: str | None
    posts: bool


@dataclass
class ExternalImportRecord:
    """What a connector's product import did, for its attention list and its undo.

    ``created``: (entity id, ledger key of its creation) for each item it created.
    ``linked``: {"entity_id", "restore"} for each existing item it newly linked by SKU;
    ``restore`` holds the item's values before the store's replaced them, or None when
    the item kept its own. ``conflicts``: {"entity_id", "fields"} for each linked item
    whose field changed in both systems, each field with both values."""
    created: list[tuple[str, str]] = field(default_factory=list)
    linked: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    # Ledger keys of the files pulled onto the items it created.
    files: list[str] = field(default_factory=list)


@dataclass
class FirstSync:
    """A first sync's choices for its product pass, and what that pass did.

    ``match``: "keep" links products that match by SKU keeping Celerp's values,
    "take" replaces them with the platform's. ``create``: whether products new to
    Celerp are created (only when products come in). ``batch_id``: the import batch
    the items it creates join, so its undo removes them with their opening stock.
    ``record``: the ExternalImportRecord the pass fills in."""
    match: str
    create: bool
    batch_id: str | None
    record: ExternalImportRecord


@dataclass
class SyncResult:
    entity: SyncEntity
    direction: SyncDirection = SyncDirection.INBOUND
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] | None = None
    # Records the run could not import and that a person must resolve, one
    # dict per record: {"id", "label", "reason"}. They do not fail the run;
    # the next run retries them and keeps the ones that still fail. A sync
    # that tracks attention always returns a list (empty when nothing waits);
    # None means the run produced no list and the previous one stands.
    attention: list[dict] | None = None
    # The run did not go ahead because modules were being changed (ModuleBusy): the same
    # run can succeed when tried again.
    busy: bool = False
    # The run was refused for a reason only the company can clear (a module turned off, the
    # direction, a changed store, a lapsed connection, no such sync): kept on the history as
    # an actionable failure, never retried on its own.
    final: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def record(self, outcome: str) -> None:
        """Tally a `connector_upsert`/push outcome ("created" | "updated" | "noop")."""
        if outcome == "created":
            self.created += 1
        elif outcome == "updated":
            self.updated += 1
        else:
            self.skipped += 1


def store_holds_records(matches: list[bool]) -> bool:
    """A store holds imported records when most of the sampled records match
    it, one entry per sampled record, a record the store did not return
    counting as a mismatch. One edited record does not disprove it."""
    return bool(matches) and sum(matches) * 2 > len(matches)


def contact_matches(stored: dict, remote: dict) -> bool:
    """A store's customer is the imported contact when it has the same email,
    or the same phone when there is no email, or the same name when there is
    neither."""
    for key in ("email", "phone", "name"):
        if stored.get(key):
            return str(stored[key]).strip().casefold() == str(remote.get(key) or "").strip().casefold()
    return False


class ConnectorBase(ABC):
    """Abstract base for all platform connectors (inbound + outbound)."""

    name: str
    display_name: str
    # The kinds of record a person sets a direction for, in the order they are shown.
    entity_flows: dict[str, EntityFlow]
    category: ConnectorCategory
    conflict_strategy: dict[str, str]

    @property
    def default_directions(self) -> dict[str, str]:
        """Directions a new connection starts with: every record each way the connector
        can carry it. A connector whose first sync should start narrower overrides this."""
        return {key: flow.widest().value for key, flow in self.entity_flows.items()}

    # A person reviews and starts this connector's first sync; until it completes,
    # nothing runs on a schedule, from a webhook or from the outbound queue.
    reviewed_first_sync: bool = False

    # The document state key naming the platform record an imported invoice came from.
    imported_doc_marker: str | None = None

    # Order and customer numbers are only unique within one store, so imported
    # records are tied to the store they came from (see bind_connector_store).
    store_scoped_ids: bool = True

    async def same_store(self, ctx: ConnectorContext, records: list[dict]) -> bool:
        """True when the store in ctx still holds most of these imported
        document states. Connectors that cannot tell return False."""
        return False

    async def preview_products(self, ctx: ConnectorContext) -> list[RemoteProduct]:
        """Every product the platform holds, read only. Override if products sync."""
        return []

    async def preview_orders(self, ctx: ConnectorContext, orders_from: datetime) -> list[RemoteOrder]:
        """The platform's orders placed on or after ``orders_from``, read only.
        Override if orders sync."""
        return []

    async def before_outbound(self, ctx: ConnectorContext, now: datetime | None = None) -> None:
        """Called once before a run sends anything, e.g. to warn about expiring credentials."""
        return None

    @abstractmethod
    async def sync_products(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull products/variants from platform -> Celerp items."""
        ...

    @abstractmethod
    async def sync_orders(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull orders from platform -> Celerp documents."""
        ...

    async def sync_inventory(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull inventory levels from platform -> Celerp. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support inventory sync")

    async def sync_contacts(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull customers/vendors from platform -> Celerp CRM. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support contact sync")

    async def sync_contacts_out(self, ctx: ConnectorContext) -> SyncResult:
        """Send company contacts to an approved external recipient."""
        raise NotImplementedError(f"{self.name} does not support outbound contact sync")

    async def sync_products_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp items -> platform. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support outbound product sync")

    async def sync_invoices_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp invoices -> platform. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support outbound invoice sync")

    async def sync_inventory_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp inventory -> platform. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support outbound inventory sync")

    # -- Webhook lifecycle (override for e-commerce connectors) ----------------

    async def list_webhooks(self, ctx: ConnectorContext, webhook_url: str) -> list[dict]:
        """Celerp's hooks on the platform delivering to `webhook_url`. Override if supported."""
        return []

    async def create_webhook(self, ctx: ConnectorContext, webhook_url: str, topic: str,
                             secret: str | None) -> dict:
        raise NotImplementedError(f"{self.name} has no webhooks")

    async def activate_webhook(self, ctx: ConnectorContext, webhook_id) -> dict:
        raise NotImplementedError(f"{self.name} has no webhooks")

    async def get_webhook(self, ctx: ConnectorContext, webhook_id) -> dict | None:
        raise NotImplementedError(f"{self.name} has no webhooks")

    async def delete_webhook(self, ctx: ConnectorContext, webhook_id) -> None:
        raise NotImplementedError(f"{self.name} has no webhooks")

    async def deregister_webhooks(
        self, ctx: ConnectorContext, webhook_url: str, webhook_ids: list[str]
    ) -> None:
        """Remove the known webhooks and any others delivering to `webhook_url`.
        Override if supported."""
        pass

    def webhook_topics(self, directions: dict) -> list[str]:
        """The webhook topics these per-record directions listen to. Override if supported."""
        return []

    def validate_webhook(self, payload: bytes, signature: str, secret: str) -> bool:
        """Validate HMAC-SHA256 webhook signature. Works for Shopify and WooCommerce."""
        computed = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
        expected = base64.b64encode(computed).decode()
        return hmac.compare_digest(expected, signature)
