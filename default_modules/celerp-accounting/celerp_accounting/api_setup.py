# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""API route registration for celerp-accounting module."""

from celerp_accounting.routes import router as accounting_router


def setup_api_routes(app) -> None:
    app.include_router(accounting_router, prefix="/accounting", tags=["accounting"])
