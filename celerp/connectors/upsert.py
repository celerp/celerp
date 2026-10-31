# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Shared upsert helpers for all connectors. Single source of truth.

Each helper opens its own DB session (via the target service) so connectors
never manage session lifecycle. Imports are deferred to keep connector import
light and to respect default-module load order.
"""
from __future__ import annotations


async def import_store_product(
    ctx, connector, *, key: str, label: str, result, pending: dict, first_sync=None,
    errors: list[str], **fields,
) -> str | None:
    """Import one store product for a product pull and return the item it landed on,
    or None when nothing more should happen to it (no SKU, refused, disabled, or left
    alone by a first sync that creates nothing).

    A product with no SKU is not imported and waits on a person (``pending``, keyed
    by ``key``). A linked product changed in both systems keeps Celerp's values and
    waits on a person with both. ``first_sync`` carries the first sync's choices and
    collects what it did."""
    from celerp.connectors.base import ExternalImportRecord
    from celerp_inventory.services import upsert_external_product
    from ui.i18n import t

    sku = fields.pop("sku")
    if not sku:
        pending[key] = {"id": key, "label": label, "kind": "no_sku",
                        "reason": t("connectors.attention_no_sku", service=connector.display_name)}
        result.skipped += 1
        return None
    record = ExternalImportRecord(first_sync=None if first_sync is None else first_sync.record.first_sync)
    options = {} if first_sync is None else {
        "link_only": first_sync.match == "keep",
        "create": first_sync.create,
        "opening_operation": first_sync.batch_id,
    }
    try:
        outcome, entity_id = await upsert_external_product(
            ctx.company_id, platform=connector.name, sku=sku, record=record, **options, **fields,
        )
    except Exception as exc:
        errors.append(f"SKU {sku}: {exc}")
        return None
    result.record(outcome)
    if first_sync is not None:
        first_sync.record.created += record.created
        first_sync.record.linked += record.linked
    for conflict in record.conflicts:
        pending[key] = {
            "id": key, "label": label, "kind": "conflict",
            "entity_id": conflict["entity_id"], "fields": conflict["fields"],
            "reason": t("connectors.attention_conflict", service=connector.display_name,
                        fields=", ".join(t(f"connectors.conflict_field_{f}")
                                         for f in conflict["fields"])),
        }
    return None if outcome in ("disabled", "skipped") else entity_id


def carried_attention(attention: list[dict] | None, pending: dict, processed: set[str]) -> list[dict]:
    """This run's entries, then the previous run's for records this run did not read."""
    return list(pending.values()) + [
        entry for entry in (attention or [])
        if entry.get("id") and str(entry["id"]) not in processed
    ]


async def upsert_item(company_id: str, item, first_sync=None) -> str:
    """One accounting item, recorded on a first sync when it creates one."""
    from celerp_inventory import services as items_svc
    if first_sync is None:
        return await items_svc.upsert_from_connector(company_id, item)
    return await items_svc.upsert_from_connector(
        company_id, item, record=first_sync.record, opening_operation=first_sync.batch_id)


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


def woocommerce_reconciliation_signature(order: dict) -> str:
    from celerp_docs import doc_service as docs_svc
    return docs_svc.woocommerce_reconciliation_signature(order)


async def list_imported_woocommerce_order_ids(company_id: str) -> list[str]:
    from celerp_docs import doc_service as docs_svc
    return await docs_svc.list_imported_woocommerce_order_ids(company_id)


async def hold_missing_woocommerce_order(company_id: str, order_id: str) -> dict | None:
    from celerp_docs import doc_service as docs_svc
    return await docs_svc.hold_missing_woocommerce_order(company_id, order_id)


async def upsert_contact_from_shopify(company_id: str, customer: dict) -> str:
    from celerp_contacts import services as contacts_svc
    return await contacts_svc.upsert_contact_from_shopify(company_id, customer)


async def upsert_contact_from_quickbooks(company_id: str, customer: dict) -> str:
    from celerp_contacts import services as contacts_svc
    return await contacts_svc.upsert_contact_from_quickbooks(company_id, customer)


async def upsert_contact_from_xero(company_id: str, contact: dict) -> str:
    from celerp_contacts import services as contacts_svc
    return await contacts_svc.upsert_contact_from_xero(company_id, contact)


# -- Outbound: read the Celerp-side list of records to push ---------------------

async def list_items_with_external_id(company_id: str, platform: str) -> list:
    from celerp_inventory import services as items_svc
    return await items_svc.list_items_with_external_id(company_id, platform=platform)


async def list_item_for_external_identity(
    company_id: str, platform: str, product_id: str, variation_id: str | None
) -> list:
    from celerp_inventory import services as items_svc
    return await items_svc.list_item_for_external_identity(
        company_id, platform, product_id, variation_id
    )


async def list_items_modified_since_last_sync(company_id: str, platform: str) -> list:
    from celerp_inventory import services as items_svc
    return await items_svc.list_items_modified_since_last_sync(company_id, platform=platform)


async def list_unsynced_invoices(company_id: str, platform: str) -> list:
    from celerp_docs import doc_service as docs_svc
    return await docs_svc.list_unsynced_invoices(company_id, platform=platform)


async def invoice_for_push(company_id: str, entity_id: str, platform: str) -> dict | None:
    from celerp_docs import doc_service as docs_svc
    return await docs_svc.invoice_for_push(company_id, entity_id, platform)


async def mark_doc_pushed(company_id: str, entity_id: str, platform: str, external_id: str, entity: str = "invoice") -> None:
    from celerp_docs import doc_service as docs_svc
    return await docs_svc.mark_doc_pushed(company_id, entity_id, platform, external_id, entity=entity)
