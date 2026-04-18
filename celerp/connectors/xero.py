# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Xero connector.

OAuth model: CelERP relay service holds one registered Xero app.
Paying customers authorize via relay -> relay returns a short-lived
access_token injected into ConnectorContext.

Xero token model:
  - Access tokens expire after 30 minutes
  - Refresh tokens are long-lived (rotate on each refresh)
  - One token per (instance_id, tenant_id)

API version: Xero Accounting API v2 (https://api.xero.com/api.xro/2.0)
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

_API_BASE = "https://api.xero.com/api.xro/2.0"
_PAGE_SIZE = 100


def _headers(ctx: ConnectorContext) -> dict[str, str]:
    tenant_id = ctx.store_handle or (ctx.extra or {}).get("tenant_id", "")
    return {
        "Authorization": f"Bearer {ctx.access_token}",
        "Xero-Tenant-Id": tenant_id,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


class XeroConnector(ConnectorBase):
    name = "xero"
    display_name = "Xero"
    supported_entities = [SyncEntity.PRODUCTS, SyncEntity.ORDERS, SyncEntity.CONTACTS, SyncEntity.INVOICES]
    direction = SyncDirection.BIDIRECTIONAL
    category = ConnectorCategory.ACCOUNTING
    conflict_strategy = {
        SyncEntity.PRODUCTS: "newest",
        SyncEntity.ORDERS: "platform",
        SyncEntity.CONTACTS: "merge",
        SyncEntity.INVOICES: "platform",
    }

    # -- Internal helpers ------------------------------------------------------

    async def _paginate(self, ctx: ConnectorContext, path: str, key: str, since: datetime | None = None) -> list[dict[str, Any]]:
        """Fetch all pages for a resource using Xero's page-based pagination."""
        results: list[dict[str, Any]] = []
        page = 1
        headers = _headers(ctx)
        if since:
            headers["If-Modified-Since"] = since.strftime("%a, %d %b %Y %H:%M:%S GMT")
        async with RateLimitedClient() as client:
            while True:
                resp = await client.get(
                    f"{_API_BASE}{path}",
                    headers=headers,
                    params={"page": page, "pageSize": _PAGE_SIZE},
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get(key, [])
                results.extend(items)
                if len(items) < _PAGE_SIZE:
                    break
                page += 1
        return results

    # -- Products (Items in Xero) ----------------------------------------------

    async def sync_products(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """
        Pull Xero Items -> Celerp items.

        Mapping:
          Item.Code          -> item.sku
          Item.Name          -> item.name
          Item.Description   -> item.description
          Item.SalesDetails.UnitPrice -> item.sale_price
          Item.PurchaseDetails.UnitPrice -> item.cost_price
          Item.ItemID        -> idempotency_key
        """
        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        try:
            items = await self._paginate(ctx, "/Items", "Items", since=since)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"Xero API error: {exc}"]
            return result

        for xero_item in items:
            sku = (xero_item.get("Code") or "").strip()
            if not sku:
                result.skipped += 1
                continue

            idempotency_key = f"xero:item:{xero_item['ItemID']}"
            sale_price = (xero_item.get("SalesDetails") or {}).get("UnitPrice")
            cost_price = (xero_item.get("PurchaseDetails") or {}).get("UnitPrice")

            try:
                from celerp_inventory.routes import ItemCreate
                item = ItemCreate(
                    sku=sku,
                    name=xero_item.get("Name") or sku,
                    description=xero_item.get("Description") or "",
                    sell_by="piece",
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
            "xero.sync_products company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # -- Invoices / Orders -----------------------------------------------------

    async def sync_orders(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """
        Pull Xero Invoices (ACCREC type) -> Celerp documents.

        Mapping:
          Invoice.InvoiceNumber  -> doc.ref_id
          Invoice.Contact.Name   -> customer_name
          Invoice.LineItems      -> doc line_items
          Invoice.Status         -> doc.status (PAID->paid, AUTHORISED->final, DRAFT->draft)
          Invoice.InvoiceID      -> idempotency_key
        """
        result = SyncResult(entity=SyncEntity.ORDERS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        try:
            invoices = await self._paginate(ctx, "/Invoices", "Invoices", since=since)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"Xero API error: {exc}"]
            return result

        for inv in invoices:
            if inv.get("Type") != "ACCREC":
                result.skipped += 1
                continue
            try:
                created = await _upsert.upsert_invoice_from_xero(ctx.company_id, inv)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                errors.append(f"Invoice {inv.get('InvoiceNumber')}: {exc}")

        result.errors = errors or None
        log.info(
            "xero.sync_orders company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # -- Contacts --------------------------------------------------------------

    async def sync_contacts(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull Xero Contacts -> Celerp CRM contacts."""
        result = SyncResult(entity=SyncEntity.CONTACTS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        try:
            contacts = await self._paginate(ctx, "/Contacts", "Contacts", since=since)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"Xero API error: {exc}"]
            return result

        for contact in contacts:
            try:
                created = await _upsert.upsert_contact_from_xero(ctx.company_id, contact)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                errors.append(f"Contact {contact.get('ContactID')}: {exc}")

        result.errors = errors or None
        return result

    # -- Outbound: Invoices push -----------------------------------------------

    async def sync_invoices_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp invoices -> Xero (outbound)."""
        result = SyncResult(entity=SyncEntity.INVOICES, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []

        try:
            invoices = await _upsert.list_unsynced_invoices(ctx.company_id, platform="xero")
        except Exception as exc:
            result.errors = [f"Failed to load invoices: {exc}"]
            return result

        async with RateLimitedClient() as client:
            for inv in invoices:
                try:
                    line_items = [
                        {
                            "Description": line.get("description", ""),
                            "Quantity": float(line.get("quantity", 1)),
                            "UnitAmount": float(line.get("unit_price", 0)),
                            "LineAmount": float(line.get("total", 0)),
                        }
                        for line in (inv.get("line_items") or [])
                    ]
                    payload = {
                        "Invoices": [{
                            "Type": "ACCREC",
                            "InvoiceNumber": inv.get("ref_id"),
                            "Contact": {"ContactID": inv.get("customer_external_id") or inv.get("customer_name", "")},
                            "LineItems": line_items,
                            "Status": "AUTHORISED",
                        }]
                    }
                    resp = await client.put(
                        f"{_API_BASE}/Invoices",
                        headers=_headers(ctx),
                        json=payload,
                    )
                    resp.raise_for_status()
                    result.created += 1
                except Exception as exc:
                    errors.append(f"Invoice {inv.get('ref_id')}: {exc}")

        result.errors = errors or None
        log.info(
            "xero.sync_invoices_out company=%s created=%d errors=%d",
            ctx.company_id, result.created, len(errors),
        )
        return result
