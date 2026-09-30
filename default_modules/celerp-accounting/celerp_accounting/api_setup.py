# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""API route registration for celerp-accounting module."""

from celerp.importers.sinks import register_sink
from celerp_accounting.migration_sink import SINK
from celerp_accounting.routes import router as accounting_router


def setup_api_routes(app) -> None:
    app.include_router(accounting_router, prefix="/accounting", tags=["accounting"])
    register_sink(SINK)
