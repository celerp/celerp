# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-contacts UI routes."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_ui_routes(app) -> None:
    """Register all Contacts UI routes into the FastHTML app."""
    from ui.routes.contacts import setup_routes
    setup_routes(app)
    log.info("celerp-contacts: UI routes registered")
