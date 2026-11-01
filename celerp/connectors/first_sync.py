# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The first sync's preview: what starting it would do, read from both sides without
writing to either.

A connector reads its platform's products and orders (ConnectorBase.preview_products,
preview_orders); this module lays them against the company's catalog the way the first
sync will: products that match by SKU and how their values differ, products new to
Celerp, products with no SKU, Celerp products the store does not have, the store stock the
stock push would change, and the orders from the chosen date with what importing them
does to the books.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, datetime, time

from celerp.connectors.base import (
    ConnectorBase,
    ConnectorContext,
    SyncDirection,
    entity_allowed,
    validate_directions,
)
from celerp.modules.boundary import needs_modules
from celerp.services.field_fingerprints import PRODUCT_FIELDS, fingerprint
from ui.i18n import t


@dataclass
class FirstSyncPreview:
    directions: dict
    orders_from: datetime
    # Each list is newest first by when the platform (or Celerp) last changed it.
    matches: list[dict] = field(default_factory=list)     # store products Celerp has by SKU
    new: list[dict] = field(default_factory=list)         # store products new to Celerp
    no_sku: list[dict] = field(default_factory=list)      # store products that cannot import
    problems: list[dict] = field(default_factory=list)    # store products that cannot link
    to_publish: list[dict] = field(default_factory=list)  # Celerp products the store lacks
    orders: list[dict] = field(default_factory=list)
    posting_orders: int = 0
    order_effect: str = ""
    stock_changes: int = 0  # matches whose store stock becomes Celerp's
    # The receipt Start must hand back: a digest of every row above, with the connection
    # and store it was read from (_version).
    version: str = ""

    @property
    def reviewed(self) -> dict[str, str | None]:
        """Each store product the person reviewed: the item it links to, None when the
        first sync creates it."""
        return {**{r["key"]: None for r in self.new}, **{r["key"]: r["entity_id"] for r in self.matches}}


def _newest_first(rows: list[dict], key: str = "modified") -> list[dict]:
    return sorted(rows, key=lambda r: (r.get(key) is not None, r.get(key) or 0), reverse=True)


def _differences(state: dict, remote) -> dict:
    remote_values = {"sku": remote.sku, "name": remote.name, "description": remote.description,
                     "sale_price": remote.price}
    return {
        key: (state.get(key), remote_values[key]) for key in PRODUCT_FIELDS
        if remote_values[key] is not None and fingerprint(state.get(key)) != fingerprint(remote_values[key])
    }


