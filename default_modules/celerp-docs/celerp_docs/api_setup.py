# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""API route registration for celerp-docs module."""

from celerp_docs.routes import router as docs_router, lists_router


def setup_api_routes(app) -> None:
    from celerp_docs.routes_share import public_router as share_public_router, router as share_router
    # share.public_router must come before docs.router so /docs/import isn't swallowed by /docs/{entity_id}
    app.include_router(share_public_router, tags=["share-public"])
    app.include_router(docs_router, prefix="/docs", tags=["docs"])
    app.include_router(lists_router, prefix="/lists", tags=["lists"])
    app.include_router(share_router, tags=["share"])
