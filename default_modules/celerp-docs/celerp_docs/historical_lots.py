# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""The product of each lot a data migration made for goods an invoice delivered before its
books came to Celerp (``routes.record_historical_delivery``).

Such a lot is taken from its invoice line's item, so it is stock of the product that item
records (``celerp_inventory.services.product_of_stock``); the link is written to the ledger
so every rebuild keeps it. Earlier releases made these lots with no link, so the start of
each release links any still missing one. A lot whose line's item records no product is left
unlinked, never given a guessed one, and the company is told once which invoice and SKUs.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

NO_PRODUCT_TITLE = "Delivered goods with no product"


async def link_historical_lots(session: AsyncSession, company_id, lots: list[str] | None = None) -> dict:
    """Link the company's migrated delivery lots (``lots``, or all of them) that have no
    product to the product their line's item records, and tell the company of those whose
    item records none. Idempotent. Caller commits. Returns {"linked": n, "unlinked": n}."""
    from celerp.events.engine import emit_event
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.notifications.service import notify_once
    from celerp_inventory.services import product_of_stock

    def migrated(event_type: str, ids=None):
        query = select(LedgerEntry).where(LedgerEntry.company_id == company_id, LedgerEntry.source == "migration",
                                          LedgerEntry.event_type == event_type)
        return query if ids is None else query.where(LedgerEntry.entity_id.in_(ids))

    # lot -> the invoice it was delivered on, then -> the line's item it was taken from
    sold_on = {e.entity_id: (e.data or {}).get("source_doc_id")
               for e in (await session.execute(migrated("item.fulfilled", lots))).scalars()}
    made = {e.entity_id: e.metadata_["parent_id"]
            for e in (await session.execute(migrated("item.created", list(sold_on)))).scalars()
            if (e.metadata_ or {}).get("parent_id")} if sold_on else {}
    if not made:
        return {"linked": 0, "unlinked": 0}
    rows = {r.entity_id: r.state or {} for r in (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_id.in_(list(made) + list(set(made.values())) + list(set(sold_on.values()))),
    ))).scalars()}
    linked, unlinked = 0, {}
    for lot, item in sorted(made.items()):
        state = rows.get(lot)
        if state is None or state.get("catalog_item_id") or state.get("parent_item_id"):
            continue
        product = product_of_stock(item, rows[item]) if item in rows else None
        if product is None:
            unlinked.setdefault(sold_on[lot], set()).add(str(state.get("sku") or lot))
            continue
        await emit_event(
            session, company_id=company_id, entity_id=lot, entity_type="item", event_type="item.updated",
            data={"fields_changed": {"catalog_item_id": {"old": None, "new": product}}},
            actor_id=None, location_id=None, source="migration",
            idempotency_key=f"historical-lot-product:{lot}", metadata_={"parent_id": item},
        )
        linked += 1
    for doc, skus in sorted(unlinked.items()):
        doc_state = rows.get(doc) or {}
        number = doc_state.get("ref_id") or doc_state.get("doc_number") or doc
        await notify_once(session, company_id, "system", NO_PRODUCT_TITLE, (
            f"Invoice {number} came over with goods delivered before the move whose item names no "
            f"product: {', '.join(sorted(skus))}. Demand Planning counts what those lines still owe "
            "under each item's own SKU, not under a product."))
    return {"linked": linked, "unlinked": sum(len(s) for s in unlinked.values())}


async def link_historical_lots_hook(*, session: AsyncSession) -> None:
    """on_modules_ready: link every company's migrated delivery lots that have no product. A
    company staged for a migration is left alone until it finishes."""
    from celerp.models.company import Company
    from celerp.services.migrations import is_company_migration_staged

    for company_id in (await session.execute(select(Company.id).order_by(Company.id))).scalars().all():
        if await is_company_migration_staged(session, company_id):
            continue
        result = await link_historical_lots(session, company_id)
        if result["linked"]:
            log.info("Linked %d migrated delivery lot(s) of company %s to their product", result["linked"], company_id)
