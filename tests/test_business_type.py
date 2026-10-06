# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""POST /companies/me/business-type: the one operation that changes a company's
business type, and the invariants it keeps (additive modules and categories,
system-owned defaults only, demo-only inventory replacement, retry-safe restart
reporting, no generic settings bypass)."""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from celerp.config import read_config, write_config
from celerp.modules.registry import get_enabled
from celerp.services.demo import payment_terms_for, terms_conditions_for
from celerp.services.terms import normalize_terms_templates


_DEFAULT_MODULES = str(Path(__file__).resolve().parents[1] / "default_modules")


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
@pytest.mark.parametrize("key", ["payment_terms", "purchasing_payment_terms"])
async def test_earlier_generic_payment_terms_follow_the_type(client, key):
    """Companies set up before Net 90 joined the generic list still hold the shorter
    list untouched; it moves to the new type's terms like the current default."""
    h = await _owner(client)
    earlier = [t for t in payment_terms_for(None) if t["name"] != "Net 90"]
    await _patch_settings(client, h, {key: earlier})
    await _set(client, h, "gemstones")
    assert (await _settings(client, h))[key] == payment_terms_for("gemstones")


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
    # The general type has no manufacturing; switching to it keeps it enabled.
    assert "celerp-manufacturing" in read_config()["modules"]["enabled"], "a switch never disables modules"
    assert "celerp-manufacturing" in get_enabled(await _settings(client, h))


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
async def test_restart_required_until_modules_running(client, monkeypatch):
    """The config write is not the signal: a retry after the config already holds the
    modules still reports a restart while they are not running."""
    monkeypatch.setenv("MODULE_DIR", _DEFAULT_MODULES)
    monkeypatch.delenv("ENABLED_MODULES", raising=False)
    h = await _owner(client)
    with patch("celerp.modules.loader.is_running", return_value=False):
        assert (await _set(client, h, "gemstones")).json()["restart_required"] is True
        assert (await _set(client, h, "gemstones")).json()["restart_required"] is True
    with patch("celerp.modules.loader.is_running", return_value=True):
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


# -- demo replacement keeps anything the user touched ------------------------------

async def _demo_by_sku(client, h) -> dict[str, dict]:
    return {i["sku"]: i for i in await _items(client, h) if str(i.get("sku", "")).startswith("DEMO-")}


async def _rename(client, h, item_id: str, name: str) -> None:
    r = await client.patch(f"/items/{item_id}", headers=h,
                           json={"fields_changed": {"name": {"old": None, "new": name}}})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_edited_demo_item_is_kept_with_its_history(client):
    h = await _owner(client)
    await _set(client, h, "gemstones")
    edited = (await _demo_by_sku(client, h))["DEMO-DIA-001"]
    await _rename(client, h, edited["id"], "My own diamond")
    r = await _set(client, h, "fashion")
    assert r.status_code == 200, r.text
    demo = await _demo_by_sku(client, h)
    assert demo["DEMO-DIA-001"]["id"] == edited["id"]
    assert demo["DEMO-DIA-001"]["name"] == "My own diamond"
    assert "DEMO-RUB-001" not in demo, "untouched demo items of the old type are replaced"
    assert "DEMO-FSH-001" in demo, "the new type's demo set is seeded"


@pytest.mark.asyncio
async def test_demo_item_used_on_a_document_is_kept(client):
    h = await _owner(client)
    await _set(client, h, "gemstones")
    used = (await _demo_by_sku(client, h))["DEMO-RUB-001"]
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "c:1", "contact_name": "Buyer",
        "line_items": [{"item_id": used["id"], "sku": used["sku"], "description": "Ruby",
                        "quantity": 1, "unit_price": 5, "line_total": 5}],
        "subtotal": 5, "tax": 0, "total": 5,
    })
    assert r.status_code == 200, r.text
    await _set(client, h, "fashion")
    demo = await _demo_by_sku(client, h)
    assert demo.get("DEMO-RUB-001", {}).get("id") == used["id"]
    assert "DEMO-DIA-001" not in demo


