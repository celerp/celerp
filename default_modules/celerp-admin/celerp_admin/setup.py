# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
from celerp_admin.routes import router


def setup_api_routes(app) -> None:
    app.include_router(router, prefix="/admin", tags=["admin"])
