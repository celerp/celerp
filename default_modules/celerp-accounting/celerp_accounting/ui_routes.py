# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-accounting UI routes - delegates to ui.routes.accounting."""
from __future__ import annotations
import logging
log = logging.getLogger(__name__)


def setup_ui_routes(app) -> None:
    from ui.routes.accounting import setup_routes
    setup_routes(app)
    log.info("celerp-accounting: UI routes registered")
