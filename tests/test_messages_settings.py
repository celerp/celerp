# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Settings and company messages say what went wrong and what to do, in the user's language."""
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
        "company_name": "SetMsgCo", "email": f"set-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_blank_category_name_asks_for_a_name(client):
    r = await client.post("/companies/me/categories", headers=await _headers(client), json={"name": "  "})
    assert r.status_code == 422
    assert r.json()["detail"] == t("settings.name_required", "en") == "Enter a name."


@pytest.mark.asyncio
async def test_category_name_without_letters_says_add_one(client):
    r = await client.post("/companies/me/categories", headers=await _headers(client), json={"name": "!!!"})
    assert r.status_code == 422
    assert r.json()["detail"] == t("company.err_name_no_letters", "en")


@pytest.mark.asyncio
async def test_missing_category_is_named(client):
    r = await client.patch("/companies/me/categories/no_such_cat", headers=await _headers(client),
                           json={"name": "Rings"})
    assert r.status_code == 404
    assert r.json()["detail"] == t("company.err_category_not_found", "en", name="no_such_cat")


@pytest.mark.asyncio
async def test_missing_location_says_refresh(client):
    r = await client.delete(f"/companies/me/locations/{uuid.uuid4()}", headers=await _headers(client))
    assert r.status_code == 404
    assert r.json()["detail"] == t("company.err_location_not_found", "en")


@pytest.mark.asyncio
async def test_bad_unit_name_and_decimals_name_the_unit(client):
    h = await _headers(client)
    r = await client.put("/companies/me/units", headers=h,
                         json={"units": [{"name": "Big Box", "label": "Box", "decimals": 0}]})
    assert r.status_code == 422
    assert r.json()["detail"] == t("company.err_unit_name", "en", name="Big Box")
    r = await client.put("/companies/me/units", headers=h,
                         json={"units": [{"name": "box", "label": "Box", "decimals": 9}]})
    assert r.status_code == 422
    assert r.json()["detail"] == t("company.err_unit_decimals", "en", name="box")


@pytest.mark.asyncio
async def test_company_messages_follow_the_users_language(client):
    h = {**await _headers(client), "Accept-Language": "th"}
    r = await client.post("/companies/me/categories", headers=h, json={"name": "  "})
    assert r.status_code == 422
    assert r.json()["detail"] == t("settings.name_required", "th") == "ป้อนชื่อ"


def test_missing_permission_names_it_as_the_role_editor_does():
    from fastapi import HTTPException
    from celerp.services.permissions import assert_role_permission

    with pytest.raises(HTTPException) as exc:
        assert_role_permission({}, "operator", "set_inventory_prices")
    assert exc.value.status_code == 403
    assert exc.value.detail == t("error.permission_missing", "en", label="Set inventory prices")
    assert "Global Config, Users" in exc.value.detail and "set_inventory_prices" not in exc.value.detail


def test_failed_update_step_says_it_stopped_without_its_output():
    from celerp.services import update

    with pytest.raises(update.UpdateError) as exc:
        update._step("-c", "import sys; sys.exit(3)")
    assert str(exc.value).endswith("stopped with an error (code 3)")


@pytest.mark.parametrize("key", [
    "error.permission_missing", "error.period_locked", "settings.reset_name_mismatch",
    "settings.reset_not_done", "company_backup.err_not_a_backup", "company.err_permission_floor",
    "connectors.err_relay_not_https", "system_recovery.safety_failed",
])
def test_rewritten_settings_messages_exist_in_every_language(key):
    en = json.loads((_LOCALES / "en.json").read_text(encoding="utf-8"))[key]
    for lang in _LANGS:
        value = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8")).get(key)
        assert value, (lang, key)
        assert set(_PLACEHOLDER.findall(value)) == set(_PLACEHOLDER.findall(en)), (lang, key)
        assert "—" not in value and "⚙" not in value, (lang, key)


@pytest.mark.parametrize("key", [
    "error.unauthorized", "error.all_fields_required", "settings.could_not_reach_api",
    "settings.cannot_demote_the_last_owner_assign_another_owner", "settings.cant_delete_category_in_use",
    "settings_cloud.config_write_failed", "settings.relay_url_must_use_https_set_celerpallowhttprelay1",
])
def test_replaced_settings_messages_are_gone(key):
    for lang in _LANGS:
        assert key not in json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8")), (lang, key)


def test_save_failures_do_not_show_the_raw_error():
    for key in ("settings_cloud.save_failed", "settings_cloud.restore_failed"):
        assert "{err}" not in t(key, "en") and "settings folder" in t(key, "en")
