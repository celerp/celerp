# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-connectors — External platform sync connectors for Celerp.

Cloud-gated via X-Session-Token (Celerp Cloud subscription required).

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
        "Sync products, orders, and contacts from Shopify, QuickBooks, Xero, "
        "Lazada, and Shopee. Requires Celerp Cloud subscription."
    ),
    "license": "MIT",
    "author": "Celerp",

    # ── Routes ────────────────────────────────────────────────────────────────
    "api_routes": "celerp_connectors.routes",
    "depends_on": ["celerp-inventory", "celerp-docs"],

    # ── Extension slots ───────────────────────────────────────────────────────
    "slots": {
        "projection_handler": {
            "prefix": "mp.",
            "handler": "celerp.projections.handlers.marketplace:apply_marketplace_event",
        },
    },
}
