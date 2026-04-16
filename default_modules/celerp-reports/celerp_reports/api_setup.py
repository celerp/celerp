# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""API route registration for celerp-reports module."""

from celerp_reports.routes import router as reports_router


def setup_api_routes(app) -> None:
    app.include_router(reports_router, prefix="/reports", tags=["reports"])
