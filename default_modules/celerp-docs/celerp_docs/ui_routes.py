# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-docs UI routes: documents + lists — delegates to canonical core implementation."""

from ui.routes.documents import setup_routes as _setup


def setup_ui_routes(app) -> None:
    _setup(app)
