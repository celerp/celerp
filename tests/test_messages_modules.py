# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Module and marketplace messages say what went wrong and what to do next."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from celerp.modules import importer
from celerp.modules.importer import ModuleImportError
from ui.i18n import set_lang, t

_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"


async def _headers(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "ModMsgCo", "email": f"mod-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_a_file_that_is_not_a_zip_says_choose_or_download_again():
    with pytest.raises(ModuleImportError) as e:
        importer.install_from_zip(b"not a zip")
    assert str(e.value) == t("module_import.not_zip", "en")


def test_a_path_that_is_not_a_folder_says_choose_the_folder(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ModuleImportError) as e:
        importer.install_from_folder(f)
    assert str(e.value) == t("module_import.not_a_folder", "en")


def test_removing_a_module_that_is_not_installed_names_it(tmp_path, monkeypatch):
    monkeypatch.setenv("MODULE_DIR", str(tmp_path))
    with pytest.raises(ModuleImportError) as e:
        importer.remove_module_dir("acme-widgets")
    assert str(e.value) == t("module_import.not_installed", "en", name="acme-widgets")


def test_importer_messages_follow_the_readers_language():
    set_lang("th")
    try:
        with pytest.raises(ModuleImportError) as e:
            importer.install_from_zip(b"not a zip")
    finally:
        set_lang("en")
    assert str(e.value) == t("module_import.not_zip", "th")


@pytest.mark.asyncio
async def test_unknown_business_type_says_choose_another(client):
    r = await client.post("/companies/me/apply-preset", params={"vertical": "nope"},
                          headers=await _headers(client))
    assert r.status_code == 404
    assert r.json()["detail"] == t("error.business_type_unavailable", "en", vertical="nope")


def test_unused_restart_key_is_gone():
    en = json.loads((_LOCALES / "en.json").read_text(encoding="utf-8"))
    assert "msg.restart_required" not in en


@pytest.mark.parametrize("key, says", [
    ("module_import.name_invalid", "Ask the module's developer for a fixed version."),
    ("module_import.manifest_bad", "description is built incorrectly"),
    ("module_import.multiple_modules", "Install them one at a time"),
    ("module_import.no_backup_rule", "doesn't say how its data is backed up"),
    ("module_import.no_module_dir", "Ask whoever installed Celerp to set a module folder."),
    ("module_import.already_installed", "Remove it first, then install this one."),
    ("module_import.write_failed", "Check that there is free disk space"),
    ("module_import.unsafe", "Only install modules from developers you trust"),
    ("module_import.too_large", "Ask the module's developer for a smaller version."),
    ("modules.delete_failed", "Restart Celerp and try again."),
    ("marketplace.from_cache", "Check your internet connection and reload the page."),
    ("marketplace.import_failed", "Download it again and retry"),
    ("modules.required_by", "Disable those first."),
    ("setup.modules_failed_to_start", "Open the Modules page"),
    ("modules.load_failed", "or turn the module off."),
    ("modules.not_reported", "gave no reason"),
    ("error.category_not_found", "It may have been deleted. Refresh the page."),
])
def test_module_messages_say_what_to_do(key, says):
    assert says in t(key, "en", name="X", label="X", error="E", exc="E", names="X")


def test_a_module_that_did_not_report_shows_the_plain_reason():
    from celerp.modules.outcome import NOT_REPORTED
    from ui.routes.modules_page import _load_error_message
    assert _load_error_message("Acme", NOT_REPORTED, "en") == t("modules.not_reported", "en", label="Acme")
    assert _load_error_message("Acme", "boom", "en") == t("modules.load_failed", "en", label="Acme", error="boom")