def _version(connector: ConnectorBase, ctx: ConnectorContext, config_id: int, preview: FirstSyncPreview) -> str:
    """The preview's receipt: the same choices, connection, store and rows give the same
    digest, and any change to one of them gives another. It reads every field of every row
    of every list the preview shows, whatever order they are listed in; the counts and the
    sentence about the books follow from those rows."""
    import hashlib
    import json

    facts = {
        "connector": connector.name, "config": config_id, "store": ctx.store_handle,
        "directions": preview.directions, "orders_from": preview.orders_from.isoformat(),
        **{name: sorted(json.dumps(row, sort_keys=True, default=str) for row in getattr(preview, name))
           for name in _LISTS},
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


_LISTS = tuple(f.name for f in fields(FirstSyncPreview) if f.default_factory is list)


def order_effect(posting: int, total: int, service: str) -> str:
    """What the first sync does to the books with the orders it imports: issued as invoices
    when any posts, else recorded; one order is counted in the singular."""
    if posting:
        return t("connectors.preview_orders_post" + ("_one" if posting == 1 else ""), n=posting, service=service)
    return t("connectors.preview_orders_record" + ("_one" if total == 1 else ""), n=total, service=service)

def _status_key(connector: ConnectorBase, status: str) -> str | None:
    """The key of the label an order's status reads by, or None for a status the
    connector does not name (a store's own custom status)."""
    label = connector.order_statuses.get(status)
    return f"connectors.order_status_{label}" if label else None


async def orders_start(session, company_id, orders_from: date | datetime | None) -> datetime:
    """The earliest order date a connector reads, the same for the preview and the import:
    midnight in the company's business time zone starting ``orders_from`` (a day), or the day
    a saved start falls on in that zone, or the company's today when none is given. The day
    is never taken from a stored instant read in UTC."""
    from celerp.services.business_time import company_timezone, company_today

    zone = await company_timezone(session, company_id)
    if isinstance(orders_from, datetime):
        day = orders_from.astimezone(zone).date()
    else:
        day = orders_from or await company_today(session, company_id)
    return datetime.combine(day, time(), tzinfo=zone)


async def build_preview(
    connector: ConnectorBase, ctx: ConnectorContext, directions: dict, orders_from: datetime, config_id: int
) -> FirstSyncPreview:
    """What a first sync in ``directions``, importing orders from ``orders_from``, would
    do on the connection ``config_id``. Reads only: nothing is written to Celerp or to the
    platform. The directions are read as the full map Start saves, so the same choices
    give the same version however they were named."""
    directions = validate_directions(connector, directions)
    preview = FirstSyncPreview(directions=directions, orders_from=orders_from)
    if directions.get("products", SyncDirection.OFF.value) != SyncDirection.OFF.value:
        await _preview_products(connector, ctx, directions, preview)

    if entity_allowed(connector, "orders", directions):
        orders = await connector.preview_orders(ctx, orders_from)
        preview.orders = _newest_first([
            {"id": o.id, "number": o.number, "status": o.status, "created": o.created,
             "status_key": _status_key(connector, o.status),
             "total": o.total, "currency": o.currency, "posts": o.posts} for o in orders
        ], key="created")
        preview.posting_orders = sum(1 for o in orders if o.posts)
        preview.order_effect = order_effect(preview.posting_orders, len(orders), connector.display_name)
    preview.version = _version(connector, ctx, config_id, preview)
    return preview


@needs_modules("celerp-inventory")
async def _preview_products(
    connector: ConnectorBase, ctx: ConnectorContext, directions: dict, preview: FirstSyncPreview
) -> None:
    """The products part of ``preview``: the store's products against Celerp's items,
    and the items that would go out."""
    import uuid

    from celerp.db import get_session_ctx
    from celerp_inventory.services import (
        catalog_product_rows,
        normalize_sku,
        resolve_external_product,
        sellable_quantities,
    )
    from celerp.connectors.product_links import external_link_for_state

    remote_products = await connector.preview_products(ctx)
    cid = uuid.UUID(str(ctx.company_id))
    store_skus = set()
    stock_goes_out = entity_allowed(connector, "inventory_out", directions)
    async with get_session_ctx() as session:
        sellable = await sellable_quantities(session, cid)
        for remote in remote_products:
            row = {"key": remote.key, "sku": remote.sku or None, "name": remote.name or None,
                   "modified": remote.modified, "price": remote.price, "stock": remote.stock}
            if not remote.sku:
                preview.no_sku.append(row)
                continue
            store_skus.add(normalize_sku(remote.sku))
            try:
                found = await resolve_external_product(
                    session, cid, connector.name, remote.product_id, remote.variation_id, sku=remote.sku)
            except ValueError as exc:
                preview.problems.append({**row, "reason": str(exc)})
                continue
            if found is None:
                preview.new.append(row)
                continue
            state = found.state or {}
            celerp_stock = sellable.get(found.entity_id, 0.0)
            # The stock push sets the store's count to Celerp's sellable quantity.
            stock_change = ({"store": remote.stock, "celerp": celerp_stock}
                            if stock_goes_out and remote.stock is not None and remote.stock != celerp_stock
                            else None)
            preview.stock_changes += stock_change is not None
            preview.matches.append({**row, "entity_id": found.entity_id,
                                    "celerp_name": state.get("name"),
                                    "differences": _differences(state, remote),
                                    "celerp_stock": celerp_stock, "stock_change": stock_change})
        if entity_allowed(connector, "products_out", directions):
            for item in await catalog_product_rows(session, cid):
                state = item.state or {}
                if (not state.get("sku") or normalize_sku(state["sku"]) in store_skus
                        or external_link_for_state(state, connector.name)):
                    continue
                preview.to_publish.append({"entity_id": item.entity_id, "sku": state["sku"],
                                           "name": state.get("name"), "modified": item.updated_at})
    preview.matches = _newest_first(preview.matches)
    preview.new = _newest_first(preview.new)
    preview.no_sku = _newest_first(preview.no_sku)
    preview.problems = _newest_first(preview.problems)
    preview.to_publish = _newest_first(preview.to_publish)
