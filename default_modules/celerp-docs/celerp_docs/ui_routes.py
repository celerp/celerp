# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-docs UI routes: documents + lists — delegates to canonical core implementation."""

from ui.routes import docs_import, documents, lists_import, settings_purchasing, settings_sales


def setup_ui_routes(app) -> None:
    # The import pages go first: /docs/import and /lists/import must match before
    # the /docs/{entity_id} and /lists/{entity_id} pages do.
    for mod in (docs_import, lists_import, documents, settings_sales, settings_purchasing):
        mod.setup_routes(app)
