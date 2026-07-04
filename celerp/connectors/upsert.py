# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Shared upsert helpers for all connectors. Single source of truth.

Each helper opens its own DB session (via the target service) so connectors
never manage session lifecycle. Imports are deferred to keep connector import
light and to respect default-module load order.
"""
from __future__ import annotations


async def upsert_item(company_id: str, item) -> str:
    from celerp_inventory import services as items_svc
    return await items_svc.upsert_from_connector(company_id, item)


async def upsert_order_from_shopify(company_id: str, order: dict) -> str:
    from celerp_docs import doc_service as docs_svc
    return await docs_svc.upsert_order_from_shopify(company_id, order)


async def upsert_invoice_from_quickbooks(company_id: str, invoice: dict) -> str:
    from celerp_docs import doc_service as docs_svc
    return await docs_svc.upsert_invoice_from_quickbooks(company_id, invoice)


async def upsert_invoice_from_xero(company_id: str, invoice: dict) -> str:
    from celerp_docs import doc_service as docs_svc
    return await docs_svc.upsert_invoice_from_xero(company_id, invoice)


async def upsert_order_from_woocommerce(company_id: str, order: dict) -> str:
    from celerp_docs import doc_service as docs_svc
    return await docs_svc.upsert_order_from_woocommerce(company_id, order)


async def upsert_contact_from_shopify(company_id: str, customer: dict) -> str:
    from celerp_contacts import services as contacts_svc
    return await contacts_svc.upsert_contact_from_shopify(company_id, customer)


async def upsert_contact_from_quickbooks(company_id: str, customer: dict) -> str:
    from celerp_contacts import services as contacts_svc
    return await contacts_svc.upsert_contact_from_quickbooks(company_id, customer)


async def upsert_contact_from_xero(company_id: str, contact: dict) -> str:
    from celerp_contacts import services as contacts_svc
    return await contacts_svc.upsert_contact_from_xero(company_id, contact)


async def upsert_contact_from_woocommerce(company_id: str, customer: dict) -> str:
    from celerp_contacts import services as contacts_svc
    return await contacts_svc.upsert_contact_from_woocommerce(company_id, customer)
