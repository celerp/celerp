# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
QuickBooks Online connector.

OAuth model: CelERP relay service holds one registered Intuit app.
Paying customers authorize via relay -> relay returns a short-lived
access_token injected into ConnectorContext.

QuickBooks token model:
  - Access tokens expire after 60 minutes
  - Refresh tokens expire after 100 days
  - store_handle = realmId (QB company ID)

API: QuickBooks Accounting API v3
Reference: https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from celerp.connectors.http import RateLimitedClient
from celerp.connectors.util import money
from celerp.connectors.base import (
    ConnectorBase,
    ConnectorCategory,
    ConnectorContext,
    EntityFlow,
    FirstSync,
    RemoteOrder,
    RemoteProduct,
    SyncDirection,
    SyncEntity,
    SyncResult,
    contact_matches,
    store_holds_records,
)
import celerp.connectors.upsert as _upsert

log = logging.getLogger(__name__)

_API_BASE = "https://quickbooks.api.intuit.com/v3/company"
_PAGE_SIZE = 100


def _where(*clauses: str | None) -> str:
    """A QB query WHERE clause joining the given conditions, or nothing."""
    kept = [c for c in clauses if c]
    return f" WHERE {' AND '.join(kept)}" if kept else ""


def _since(since: datetime | None) -> str | None:
    """The condition of an incremental pull: changed after ``since``."""
    if since is None:
        return None
    if not isinstance(since, datetime):
        raise TypeError(f"Expected datetime, got {type(since).__name__}")
    return f"MetaData.LastUpdatedTime > '{since.isoformat()}'"


def _from_date(orders_from: datetime | None) -> str | None:
    """The condition keeping invoices dated on or after the connection's earliest order date."""
    return f"TxnDate >= '{orders_from.date().isoformat()}'" if orders_from else None


def _qb_sku(item: dict) -> str:
    """An item's SKU: its Sku, or its Name when it has none."""
    return (item.get("Sku") or item.get("Name") or "").strip()


_QB_ITEM_TYPES = ("Service", "Inventory", "NonInventory")


