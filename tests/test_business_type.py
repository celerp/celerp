# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""POST /companies/me/business-type: the one operation that changes a company's
business type, and the invariants it keeps (additive modules and categories,
system-owned defaults only, demo-only inventory replacement, retry-safe restart
reporting, no generic settings bypass)."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from celerp.config import read_config, write_config
from celerp.modules.registry import get_enabled
from celerp.services.demo import payment_terms_for, terms_conditions_for
from celerp.services.terms import normalize_terms_templates


async def _owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "TypeCo", "email": f"type-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _set(client, h, vertical: str):
    return await client.post("/companies/me/business-type", json={"vertical": vertical}, headers=h)


async def _settings(client, h) -> dict:
    return (await client.get("/companies/me", headers=h)).json()["settings"]


async def _patch_settings(client, h, patch_: dict) -> None:
    r = await client.patch("/companies/me", json={"settings": patch_}, headers=h)
    assert r.status_code == 200, r.text


async def _items(client, h) -> list[dict]:
    return (await client.get("/items", headers=h, params={"limit": 500})).json()["items"]


@pytest.mark.asyncio
async def test_setting_gems_persists_vertical(client):
    h = await _owner(client)
    r = await _set(client, h, "gemstones")
    assert r.status_code == 200, r.text
    assert r.json()["vertical"] == "gemstones"
    assert r.json()["changed"] is True
    assert (await _settings(client, h))["vertical"] == "gemstones"


@pytest.mark.asyncio
async def test_initial_agricultural_applies_fefo(client):
    h = await _owner(client)
    assert (await _set(client, h, "agricultural")).status_code == 200
    assert (await _settings(client, h))["inventory_method"] == "fefo"


@pytest.mark.asyncio
async def test_agricultural_to_gems_removes_untouched_preset_setting(client):
    h = await _owner(client)
    await _set(client, h, "agricultural")
    assert (await _set(client, h, "gemstones")).status_code == 200
    assert "inventory_method" not in await _settings(client, h)


@pytest.mark.asyncio
async def test_custom_inventory_method_survives_switch(client):
    h = await _owner(client)
    await _set(client, h, "agricultural")
    await _patch_settings(client, h, {"inventory_method": "fifo"})
    await _set(client, h, "gemstones")
    assert (await _settings(client, h))["inventory_method"] == "fifo"


@pytest.mark.asyncio
async def test_generic_untouched_defaults_become_target_defaults(client):
    h = await _owner(client)
    before = await _settings(client, h)
    assert before["payment_terms"] == payment_terms_for(None)
    await _set(client, h, "gemstones")
    after = await _settings(client, h)
    assert "Consignment 90" in [t["name"] for t in after["payment_terms"]]
    assert "Gemstone Sales Terms" in [t["name"] for t in after["terms_conditions"]]


@pytest.mark.asyncio
async def test_previous_vertical_untouched_defaults_become_target_defaults(client):
    h = await _owner(client)
    await _set(client, h, "gemstones")
    await _set(client, h, "agricultural")
    after = await _settings(client, h)
    assert after["payment_terms"] == payment_terms_for("agricultural")
    assert normalize_terms_templates(after["terms_conditions"]) == terms_conditions_for("agricultural")


@pytest.mark.asyncio
async def test_customised_terms_survive_switch(client):
    h = await _owner(client)
    mine_terms = [{"name": "House Terms", "days": 45, "description": "Our own"}]
    mine_tc = [{"name": "House T&C", "text": "Ours.", "doc_types": ["invoice"], "default_for": ["invoice"]}]
    await _patch_settings(client, h, {"payment_terms": mine_terms, "terms_conditions": mine_tc})
    await _set(client, h, "gemstones")
    after = await _settings(client, h)
    assert after["payment_terms"] == mine_terms
    assert [t["name"] for t in after["terms_conditions"]] == ["House T&C"]


@pytest.mark.asyncio
async def test_missing_category_schemas_added(client):
    h = await _owner(client)
    await _set(client, h, "gemstones")
    schemas = (await _settings(client, h))["category_schemas"]
    assert "diamond" in schemas and "jewelry" in schemas


