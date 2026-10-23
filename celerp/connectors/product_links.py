# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Links between a company's items and a store's products.

A link is stored on the item, in Inventory's records, so Inventory writes it. Reading
one needs no module: connecting, disconnecting and deactivating a company read links
whether or not this process has started Inventory."""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.connectors.ownership import PRODUCT_CHANNEL_PLATFORMS
from celerp.modules.boundary import company_settings, require_on
from celerp.modules.loader import is_running

INVENTORY = "celerp-inventory"


def _legacy_external_link(platform: str, idem_key: str) -> dict:
    """Decode connector identity stored by releases before external_links existed."""
    parts = (idem_key or "").split(":")
    if parts[0] != platform:
        return {}
    if platform == "shopify" and len(parts) >= 3:
        return {"product_id": parts[1], "variant_id": parts[2], "sync_enabled": True}
    if platform == "woocommerce" and len(parts) >= 2:
        link = {"product_id": parts[1], "sync_enabled": True}
        if len(parts) >= 3 and parts[2]:
            link["variation_id"] = parts[2]
        return link
    return {}


def external_link_for_state(state: dict, platform: str) -> dict:
    """Return one normalized external product link without mutating item state."""
    links = state.get("external_links") or {}
    raw = links.get(platform) if isinstance(links, dict) else None
    if isinstance(raw, dict) and raw.get("detached") is True:
        return {}
    if isinstance(raw, dict) and raw.get("product_id") not in (None, ""):
        return dict(raw)
    return _legacy_external_link(platform, str(state.get("idempotency_key") or ""))


async def require_inventory_for(company_id, platform: str) -> None:
    """Connecting a store, or choosing which items sync with it, writes links on
    Inventory's items: refused, naming Inventory, while the company cannot use it."""
    if platform in PRODUCT_CHANNEL_PLATFORMS:
        require_on(await company_settings(company_id), INVENTORY)


async def refuse_while_links_cannot_detach(session: AsyncSession, company_id, platform: str) -> None:
    """Refuse, naming Inventory, while the company's items still link to *platform*
    products and this process has not started Inventory to turn them into history.
    With no link left there is nothing to detach, so the caller goes ahead."""
    if platform not in PRODUCT_CHANNEL_PLATFORMS or is_running(INVENTORY):
        return
    cid = uuid.UUID(str(company_id))
    states = await session.scalars(sa.select(Projection.state).where(
        Projection.company_id == cid, Projection.entity_type == "item"))
    if any(external_link_for_state(state or {}, platform) for state in states):
        company = await session.get(Company, cid)
        require_on(company.settings if company is not None else None, INVENTORY)


async def detach_product_links(session: AsyncSession, company_id, platform: str) -> None:
    """Turn every link from the company's items to *platform* products into history,
    in the caller's transaction (``refuse_while_links_cannot_detach`` first)."""
    await refuse_while_links_cannot_detach(session, company_id, platform)
    if is_running(INVENTORY):
        from celerp_inventory.services import detach_external_links_for_platform
        await detach_external_links_for_platform(session, company_id, platform)
