# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-accounting UI routes - delegates to ui.routes.accounting."""
from __future__ import annotations
import logging
log = logging.getLogger(__name__)


def setup_ui_routes(app) -> None:
    from ui.routes import (
        accounting, accounting_import, financial_reports, reconciliation, settings_accounting,
    )
    # The import and reconcile pages go first: they must match before the
    # /accounting/{...} pages do. The financial reports sit at /reports but belong
    # to this module, which owns the figures, so they stay reachable for a company
    # that runs accounting without the reports module installed.
    for mod in (accounting_import, reconciliation, accounting, financial_reports, settings_accounting):
        mod.setup_routes(app)
    log.info("celerp-accounting: UI routes registered")
