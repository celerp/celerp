# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""celerp.services.vertical_presets: locating the preset library through the module
loader, and the pure merge/reconcile rules business type and Apply preset share."""
from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest

from celerp.services import vertical_presets as vp


def _library(tmp_path, presets=(), categories=()):
    for kind, rows in (("presets", presets), ("categories", categories)):
        d = tmp_path / kind
        d.mkdir()
        for row in rows:
            (d / f"{row['name']}.json").write_text(json.dumps(row))
    return patch.object(vp, "_data_dir", return_value=tmp_path)


def test_library_resolves_through_module_loader_when_not_enabled():
    """The shipped catalog is found whether or not celerp-verticals is enabled."""
    names = {p["name"] for p in vp.list_presets()}
    assert {"gemstones", "agricultural", "blank"} <= names
    assert vp.load_category("diamond") is not None


def test_missing_package_gives_empty_catalog():
    with patch.object(vp, "_data_dir", return_value=None):
        assert vp.list_presets() == []
        assert vp.load_preset("gemstones") is None


def test_hidden_presets_only_on_request():
    assert vp.load_preset("saas") is None
    assert vp.load_preset("saas", allow_hidden=True) is not None
    assert "saas" not in {p["name"] for p in vp.list_presets()}


def test_unreadable_and_nameless_files_skipped(tmp_path):
    with _library(tmp_path, presets=[{"name": "ok"}]):
        (tmp_path / "presets" / "bad.json").write_text("{nope")
        (tmp_path / "presets" / "anon.json").write_text(json.dumps({"display_name": "x"}))
        assert [p["name"] for p in vp.list_presets()] == ["ok"]


def test_merge_adds_missing_and_keeps_existing(tmp_path):
    cats = [{"name": "a", "display_name": "A", "fields": [{"key": "x"}]},
            {"name": "b", "display_name": "B", "fields": [{"key": "y"}], "default_sell_by": "carat"}]
    with _library(tmp_path, categories=cats):
        settings = {"category_schemas": {"a": [{"key": "mine"}]}, "category_display_names": {"a": "Mine"},
                    "units": [{"name": "piece", "label": "Piece", "decimals": 0, "unit_type": "pieces"}]}
        out, resolved = vp.merge_missing_preset_categories(settings, {"name": "p", "categories": ["a", "b", "zz"]})
    assert out["category_schemas"]["a"] == [{"key": "mine"}]
    assert out["category_display_names"]["a"] == "Mine"
    assert out["category_schemas"]["b"] == [{"key": "y"}]
    assert resolved == ["a", "b"]
    assert any(u["name"] == "carat" for u in out["units"])
    assert settings["category_schemas"] == {"a": [{"key": "mine"}]}, "input is not mutated"


def test_reconcile_moves_only_system_owned_values():
    prev = {"company_settings": {"inventory_method": "fefo", "kept": 1}}
    target = {"company_settings": {"kept": 2, "new_key": "n"}}
    out = vp.reconcile_preset_settings({"inventory_method": "fefo", "kept": 5}, prev, target)
    assert "inventory_method" not in out
    assert out["kept"] == 5, "a user-changed value is kept"
    assert out["new_key"] == "n"


@pytest.mark.asyncio
async def test_item_create_applies_category_unit_defaults(client):
    """Inventory reads category unit defaults from this library (diamond: gram)."""
    r = await client.post("/auth/register", json={
        "company_name": "CatCo", "email": f"cat-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/items", headers=h, json={
        "sku": "CAT-1", "name": "Stone", "sell_by": "piece", "quantity": 1, "category": "diamond"})
    assert r.status_code == 200, r.text
    item = (await client.get(f"/items/{r.json()['id']}", headers=h)).json()
    assert item["purchase_unit"] == "gram"
    assert item["weight_unit"] == "gram"


def test_installed_preset_modules_skips_uninstalled():
    mods = vp.installed_preset_modules({"name": "p", "modules": ["celerp-inventory", "celerp-not-a-module"]})
    assert mods == ["celerp-inventory"]


def test_catalog_is_read_once_per_process():
    vp.list_presets()
    with patch.object(vp, "resolve_runtime_module_path", wraps=vp.resolve_runtime_module_path) as resolve, \
         patch.object(vp.json, "loads", wraps=json.loads) as parse:
        vp.list_presets()
        vp.load_preset("gemstones")
        vp.load_category("diamond")
    assert resolve.call_count == 0
    assert parse.call_count == 0


def test_cached_catalog_cannot_be_mutated_by_callers():
    vp.load_preset("gemstones")["categories"].append("not-a-category")
    assert "not-a-category" not in vp.load_preset("gemstones")["categories"]
