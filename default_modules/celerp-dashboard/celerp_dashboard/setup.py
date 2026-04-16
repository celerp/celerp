# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
from celerp_dashboard.routes import router


def setup_api_routes(app) -> None:
    app.include_router(router, prefix="/dashboard", tags=["dashboard"])
