# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
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
from celerp.connectors.base import (
    ConnectorBase,
    ConnectorCategory,
    ConnectorContext,
    SyncDirection,
    SyncEntity,
    SyncResult,
)
import celerp.connectors.upsert as _upsert

log = logging.getLogger(__name__)

_API_BASE = "https://quickbooks.api.intuit.com/v3/company"
_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com/v3/company"
_PAGE_SIZE = 100


def _base_url(ctx: ConnectorContext) -> str:
    realm_id = ctx.store_handle or ""
    if not realm_id:
        raise ValueError("ConnectorContext.store_handle (realmId) is required for QuickBooks")
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
    supported_entities = [SyncEntity.PRODUCTS, SyncEntity.ORDERS, SyncEntity.CONTACTS, SyncEntity.INVOICES]
    direction = SyncDirection.BIDIRECTIONAL
    category = ConnectorCategory.ACCOUNTING
    conflict_strategy = {
        SyncEntity.PRODUCTS: "newest",
        SyncEntity.ORDERS: "platform",
        SyncEntity.CONTACTS: "merge",
        SyncEntity.INVOICES: "platform",
    }

    # -- Products (Items in QB) ------------------------------------------------

    async def sync_products(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
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
        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        try:
            sql = (
                f"SELECT * FROM Item WHERE Active = true AND MetaData.LastUpdatedTime > '{since.isoformat()}'"
                if since else "SELECT * FROM Item WHERE Active = true"
            )
            items = await _query(ctx, sql)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"QuickBooks API error: {exc}"]
            return result

        for qb_item in items:
            item_type = qb_item.get("Type", "")
            if item_type not in ("Service", "Inventory", "NonInventory"):
                result.skipped += 1
                continue

            sku = (qb_item.get("Sku") or qb_item.get("Name") or "").strip()
            if not sku:
                result.skipped += 1
                continue

            idempotency_key = f"quickbooks:item:{qb_item['Id']}"
            sale_price = qb_item.get("UnitPrice")
            cost_price = qb_item.get("PurchaseCost")
            sell_by = "service" if item_type == "Service" else "piece"

            try:
                from celerp_inventory.routes import ItemCreate
                item = ItemCreate(
                    sku=sku,
                    name=qb_item.get("Name") or sku,
                    description=qb_item.get("Description") or "",
                    sell_by=sell_by,
                    sale_price=float(sale_price) if sale_price else None,
                    cost_price=float(cost_price) if cost_price else None,
                    idempotency_key=idempotency_key,
                )
                created = await _upsert.upsert_item(ctx.company_id, item)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                errors.append(f"SKU {sku}: {exc}")

        result.errors = errors or None
        log.info(
            "quickbooks.sync_products company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # -- Invoices / Orders -----------------------------------------------------

    async def sync_orders(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """
        Pull QuickBooks Invoices -> Celerp documents.

        Mapping:
          Invoice.DocNumber       -> doc.ref_id
          Invoice.CustomerRef     -> customer lookup/create
          Invoice.Line            -> doc line_items
          Invoice.Balance         -> used to determine status
          Invoice.Id              -> idempotency_key
        """
        result = SyncResult(entity=SyncEntity.ORDERS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        try:
            sql = (
                f"SELECT * FROM Invoice WHERE MetaData.LastUpdatedTime > '{since.isoformat()}'"
                if since else "SELECT * FROM Invoice"
            )
            invoices = await _query(ctx, sql)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"QuickBooks API error: {exc}"]
            return result

        for inv in invoices:
            try:
                created = await _upsert.upsert_invoice_from_quickbooks(ctx.company_id, inv)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
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
        result = SyncResult(entity=SyncEntity.CONTACTS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        try:
            sql = (
                f"SELECT * FROM Customer WHERE Active = true AND MetaData.LastUpdatedTime > '{since.isoformat()}'"
                if since else "SELECT * FROM Customer WHERE Active = true"
            )
            customers = await _query(ctx, sql)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"QuickBooks API error: {exc}"]
            return result

        for customer in customers:
            try:
                created = await _upsert.upsert_contact_from_quickbooks(ctx.company_id, customer)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
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
        if not realm_id:
            result.errors = ["realm_id is required for QuickBooks outbound invoice sync"]
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
                    result.created += 1
                except Exception as exc:
                    errors.append(f"Invoice {inv.get('ref_id')}: {exc}")

        result.errors = errors or None
        log.info(
            "quickbooks.sync_invoices_out company=%s created=%d errors=%d",
            ctx.company_id, result.created, len(errors),
        )
        return result
