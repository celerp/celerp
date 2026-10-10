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


def test_scanning_leaves_no_router_event_or_label_behind():
    """The standalone scanning module was never mounted and its events were never written;
    barcode scanning lives on lists and documents. No router, handler, event type, schema,
    merge rule or navigation label of the old module remains."""
    import importlib.util
    import json
    import pathlib

    from celerp.events.schemas import EVENT_SCHEMA_MAP
    from celerp.events.types import EventType
    from celerp.projections.engine import MERGE_EVENTS

    assert importlib.util.find_spec("celerp_inventory.routes_scanning") is None
    assert importlib.util.find_spec("celerp.projections.handlers.scanning") is None
    left = [e for e in EVENT_SCHEMA_MAP if e.startswith("scan.")]
    left += [e.value for e in EventType if e.value.startswith("scan.")]
    left += [e for e in MERGE_EVENTS if e.startswith("scan.")]
    assert not left, left
    root = pathlib.Path(__file__).parents[2]
    for catalog in sorted((root / "ui" / "locales").glob("*.json")):
        keys = json.loads(catalog.read_text(encoding="utf-8"))
        assert not {"nav.scanning", "page.scanning"} & set(keys), catalog.name
    assert "/scanning/" not in (root / "ui" / "api_client.py").read_text(encoding="utf-8")
