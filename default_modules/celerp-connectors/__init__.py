# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""celerp-connectors — External platform sync connectors for Celerp.

Cloud-gated via X-Session-Token (Celerp Connect subscription required).

Bundled connectors
------------------
- Shopify      (products, orders, contacts, inventory)
- QuickBooks   (products, orders, contacts, invoices)
- Xero         (products, orders, contacts, invoices)
- Lazada       — coming soon
- Shopee       — coming soon

Each connector delegates OAuth entirely to the Celerp relay service.
The core instance never stores platform credentials.
"""

PLUGIN_MANIFEST = {
    # ── Identity ──────────────────────────────────────────────────────────────
    "name": "celerp-connectors",
    "version": "0.1.0",
    "display_name": "Connectors",
    "description": (
        "Sync products, orders, and contacts from Shopify, QuickBooks, and "
        "Xero. Requires Celerp Connect subscription."
    ),
    "license": "LicenseRef-Proprietary",
    "author": "Celerp",

    # ── Routes ────────────────────────────────────────────────────────────────
    "api_routes": "celerp_connectors.routes",
    "depends_on": ["celerp-inventory", "celerp-docs"],

    # ── Extension slots ───────────────────────────────────────────────────────
    "slots": {
        "projection_handler": [
            {
                "prefix": "mp.",
                "handler": "celerp.projections.handlers.marketplace:apply_marketplace_event",
            },
            {
                "prefix": "shop.sync.",
                "handler": "celerp.projections.handlers.shopify:apply_shop_sync_event",
            },
        ],
        # Inventory bulk actions to opt items into / out of outbound Shopify sync.
        "bulk_action": [
            {
                "label": "Enable Shopify sync",
                "form_action": "/api/items/bulk/shopify-sync/enable",
                "icon": "🛍",
                "action_type": "htmx",
                "permission": "adjust_inventory",
            },
            {
                "label": "Disable Shopify sync",
                "form_action": "/api/items/bulk/shopify-sync/disable",
                "icon": "🛍",
                "action_type": "htmx",
                "permission": "adjust_inventory",
            },
        ],
    },
}
