# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
from celerp_ai.routes import router, settings_router


def setup_api_routes(app) -> None:
    app.include_router(router, prefix="/ai", tags=["ai"])
    app.include_router(settings_router, prefix="/ai", tags=["ai"])