def _qb_when(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _base_url(ctx: ConnectorContext) -> str:
    realm_id = ctx.store_handle or ""
    if not realm_id:
        raise ValueError("ConnectorContext.store_handle (realmId) is required for QuickBooks")
    # realmId is an Intuit-issued numeric company id; reject anything else so it
    # can never be used to redirect the request away from the Intuit API host.
    if not realm_id.isdigit():
        raise ValueError("QuickBooks realmId must be numeric")
    return f"{_API_BASE}/{realm_id}"


def _headers(ctx: ConnectorContext) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {ctx.access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def _query(ctx: ConnectorContext, sql: str) -> list[dict[str, Any]]:
    """Execute a QuickBooks query and return all rows."""
    base = _base_url(ctx)
    results: list[dict[str, Any]] = []
    start = 1
    async with RateLimitedClient() as client:
        while True:
            paginated = f"{sql} STARTPOSITION {start} MAXRESULTS {_PAGE_SIZE}"
            resp = await client.get(
                f"{base}/query",
                headers=_headers(ctx),
                params={"query": paginated, "minorversion": "65"},
            )
            resp.raise_for_status()
            data = resp.json()
            query_resp = data.get("QueryResponse", {})
            rows = []
            for key, val in query_resp.items():
                if isinstance(val, list):
                    rows = val
                    break
            results.extend(rows)
            if len(rows) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE
    return results


class QuickBooksConnector(ConnectorBase):
    name = "quickbooks"
    display_name = "QuickBooks"
    imported_doc_marker = "quickbooks_invoice_id"
    entity_flows = {
        "products": EntityFlow(inbound=("products",)),
        "invoices": EntityFlow(inbound=("orders",), outbound=("invoices_out",)),
        "contacts": EntityFlow(inbound=("contacts",)),
    }
    category = ConnectorCategory.ACCOUNTING
    conflict_strategy = {
        SyncEntity.PRODUCTS: "newest",
        SyncEntity.ORDERS: "platform",
        SyncEntity.CONTACTS: "merge",
    }

    async def same_store(self, ctx: ConnectorContext, records: list[dict]) -> bool:
        """Invoice, customer and item ids repeat across companies, so a sampled
        invoice counts only when its number and total match what was imported,
        a sampled customer only when its contact details do, and a sampled item
        only when its SKU or name does."""
        def customer_id(record: dict) -> str:
            return str((record.get("attributes") or {}).get("quickbooks_id") or "")

        def item_id(record: dict) -> str:
            prefix = "quickbooks:item:"
            key = str(record.get("idempotency_key") or "")
            return key[len(prefix):] if key.startswith(prefix) else ""

        async def fetch(entity: str, ids: set[str]) -> dict[str, dict]:
            ids = {i for i in ids if i.isdigit()}
            if not ids:
                return {}
            quoted = ",".join(f"'{i}'" for i in sorted(ids))
            return {str(r.get("Id")): r for r in await _query(ctx, f"SELECT * FROM {entity} WHERE Id IN ({quoted})")}

        invoices = await fetch("Invoice", {str(r.get("quickbooks_invoice_id") or "") for r in records})
        customers = await fetch("Customer", {customer_id(r) for r in records})
        items = await fetch("Item", {item_id(r) for r in records})
        matches = []
        for record in records:
            invoice = invoices.get(str(record.get("quickbooks_invoice_id") or ""))
            customer = customers.get(customer_id(record))
            item = items.get(item_id(record))
            if invoice is not None:
                ref_id = str(invoice.get("DocNumber") or f"quickbooks-{invoice['Id']}")
                matches.append(
                    ref_id == record.get("ref_id")
                    and abs((money(invoice.get("TotalAmt")) or 0.0) - float(record.get("total") or 0)) < 0.005
                )
            elif customer is not None:
                matches.append(contact_matches(record, {
                    "name": customer.get("DisplayName") or " ".join(
                        p for p in (customer.get("GivenName", ""), customer.get("FamilyName", "")) if p
                    ).strip(),
                    "email": (customer.get("PrimaryEmailAddr") or {}).get("Address"),
                    "phone": (customer.get("PrimaryPhone") or {}).get("FreeFormNumber"),
                }))
            elif item is not None:
                matches.append(
                    bool(item.get("Name")) and item.get("Name") == record.get("name")
                    or bool(item.get("Sku")) and item.get("Sku") == record.get("sku")
                )
            else:
                matches.append(False)
        return store_holds_records(matches)

    # -- Products (Items in QB) ------------------------------------------------

    async def sync_products(
        self, ctx: ConnectorContext, since: datetime | None = None,
        first_sync: FirstSync | None = None,
    ) -> SyncResult:
        """
        Pull QuickBooks Items -> Celerp items.

        Mapping:
          Item.Sku / Item.Name  -> item.sku (Name used if no Sku)
          Item.Name             -> item.name
          Item.Description      -> item.description
          Item.UnitPrice        -> item.sale_price
          Item.PurchaseCost     -> item.cost_price
          Item.Id               -> idempotency_key
        """
        result = SyncResult(entity=SyncEntity.PRODUCTS)
        errors: list[str] = []

        try:
            sql = f"SELECT * FROM Item{_where('Active = true', _since(since))}"
            items = await _query(ctx, sql)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"QuickBooks API error: {exc}"]
            return result

        for qb_item in items:
            item_type = qb_item.get("Type", "")
            if item_type not in _QB_ITEM_TYPES:
                result.skipped += 1
                continue

            sku = _qb_sku(qb_item)
            if not sku:
                result.skipped += 1
                continue

            idempotency_key = f"quickbooks:item:{qb_item['Id']}"
            sell_by = "service" if item_type == "Service" else "piece"

            try:
                from celerp_inventory.routes import ItemCreate
                item = ItemCreate(
                    sku=sku,
                    name=qb_item.get("Name") or sku,
                    description=qb_item.get("Description") or "",
                    sell_by=sell_by,
                    sale_price=money(qb_item.get("UnitPrice")),
                    cost_price=money(qb_item.get("PurchaseCost")),
                    idempotency_key=idempotency_key,
                )
                result.record(await _upsert.upsert_item(ctx.company_id, item, first_sync))
            except Exception as exc:
                errors.append(f"SKU {sku}: {exc}")

        result.errors = errors or None
        log.info(
            "quickbooks.sync_products company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    async def preview_products(self, ctx: ConnectorContext) -> list[RemoteProduct]:
        items = await _query(ctx, f"SELECT * FROM Item{_where('Active = true')}")
        return [RemoteProduct(
            key=str(i["Id"]), product_id=str(i["Id"]), variation_id=None, sku=_qb_sku(i),
            name=i.get("Name") or "", description=i.get("Description"),
            price=money(i.get("UnitPrice")),
            modified=_qb_when((i.get("MetaData") or {}).get("LastUpdatedTime")),
        ) for i in items if i.get("Type", "") in _QB_ITEM_TYPES]

    async def preview_orders(self, ctx: ConnectorContext, orders_from: datetime) -> list[RemoteOrder]:
        invoices = await _query(ctx, f"SELECT * FROM Invoice{_where(_from_date(orders_from))}")
        return [RemoteOrder(
            id=str(inv["Id"]), number=str(inv.get("DocNumber") or inv["Id"]),
            status="paid" if money(inv.get("Balance")) == 0 else "open",
            created=_qb_when(inv.get("TxnDate")), total=money(inv.get("TotalAmt")),
            currency=(inv.get("CurrencyRef") or {}).get("value"), posts=False,
        ) for inv in invoices]

    # -- Invoices / Orders -----------------------------------------------------

    async def sync_orders(
        self, ctx: ConnectorContext, since: datetime | None = None,
        orders_from: datetime | None = None,
    ) -> SyncResult:
        """
        Pull QuickBooks Invoices -> Celerp documents, none dated before ``orders_from``.

        Mapping:
          Invoice.DocNumber       -> doc.ref_id
          Invoice.CustomerRef     -> customer lookup/create
          Invoice.Line            -> doc line_items
          Invoice.Balance         -> used to determine status
          Invoice.Id              -> idempotency_key
        """
        result = SyncResult(entity=SyncEntity.ORDERS)
        errors: list[str] = []

        try:
            sql = f"SELECT * FROM Invoice{_where(_since(since), _from_date(orders_from))}"
            invoices = await _query(ctx, sql)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"QuickBooks API error: {exc}"]
            return result

        for inv in invoices:
            try:
                result.record(await _upsert.upsert_invoice_from_quickbooks(ctx.company_id, inv))
            except Exception as exc:
                errors.append(f"Invoice {inv.get('DocNumber')}: {exc}")

        result.errors = errors or None
        log.info(
            "quickbooks.sync_orders company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # -- Contacts --------------------------------------------------------------

    async def sync_contacts(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull QuickBooks Customers -> Celerp CRM contacts."""
        result = SyncResult(entity=SyncEntity.CONTACTS)
        errors: list[str] = []

        try:
            sql = f"SELECT * FROM Customer{_where('Active = true', _since(since))}"
            customers = await _query(ctx, sql)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"QuickBooks API error: {exc}"]
            return result

        for customer in customers:
            try:
                result.record(await _upsert.upsert_contact_from_quickbooks(ctx.company_id, customer))
            except Exception as exc:
                errors.append(f"Customer {customer.get('Id')}: {exc}")

        result.errors = errors or None
        return result

    # -- Outbound: Invoices push -----------------------------------------------

    async def sync_invoices_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp invoices -> QuickBooks (outbound)."""
        result = SyncResult(entity=SyncEntity.INVOICES, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []
        realm_id = (ctx.extra or {}).get("realm_id") or ctx.store_handle
        if not realm_id or not str(realm_id).isdigit():
            result.errors = ["A numeric realm_id is required for QuickBooks outbound invoice sync"]
            return result

        try:
            invoices = await _upsert.list_unsynced_invoices(ctx.company_id, platform="quickbooks")
        except Exception as exc:
            result.errors = [f"Failed to load invoices: {exc}"]
            return result

        base = f"{_API_BASE}/{realm_id}"
        async with RateLimitedClient() as client:
            for inv in invoices:
                try:
                    line_items = [
                        {
                            "DetailType": "SalesItemLineDetail",
                            "Amount": float(line.get("total", 0)),
                            "Description": line.get("description", ""),
                            "SalesItemLineDetail": {
                                "Qty": float(line.get("quantity", 1)),
                                "UnitPrice": float(line.get("unit_price", 0)),
                            },
                        }
                        for line in (inv.get("line_items") or [])
                    ]
                    payload = {
                        "CustomerRef": {"value": inv.get("customer_external_id") or inv.get("customer_name", "")},
                        "Line": line_items,
                        "DocNumber": inv.get("ref_id"),
                    }
                    resp = await client.post(
                        f"{base}/invoice",
                        headers=_headers(ctx),
                        params={"minorversion": "65"},
                        json=payload,
                    )
                    resp.raise_for_status()
                    # Write-back: stamp the returned QB invoice id so the next run's
                    # list_unsynced_invoices skips this doc — never created in QB twice.
                    qb_id = ((resp.json() or {}).get("Invoice") or {}).get("Id")
                    if qb_id and inv.get("entity_id"):
                        await _upsert.mark_doc_pushed(ctx.company_id, inv["entity_id"], "quickbooks", qb_id)
                    result.created += 1
                except Exception as exc:
                    errors.append(f"Invoice {inv.get('ref_id')}: {exc}")

        result.errors = errors or None
        log.info(
            "quickbooks.sync_invoices_out company=%s created=%d errors=%d",
            ctx.company_id, result.created, len(errors),
        )
        return result
