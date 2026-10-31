# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""API route registration for celerp-accounting module."""

from celerp.importers.sinks import register_sink
from celerp.services.journal_accounts import ChartAccess, register_chart
from celerp_accounting.chart_rules import chart_accounts, lock_accounts
from celerp_accounting.import_service import add_posting_account
from celerp_accounting.migration_sink import SINK
from celerp_accounting.recon_guard import guard_journal_change
from celerp_accounting.routes import router as accounting_router


def setup_api_routes(app) -> None:
    app.include_router(accounting_router, prefix="/accounting", tags=["accounting"])
    register_sink(SINK)
    # The chart and the bank reconciliations as the core journal boundary and
    # posting-account choices see them.
    register_chart(ChartAccess(lock_accounts=lock_accounts, list_accounts=chart_accounts,
                               add_account=add_posting_account, guard_journal_change=guard_journal_change))
