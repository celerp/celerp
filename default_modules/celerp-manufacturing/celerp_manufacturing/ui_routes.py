# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-manufacturing UI routes.

Delegates to the existing core UI route modules. The module loader calls
setup_ui_routes(app) which wires all manufacturing pages and HTMX fragments
into the FastHTML app — identical to the previous hardcoded registration.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_ui_routes(app) -> None:
    """Register all manufacturing UI routes into the FastHTML app."""
    from ui.routes.manufacturing import setup_routes as setup_orders
    setup_orders(app)
    log.info("celerp-manufacturing: UI routes registered")
