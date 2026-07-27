# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-accounting UI routes - delegates to ui.routes.accounting."""
from __future__ import annotations
import logging
log = logging.getLogger(__name__)


def setup_ui_routes(app) -> None:
    from ui.routes.accounting import setup_routes
    from ui.routes.financial_reports import setup_routes as setup_report_routes
    setup_routes(app)
    # The financial reports sit at /reports but belong to this module, which owns
    # the figures. Registering them here keeps them reachable for a company that
    # runs accounting without the reports module installed.
    setup_report_routes(app)
    log.info("celerp-accounting: UI routes registered")