@pytest.mark.asyncio
async def test_existing_custom_category_schema_not_overwritten(client):
    h = await _owner(client)
    custom = [{"key": "my_field", "label": "My field", "type": "text"}]
    await _patch_settings(client, h, {"category_schemas": {"diamond": custom}})
    await _set(client, h, "gemstones")
    schemas = (await _settings(client, h))["category_schemas"]
    assert schemas["diamond"] == custom
    assert "ruby" in schemas


@pytest.mark.asyncio
async def test_modules_added_without_removing_unrelated(client):
    h = await _owner(client)
    cfg = read_config()
    cfg.setdefault("modules", {})["enabled"] = ["celerp-unrelated-extra"]
    write_config(cfg)
    await _set(client, h, "gemstones")
    enabled_cfg = read_config()["modules"]["enabled"]
    assert "celerp-unrelated-extra" in enabled_cfg
    assert "celerp-inventory" in enabled_cfg
    await _set(client, h, "blank")
    assert "celerp-inventory" in read_config()["modules"]["enabled"], "a switch never disables modules"
    assert "celerp-inventory" in get_enabled(await _settings(client, h))


@pytest.mark.asyncio
async def test_same_value_retry_keeps_demo_items(client):
    h = await _owner(client)
    await _set(client, h, "gemstones")
    first = {i["id"] for i in await _items(client, h)}
    r = await _set(client, h, "gemstones")
    assert r.json()["changed"] is False
    assert {i["id"] for i in await _items(client, h)} == first


@pytest.mark.asyncio
async def test_change_replaces_only_demo_items(client):
    h = await _owner(client)
    r = await client.post("/items", headers=h, json={"sku": "REAL-001", "name": "Real stock", "sell_by": "piece", "quantity": 3})
    assert r.status_code == 200, r.text
    real_id = r.json()["id"]
    await _set(client, h, "gemstones")
    await _set(client, h, "fashion")
    items = await _items(client, h)
    skus = {i["sku"] for i in items}
    assert real_id in {i["id"] for i in items}, "real items survive every switch"
    assert "DEMO-001" not in skus and "DEMO-DIA-001" not in skus


@pytest.mark.asyncio
async def test_restart_required_until_modules_running(client):
    """The config write is not the signal: a retry after the config already holds the
    modules still reports a restart while they are not running."""
    h = await _owner(client)
    with patch("celerp.services.business_type.is_running", return_value=False):
        assert (await _set(client, h, "gemstones")).json()["restart_required"] is True
        assert (await _set(client, h, "gemstones")).json()["restart_required"] is True
    with patch("celerp.services.business_type.is_running", return_value=True):
        assert (await _set(client, h, "gemstones")).json()["restart_required"] is False


@pytest.mark.asyncio
async def test_generic_patch_rejects_vertical(client):
    h = await _owner(client)
    r = await client.patch("/companies/me", json={"settings": {"vertical": "gemstones"}}, headers=h)
    assert r.status_code == 422
    assert "business-type" in r.json()["detail"]
    assert (await _settings(client, h)).get("vertical") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("vertical", ["saas", "no_such_type", ""])
async def test_hidden_or_unknown_type_rejected(client, vertical):
    h = await _owner(client)
    r = await _set(client, h, vertical)
    assert r.status_code == 422
    assert (await _settings(client, h)).get("vertical") is None


@pytest.mark.asyncio
async def test_blank_is_a_valid_type(client):
    h = await _owner(client)
    assert (await _set(client, h, "blank")).status_code == 200
    assert (await _settings(client, h))["vertical"] == "blank"


@pytest.mark.asyncio
async def test_lifecycle_permission_required(client):
    h = await _owner(client)
    email = f"adm-{uuid.uuid4().hex[:8]}@test.example"
    r = await client.post("/companies/me/users", headers=h,
                          json={"email": email, "name": "Adm", "password": "pwvalid1", "role": "admin"})
    assert r.status_code == 200, r.text
    login = await client.post("/auth/login", json={"email": email, "password": "pwvalid1"})
    admin_h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    r = await _set(client, admin_h, "gemstones")
    assert r.status_code == 403
    assert (await _settings(client, h)).get("vertical") is None
