# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Inventory and manufacturing messages say what went wrong and what to do, in the user's language."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from ui.i18n import t

_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"
_LANGS = ("en", "th", "de", "fr", "es", "it", "pt", "id", "vi", "ja", "ar", "am")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


async def _headers(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "InvMsgCo", "email": f"inv-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _item(client, h, sku: str, **extra) -> str:
    r = await client.post("/items", headers=h, json={
        "sku": sku, "name": sku, "quantity": 5.0, "sell_by": "piece", "status": "available", **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _create_rejected(client, h, **fields) -> str:
    r = await client.post("/items", headers=h, json={
        "sku": f"S-{uuid.uuid4().hex[:6]}", "name": "Ring", "quantity": 1, "sell_by": "piece", **fields})
    assert r.status_code == 422, r.text
    return r.json()["detail"]


@pytest.mark.asyncio
async def test_bad_codes_say_what_to_remove(client):
    h = await _headers(client)
    assert await _create_rejected(client, h, sku="A,B") == t("inventory.err_sku_comma", "en")
    assert await _create_rejected(client, h, barcode="12AB") == t("inventory.err_barcode_digits", "en")
    assert await _create_rejected(client, h, gtin="1234567") == t("inventory.err_gtin_length", "en")


@pytest.mark.asyncio
async def test_unknown_unit_and_location_say_where_to_fix_them(client):
    h = await _headers(client)
    assert await _create_rejected(client, h, sell_by="crate") == t("inventory.err_unit_unknown", "en", unit="crate")
    iid = await _item(client, h, f"LC-{uuid.uuid4().hex[:6]}")
    r = await client.patch(f"/items/{iid}", headers=h,
                           json={"fields_changed": {"location_id": {"old": None, "new": "nowhere"}}})
    assert r.status_code == 422
    assert r.json()["detail"]["message_key"] == "location.not_found"
    assert (await _create_rejected(client, h, location_id=str(uuid.uuid4())))["message_key"] == "location.not_found"


@pytest.mark.asyncio
async def test_missing_item_says_refresh(client):
    h = await _headers(client)
    r = await client.get("/items/item:nope", headers=h)
    assert r.status_code == 404
    assert r.json()["detail"] == t("inventory.err_item_not_found", "en")
    r = await client.post("/items/bulk/status", headers=h, json={"entity_ids": ["item:nope"], "status": "archived"})
    assert r.status_code == 404
    assert r.json()["detail"] == t("inventory.err_items_not_found", "en")
    r = await client.post("/items/bulk/status", headers=h, json={"entity_ids": [], "status": "archived"})
    assert r.status_code == 422
    assert r.json()["detail"] == t("inventory.err_none_selected", "en")


@pytest.mark.asyncio
async def test_status_edits_point_to_the_right_action(client):
    h = await _headers(client)
    iid = await _item(client, h, f"ST-{uuid.uuid4().hex[:6]}")
    r = await client.post("/items/bulk/status", headers=h, json={"entity_ids": [iid], "status": "disposed"})
    assert r.status_code == 422
    assert r.json()["detail"] == t("inventory.err_dispose_via_write_off", "en")
    r = await client.post("/items/bulk/status", headers=h, json={"entity_ids": [iid], "status": "archived"})
    assert r.status_code == 200, r.text
    r = await client.post("/items/bulk/revert-to-draft", headers=h, json={"entity_ids": [iid]})
    assert r.status_code == 409
    assert r.json()["detail"] == t("inventory.err_revert_not_available", "en", status="Archived")


@pytest.mark.asyncio
async def test_merge_and_undo_messages(client):
    h = await _headers(client)
    iid = await _item(client, h, f"MG-{uuid.uuid4().hex[:6]}")
    r = await client.post("/items/merge", headers=h, json={"source_entity_ids": [iid], "target_sku_from": iid})
    assert r.status_code == 422
    assert r.json()["detail"] == t("inventory.err_merge_min_two", "en")
    r = await client.post("/items/merge", headers=h, json={"source_entity_ids": [iid, iid], "target_sku_from": iid})
    assert r.status_code == 422
    assert r.json()["detail"] == t("inventory.err_merge_duplicate", "en")
    r = await client.post(f"/items/import/batches/{uuid.uuid4()}/undo", headers=h)
    assert r.status_code == 404
    assert r.json()["detail"] == t("inventory.err_import_not_found", "en")


@pytest.mark.asyncio
async def test_work_center_messages(client):
    h = await _headers(client)
    r = await client.post("/manufacturing/work-centers", headers=h, json={"name": "  "})
    assert r.status_code == 422
    assert r.json()["detail"] == t("manufacturing.err_wc_name_required", "en")
    r = await client.delete(f"/manufacturing/work-centers/{uuid.uuid4()}", headers=h)
    assert r.status_code == 404
    assert r.json()["detail"] == t("manufacturing.err_wc_not_found", "en")


@pytest.mark.asyncio
async def test_inventory_messages_follow_the_users_language(client):
    h = {**await _headers(client), "Accept-Language": "th"}
    assert await _create_rejected(client, h, sku="A,B") == t("inventory.err_sku_comma", "th")
    assert t("inventory.err_sku_comma", "th") != t("inventory.err_sku_comma", "en")


def test_code_validators_raise_the_translated_message():
    from celerp.inventory_codes import (MAX_RFID_EPC_LEN, MAX_SKU_LEN, validate_rfid_epc,
                                        validate_sku)

    with pytest.raises(ValueError, match=re.escape(t("inventory.err_sku_too_long", "en", max=MAX_SKU_LEN))):
        validate_sku("A" * (MAX_SKU_LEN + 1))
    with pytest.raises(ValueError, match=re.escape(t("inventory.err_rfid_too_long", "en", max=MAX_RFID_EPC_LEN))):
        validate_rfid_epc("A" * (MAX_RFID_EPC_LEN + 1))


def test_projection_conflicts_say_refresh():
    from celerp.projections import engine

    assert engine._not_found("item").detail == t("inventory.err_item_not_found", "en")
    assert engine._not_found("doc").detail == t("error.record_not_found", "en")


def test_status_words_are_translated_with_a_raw_fallback():
    from ui.i18n import item_status_label

    assert item_status_label("archived", "de") == t("enum.item_status.archived", "de")
    assert item_status_label("no_such_status") == "no_such_status"


def _spec_keys() -> list[str]:
    en = json.loads((_LOCALES / "en.json").read_text(encoding="utf-8"))
    return sorted(k for k in en if k.startswith(("inventory.err_", "manufacturing.err_")))


_REWRITTEN = [
    "inv.invalid_json_body", "inv.invalid_quantities_use_commaseparated_numbers",
    "inv.pos_some_failed",
    "inv.target_item_selection_is_required", "inventory.bulk_duplicate_failed", "inventory.bulk_duplicated_partial",
    "inventory.cannot_split_one_piece", "inventory.child_pieces_too_high", "inventory.count_whole_number",
    "inventory.import_failed", "inventory.invalid_numeric_input",
    "inventory.row_gone_reload", "inventory.split_qty_too_high", "inventory.step_gone_reload",
    "settings_inventory.category_not_found", "settings_manufacturing.save_failed", "inv.invalid_split_quantity",
    "inv.split_quantity_must_be_greater_than_0", "inv.quantity_must_be_greater_than_0", "inventory.count_min_two",
    "inv.select_exactly_1_item_to_split", "table.select_exactly_1_to_transform", "inv.select_at_least_2_items_to_merge",
]


@pytest.mark.parametrize("key", [
    "inventory.err_sku_comma", "inventory.err_fields_restricted", "inventory.err_price_derived",
    "inventory.err_merge_linked", "inventory.err_import_not_reversible", "inventory.err_store_product_taken",
    "mfg.recipe_cycle", "manufacturing.err_issue_before_complete", "manufacturing.err_run_closed",
    *_REWRITTEN,
])
def test_rewritten_inventory_messages_exist_in_every_language(key):
    en = json.loads((_LOCALES / "en.json").read_text(encoding="utf-8"))[key]
    for lang in _LANGS:
        value = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8")).get(key)
        assert value, (lang, key)
        assert set(_PLACEHOLDER.findall(value)) == set(_PLACEHOLDER.findall(en)), (lang, key)
        assert "—" not in value, (lang, key)


def test_every_new_error_key_is_in_every_language():
    keys = _spec_keys()
    assert len(keys) >= 90
    for lang in _LANGS:
        cat = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        assert [k for k in keys if not cat.get(k)] == [], lang


def test_rewritten_messages_say_what_to_do():
    assert t("inv.select_exactly_1_item_to_split", "en").startswith("Split works on one item at a time.")
    assert t("inventory.import_failed", "en", detail="x") == "The import didn't finish: x"
    assert t("inv.pos_some_failed", "en").startswith("Draft purchase orders couldn't be created")


@pytest.mark.parametrize("key", [
    "inventory.error_detail", "error.location_col_required", "error.merge_required_fields",
    "error.new_sku_required", "error.sku_required", "error.split_invalid_quantities", "error.split_min_two",
])
def test_replaced_inventory_messages_are_gone(key):
    for lang in _LANGS:
        assert key not in json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8")), (lang, key)
