# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-reports UI routes - delegates to ui.routes.reports."""
from __future__ import annotations
import logging
log = logging.getLogger(__name__)


def setup_ui_routes(app) -> None:
    from ui.routes.reports import setup_routes
    setup_routes(app)
    log.info("celerp-reports: UI routes registered")
