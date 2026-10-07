# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-dashboard UI routes - delegates to ui.routes.dashboard."""
from __future__ import annotations


def setup_ui_routes(app) -> None:
    from ui.routes.dashboard import setup_routes
    setup_routes(app)
