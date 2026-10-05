# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-subscriptions UI routes."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_ui_routes(app) -> None:
    # Import before the list routes, so /subscriptions/import is not read as a subscription id.
    from ui.routes import subscriptions_import
    from ui.routes.subscriptions import setup_routes
    subscriptions_import.setup_routes(app)
    setup_routes(app)
    log.info("celerp-subscriptions: UI routes registered")
