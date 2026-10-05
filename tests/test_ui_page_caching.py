# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""App pages are never reused from the browser cache: Back after switching company
must load the page again for the company now open, not show the one before it."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from test_helpers import authed_cookies


@pytest.mark.asyncio
async def test_pages_are_not_kept_in_the_browser_cache_and_static_files_are():
    """Red statement: /inventory came back with no Cache-Control, so the browser's
    Back reused an earlier company's list and Delete on it answered "Not found"."""
    from ui.app import app as ui_app
    static = AsyncMock(return_value=([], {}, {}, [], [], {}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app), base_url="http://testserver") as c:
        with patch.multiple("ui.api_client",
                            list_items=AsyncMock(return_value={"items": [], "total": 0}),
                            get_valuation=AsyncMock(return_value={"item_count": 0, "category_counts": {}}),
                            get_company=AsyncMock(return_value={}),
                            list_import_batches=AsyncMock(return_value={"batches": []})), \
             patch("ui.routes.inventory._load_inventory_static_metadata", new=static):
            page = await c.get("/inventory", params={"filter": "demo"}, cookies=authed_cookies(role="owner"))
        css = await c.get("/static/app.css")
    assert page.status_code == 200
    assert page.headers.get("cache-control") == "no-store", dict(page.headers)
    assert css.status_code == 200
    assert "no-store" not in css.headers.get("cache-control", ""), dict(css.headers)