@pytest.mark.asyncio
async def test_first_import_keeps_a_demo_item_used_on_a_document(client):
    h = await _owner(client)
    await _set(client, h, "gemstones")
    used = (await _demo_by_sku(client, h))["DEMO-RUB-001"]
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "c:1", "contact_name": "Buyer",
        "line_items": [{"item_id": used["id"], "sku": used["sku"], "description": "Ruby",
                        "quantity": 1, "unit_price": 5, "line_total": 5}],
        "subtotal": 5, "tax": 0, "total": 5,
    })
    assert r.status_code == 200, r.text
    imp = await client.post("/items/import/batch", headers=h, json={"records": [{
        "entity_id": "item:real-imp-2", "event_type": "item.created", "source": "csv",
        "idempotency_key": "real-imp-2",
        "data": {"sku": "REAL-IMP-2", "name": "Imported", "quantity": 1, "sell_by": "piece"},
    }]})
    assert imp.status_code == 200, imp.text
    demo = await _demo_by_sku(client, h)
    assert set(demo) == {"DEMO-RUB-001"} and demo["DEMO-RUB-001"]["id"] == used["id"]


@pytest.mark.asyncio
async def test_no_demo_items_left_means_nothing_is_seeded(client):
    h = await _owner(client)
    imp = await client.post("/items/import/batch", headers=h, json={"records": [{
        "entity_id": "item:real-imp-1", "event_type": "item.created", "source": "csv",
        "idempotency_key": "real-imp-1",
        "data": {"sku": "REAL-IMP-1", "name": "Imported", "quantity": 2, "sell_by": "piece"},
    }]})
    assert imp.status_code == 200, imp.text
    assert not await _demo_by_sku(client, h), "the first import removes the demo set"
    r = await _set(client, h, "gemstones")
    assert r.status_code == 200, r.text
    assert not await _demo_by_sku(client, h), "a type change must not bring demo items back"
    assert r.json()["changes"]["demo_items_replaced"] == 0


@pytest.mark.asyncio
async def test_partial_replacement_never_duplicates_a_sku(client):
    """Kept demo items keep their SKU; the new set is seeded without any SKU that is
    already in use."""
    h = await _owner(client)
    await _set(client, h, "gemstones")
    edited = (await _demo_by_sku(client, h))["DEMO-DIA-001"]
    await _rename(client, h, edited["id"], "Kept diamond")
    await _set(client, h, "fashion")
    await _set(client, h, "gemstones")
    skus = [i["sku"] for i in await _items(client, h)]
    assert skus.count("DEMO-DIA-001") == 1
    demo = await _demo_by_sku(client, h)
    assert demo["DEMO-DIA-001"]["id"] == edited["id"]
    assert "DEMO-RUB-001" in demo, "the rest of the gemstones set is seeded again"


# -- the change summary ------------------------------------------------------------

@pytest.mark.asyncio
async def test_change_reports_what_it_changed(client):
    h = await _owner(client)
    await _set(client, h, "blank")
    r = await _set(client, h, "gemstones")
    assert r.status_code == 200, r.text
    changes = r.json()["changes"]
    assert changes["categories_added"]["diamond"] == "Diamond", "keyed, so the UI shows them translated"
    assert "Manufacturing" in changes["modules_enabled"], "modules are reported by display name"
    assert "Industry Verticals" not in changes["modules_enabled"], "already enabled by the blank type"
    assert changes["settings_updated"] == []
    assert changes["defaults_updated"] == ["payment_terms", "terms_conditions"]
    assert changes["demo_items_replaced"] == 3, "the blank type's three demo items"
    assert changes["demo_items_kept"] == 0


@pytest.mark.asyncio
async def test_change_reports_preset_settings(client):
    h = await _owner(client)
    changes = (await _set(client, h, "agricultural")).json()["changes"]
    assert changes["settings_updated"] == ["inventory_method"]
    assert changes["defaults_updated"] == ["payment_terms"]


@pytest.mark.asyncio
async def test_repeat_reports_nothing_changed(client):
    h = await _owner(client)
    await _set(client, h, "gemstones")
    again = (await _set(client, h, "gemstones")).json()["changes"]
    assert again == {"categories_added": {}, "modules_enabled": [], "settings_updated": [],
                     "defaults_updated": [], "demo_items_replaced": 0, "demo_items_kept": 0}


