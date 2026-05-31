# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
from celerp_backup.routes import public_router, router


def setup_api_routes(app) -> None:
    app.include_router(router, prefix="/backup", tags=["backup"])
    app.include_router(public_router, prefix="/backup", tags=["backup"])
