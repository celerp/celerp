# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Shared upsert helpers for all connectors. Single source of truth."""
from __future__ import annotations


async def upsert_item(company_id: str, item) -> bool:
    from celerp_inventory import services as items_svc
    return await items_svc.upsert_from_connector(company_id, item)


async def upsert_order_from_shopify(company_id: str, order: dict) -> bool:
    from celerp.services import docs as docs_svc
    return await docs_svc.upsert_order_from_shopify(company_id, order)


async def upsert_invoice_from_quickbooks(company_id: str, invoice: dict) -> bool:
    from celerp.services import docs as docs_svc
    return await docs_svc.upsert_invoice_from_quickbooks(company_id, invoice)


async def upsert_invoice_from_xero(company_id: str, invoice: dict) -> bool:
    from celerp.services import docs as docs_svc
    return await docs_svc.upsert_invoice_from_xero(company_id, invoice)


async def upsert_contact_from_shopify(company_id: str, customer: dict) -> bool:
    from celerp_sales_funnel import services as crm_svc
    return await crm_svc.upsert_contact_from_shopify(company_id, customer)


async def upsert_contact_from_quickbooks(company_id: str, customer: dict) -> bool:
    from celerp_contacts import services as contacts_svc
    return await contacts_svc.upsert_contact_from_quickbooks(company_id, customer)


async def upsert_contact_from_xero(company_id: str, contact: dict) -> bool:
    from celerp_contacts import services as contacts_svc
    return await contacts_svc.upsert_contact_from_xero(company_id, contact)