@pytest.mark.asyncio
async def test_kept_demo_items_are_counted(client):
    h = await _owner(client)
    await _set(client, h, "gemstones")
    await _rename(client, h, (await _demo_by_sku(client, h))["DEMO-DIA-001"]["id"], "Mine")
    changes = (await _set(client, h, "fashion")).json()["changes"]
    assert changes["demo_items_kept"] == 1
    assert changes["demo_items_replaced"] == 9


# -- purchasing payment terms follow the same ownership rule -----------------------

@pytest.mark.asyncio
async def test_untouched_purchasing_terms_follow_the_type(client):
    h = await _owner(client)
    assert (await client.get("/companies/me/purchasing-payment-terms", headers=h)).status_code == 200
    await _set(client, h, "gemstones")
    assert (await _settings(client, h))["purchasing_payment_terms"] == payment_terms_for("gemstones")


@pytest.mark.asyncio
async def test_customised_purchasing_terms_survive(client):
    h = await _owner(client)
    mine = [{"name": "Supplier 45", "days": 45, "description": "Ours"}]
    r = await client.patch("/companies/me/purchasing-payment-terms", headers=h, json={"terms": mine})
    assert r.status_code == 200, r.text
    await _set(client, h, "gemstones")
    assert (await _settings(client, h))["purchasing_payment_terms"] == mine


@pytest.mark.asyncio
async def test_purchasing_terms_not_created_by_a_type_change(client):
    h = await _owner(client)
    await _set(client, h, "gemstones")
    assert "purchasing_payment_terms" not in await _settings(client, h)


# -- one payment-terms default -----------------------------------------------------

@pytest.mark.asyncio
async def test_generic_payment_terms_are_the_list_users_see(client):
    import celerp.routers.companies as companies
    import celerp.services.payment_terms as payment_terms
    assert companies.DEFAULT_PAYMENT_TERMS is payment_terms.DEFAULT_PAYMENT_TERMS
    h = await _owner(client)
    await _patch_settings(client, h, {"payment_terms": []})
    shown = (await client.get("/companies/me/payment-terms", headers=h)).json()
    assert shown == payment_terms_for(None)
    assert "Net 90" in [t["name"] for t in shown]


# -- restart reporting -------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_restart_when_the_module_system_is_off(client, monkeypatch):
    monkeypatch.delenv("MODULE_DIR", raising=False)
    h = await _owner(client)
    assert (await _set(client, h, "gemstones")).json()["restart_required"] is False


@pytest.mark.asyncio
async def test_no_restart_when_enabled_modules_are_pinned_by_the_environment(client, monkeypatch):
    monkeypatch.setenv("MODULE_DIR", _DEFAULT_MODULES)
    monkeypatch.setenv("ENABLED_MODULES", "celerp-subscriptions")
    monkeypatch.delenv("CELERP_SUPERVISED", raising=False)
    h = await _owner(client)
    assert (await _set(client, h, "gemstones")).json()["restart_required"] is False


@pytest.mark.asyncio
async def test_supervised_restart_rereads_the_config(client, monkeypatch):
    """Under the supervisor ENABLED_MODULES is rebuilt from config on restart, so a
    pinned list does not stop the restart from loading the new modules."""
    monkeypatch.setenv("MODULE_DIR", _DEFAULT_MODULES)
    monkeypatch.setenv("ENABLED_MODULES", "celerp-subscriptions")
    monkeypatch.setenv("CELERP_SUPERVISED", "1")
    h = await _owner(client)
    assert (await _set(client, h, "gemstones")).json()["restart_required"] is True


@pytest.mark.asyncio
async def test_retry_after_failed_db_step_still_requires_restart(client, monkeypatch):
    """The company update fails: nothing is written, and the retry asks for the
    restart that loads the type's modules."""
    monkeypatch.setenv("MODULE_DIR", _DEFAULT_MODULES)
    monkeypatch.delenv("ENABLED_MODULES", raising=False)
    h = await _owner(client)
    with patch("celerp.services.business_type.reconcile_vertical_defaults",
               side_effect=RuntimeError("database step failed")):
        with pytest.raises(RuntimeError):
            await _set(client, h, "gemstones")
    assert "celerp-inventory" not in (read_config().get("modules") or {}).get("enabled", [])
    assert (await _settings(client, h)).get("vertical") is None, "the company update rolled back"
    r = await _set(client, h, "gemstones")
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is True
    assert (await _settings(client, h))["vertical"] == "gemstones"
