# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-inventory UI routes.

Delegates to the existing core UI route module. The module loader calls
setup_ui_routes(app) which wires all inventory pages and HTMX fragments
into the FastHTML app.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_ui_routes(app) -> None:
    """Register all inventory UI routes into the FastHTML app."""
    from ui.routes.inventory import setup_routes
    setup_routes(app)
    log.info("celerp-inventory: UI routes registered")
