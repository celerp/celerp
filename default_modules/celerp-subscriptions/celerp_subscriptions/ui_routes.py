# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-subscriptions UI routes."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_ui_routes(app) -> None:
    from ui.routes.subscriptions import setup_routes
    setup_routes(app)
    log.info("celerp-subscriptions: UI routes registered")
