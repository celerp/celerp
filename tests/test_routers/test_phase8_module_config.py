# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Phase 8: module config tests — labels default-on under Inventory nav, scanning removed."""

from __future__ import annotations

import pytest


def test_scanning_nav_not_in_module_nav():
    """Verify scanning nav entry is not in celerp-inventory PLUGIN_MANIFEST nav list."""
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "celerp_inventory_manifest",
        pathlib.Path(__file__).parents[2] / "default_modules" / "celerp-inventory" / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    nav = mod.PLUGIN_MANIFEST["slots"]["nav"]
    keys = [entry.get("key") for entry in nav]
    assert "scanning" not in keys, f"scanning nav entry should be removed, found: {nav}"


def test_labels_nav_group_is_inventory():
    """Verify celerp-labels nav has group='Inventory' and order=32."""
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "celerp_labels_manifest",
        pathlib.Path(__file__).parents[2] / "default_modules" / "celerp-labels" / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    nav = mod.PLUGIN_MANIFEST["slots"]["nav"]
    assert nav.get("group") == "Inventory", f"Expected group='Inventory', got: {nav.get('group')}"
    assert nav.get("order") == 32, f"Expected order=32, got: {nav.get('order')}"


@pytest.mark.asyncio
async def test_scanning_route_returns_404(client):
    """GET /scanning returns 404 — scanning module disabled."""
    r = await client.get("/scanning")
    assert r.status_code == 404
