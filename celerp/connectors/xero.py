# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Xero connector.

OAuth model: CelERP relay service holds one registered Xero app.
Paying customers authorize via relay → relay returns a short-lived
access_token injected into ConnectorContext.

Xero token model:
  - Access tokens expire after 30 minutes
  - Refresh tokens are long-lived (rotate on each refresh)
  - One token per (instance_id, tenant_id)

API version: Xero Accounting API v2 (https://api.xero.com/api.xro/2.0)
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from celerp.connectors.base import (
    ConnectorBase,
    ConnectorContext,
    SyncDirection,
    SyncEntity,
    SyncResult,
)

log = logging.getLogger(__name__)

_API_BASE = "https://api.xero.com/api.xro/2.0"
_PAGE_SIZE = 100


def _headers(ctx: ConnectorContext) -> dict[str, str]:
    # store_handle holds the Xero tenant_id (required for all API calls)
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

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _paginate(self, ctx: ConnectorContext, path: str, key: str) -> list[dict[str, Any]]:
        """Fetch all pages for a resource using Xero's page-based pagination."""
        results: list[dict[str, Any]] = []
        page = 1
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                resp = await client.get(
                    f"{_API_BASE}{path}",
                    headers=_headers(ctx),
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

    # ── Products (Items in Xero) ──────────────────────────────────────────────

    async def sync_products(self, ctx: ConnectorContext) -> SyncResult:
        """
        Pull Xero Items → Celerp items.

        Mapping:
          Item.Code          → item.sku
          Item.Name          → item.name
          Item.Description   → item.description
          Item.SalesDetails.UnitPrice → item.sale_price
          Item.PurchaseDetails.UnitPrice → item.cost_price
          Item.ItemID        → idempotency_key
        """
        result = SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        try:
            items = await self._paginate(ctx, "/Items", "Items")
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
                created = await _upsert_item(ctx.company_id, item)
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

    # ── Invoices / Orders ────────────────────────────────────────────────────

    async def sync_orders(self, ctx: ConnectorContext) -> SyncResult:
        """
        Pull Xero Invoices (ACCREC type) → Celerp documents.

        Mapping:
          Invoice.InvoiceNumber  → doc.ref_id
          Invoice.Contact.Name   → customer_name
          Invoice.LineItems      → doc line_items
          Invoice.Status         → doc.status (PAID→paid, AUTHORISED→final, DRAFT→draft)
          Invoice.InvoiceID      → idempotency_key
        """
        result = SyncResult(entity=SyncEntity.ORDERS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        try:
            invoices = await self._paginate(ctx, "/Invoices", "Invoices")
        except httpx.HTTPStatusError as exc:
            result.errors = [f"Xero API error: {exc}"]
            return result

        for inv in invoices:
            # Only sync sales invoices
            if inv.get("Type") != "ACCREC":
                result.skipped += 1
                continue
            try:
                created = await _upsert_invoice(ctx.company_id, inv)
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

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def sync_contacts(self, ctx: ConnectorContext) -> SyncResult:
        """Pull Xero Contacts → Celerp CRM contacts."""
        result = SyncResult(entity=SyncEntity.CONTACTS, direction=SyncDirection.INBOUND)
        errors: list[str] = []

        try:
            contacts = await self._paginate(ctx, "/Contacts", "Contacts")
        except httpx.HTTPStatusError as exc:
            result.errors = [f"Xero API error: {exc}"]
            return result

        for contact in contacts:
            try:
                created = await _upsert_contact(ctx.company_id, contact)
                if created:
                    result.created += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                errors.append(f"Contact {contact.get('ContactID')}: {exc}")

        result.errors = errors or None
        return result

    # ── Invoices (outbound push) ──────────────────────────────────────────────

    async def sync_invoices(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp invoices → Xero (outbound). Not yet implemented."""
        raise NotImplementedError("Xero outbound invoice push: coming soon")


# ── DB upsert helpers ─────────────────────────────────────────────────────────

async def _upsert_item(company_id: str, item) -> bool:
    from celerp_inventory import services as items_svc
    return await items_svc.upsert_from_connector(company_id, item)


async def _upsert_invoice(company_id: str, invoice: dict) -> bool:
    from celerp.services import docs as docs_svc
    return await docs_svc.upsert_invoice_from_xero(company_id, invoice)


async def _upsert_contact(company_id: str, contact: dict) -> bool:
    from celerp_contacts import services as contacts_svc
    return await contacts_svc.upsert_contact_from_xero(company_id, contact)
