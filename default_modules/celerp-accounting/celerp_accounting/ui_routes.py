# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-accounting UI routes - delegates to ui.routes.accounting."""
from __future__ import annotations
import logging
log = logging.getLogger(__name__)


def setup_ui_routes(app) -> None:
    from ui.routes import (
        accounting, accounting_import, financial_reports, fund_reports, recognition, reconciliation,
        settings_accounting, settings_funds,
    )
    # The import, reconcile and recognition pages go first: they must match before the
    # /accounting/{...} pages do. The financial reports sit at /reports but belong
    # to this module, which owns the figures, so they stay reachable for a company
    # that runs accounting without the reports module installed.
    for mod in (accounting_import, reconciliation, recognition, accounting, financial_reports, fund_reports,
                settings_accounting, settings_funds):
        mod.setup_routes(app)
    log.info("celerp-accounting: UI routes registered")
