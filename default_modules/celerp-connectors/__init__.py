# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""celerp-connectors — External platform sync connectors for Celerp.

Cloud-gated via X-Session-Token (Celerp Connect subscription required).

Bundled connectors
------------------
- WooCommerce  (products, stock, orders)
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
        "Sync products, orders, and contacts with WooCommerce, Shopify, "
        "QuickBooks, and Xero. Requires Celerp Connect subscription."
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
        "catalog_channel": [
            {"id": "woocommerce", "label": "WooCommerce", "marker": "W",
             "requires_connector": "woocommerce", "write_permission": "adjust_inventory", "can_create": True},
            {"id": "shopify", "label": "Shopify", "marker": "S",
             "requires_connector": "shopify", "write_permission": "adjust_inventory", "can_create": False},
        ],
        "bulk_action": [
            {"label": "Publish to WooCommerce", "label_key": "connectors.bulk_woo_publish",
             "form_action": "/api/items/bulk/channel-sync/woocommerce/publish",
             "action_type": "htmx", "permission": "adjust_inventory", "requires_connector": "woocommerce"},
            {"label": "Unpublish from WooCommerce", "label_key": "connectors.bulk_woo_unpublish",
             "form_action": "/api/items/bulk/channel-sync/woocommerce/unpublish",
             "action_type": "htmx", "permission": "adjust_inventory", "requires_connector": "woocommerce"},
            {"label": "Stop syncing with WooCommerce", "label_key": "connectors.bulk_woo_stop",
             "form_action": "/api/items/bulk/channel-sync/woocommerce/stop",
             "action_type": "htmx", "permission": "adjust_inventory", "requires_connector": "woocommerce"},
            {"label": "Resume syncing with WooCommerce", "label_key": "connectors.bulk_woo_resume",
             "form_action": "/api/items/bulk/channel-sync/woocommerce/resume",
             "action_type": "htmx", "permission": "adjust_inventory", "requires_connector": "woocommerce"},
            {"label": "Push to Shopify", "label_key": "connectors.bulk_shopify_resume",
             "form_action": "/api/items/bulk/channel-sync/shopify/resume",
             "action_type": "htmx", "permission": "adjust_inventory", "requires_connector": "shopify"},
            {"label": "Stop pushing to Shopify", "label_key": "connectors.bulk_shopify_stop",
             "form_action": "/api/items/bulk/channel-sync/shopify/stop",
             "action_type": "htmx", "permission": "adjust_inventory", "requires_connector": "shopify"},
        ],
        "item_action": [
            {"label": "WooCommerce", "label_key": "connectors.item_action_woocommerce",
             "href_template": "/inventory/{entity_id}/channel/woocommerce",
             "permission": "view_inventory", "requires_connector": "woocommerce"},
            {"label": "Shopify", "label_key": "connectors.item_action_shopify",
             "href_template": "/inventory/{entity_id}/channel/shopify",
             "permission": "view_inventory", "requires_connector": "shopify"},
        ],
    },
}
