# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Import and add-company pages read clearly and always lead somewhere sensible.

The add-company chooser offers the sample company; import pages go back to the
list they belong to; every import target is named in the reader's language; the
migration upload says which file each system gives."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from test_import_invariants import _company, stage_dir  # noqa: F401
from ui.i18n import _current_lang, t
from ui.routes import csv_import as ci

_LOCALES = Path(__file__).resolve().parent.parent / "ui" / "locales"


async def _ui_request(method: str, path: str, **kwargs):
    from ui.app import app as ui_app
    company = _company({})
    with patch("ui.routes.auth.api_get_company", new=AsyncMock(return_value=company)), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value=company)):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            return await c.request(method, path, cookies={"celerp_token": make_test_token(role="owner")}, **kwargs)


# ── N4: adding a company, and the company details form ───────────────────────

async def test_add_company_chooser_offers_the_sample_company():
    """RED before the change: the add-company chooser had no sample company."""
    r = await _ui_request("GET", "/setup/new-company")
    assert r.status_code == 200, r.text
    assert t("setup.card_sample") in r.text
    assert 'action="/setup/new-company/migrate/sample"' in r.text


def test_retail_is_a_business_type():
    """RED before the change: a general shop had no business type of its own."""
    from ui.routes.setup import business_type_options
    assert "retail" in {value for value, _ in business_type_options()}


# ── N2: import pages go back to their list ────────────────────

def test_upload_page_back_goes_to_its_list():
    link = to_xml(ci.import_back_link("/docs"))
    assert 'href="/docs"' in link and t("btn.back") in link


@pytest.mark.parametrize("path,home", [("/crm/import/contacts", "/contacts/customers"), ("/docs/import", "/docs")])
async def test_upload_pages_go_back_to_their_list(path, home):
    r = await _ui_request("GET", path)
    assert r.status_code == 200, r.text
    assert f'href="{home}"' in r.text


async def test_document_upload_error_keeps_the_page_header(stage_dir):  # noqa: F811
    """RED before the change: a failed document upload lost Back and the template link."""
    r = await _ui_request("POST", "/docs/import/preview",
                          files={"csv_file": ("orders.csv", io.BytesIO(b""), "text/csv")})
    assert r.status_code == 200, r.text
    assert 'href="/docs"' in r.text and 'href="/docs/import/template"' in r.text


async def test_product_mapping_cancel_returns_to_the_upload_page(stage_dir):  # noqa: F811
    with patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[])), \
         patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})):
        r = await _ui_request("POST", "/inventory/import/preview",
                              files={"csv_file": ("stock.csv", io.BytesIO(b"Name,Qty\nBasket,4\n"), "text/csv")})
    assert r.status_code == 200, r.text
    assert 'href="/inventory/import"' in r.text


# ── N7: the migration upload says which file each system gives ────────────────

_SOURCES = [{"key": "manager_io", "display_name": "Manager.io",
             "artifacts": [{"label": "Manager.io backup", "extensions": [".manager"]}]}]


async def test_migration_upload_names_the_accepted_file_and_where_to_find_it():
    """RED before the change: the upload named no file type and asked for several files."""
    with patch("ui.api_client.migration_sources", new=AsyncMock(return_value=_SOURCES)):
        r = await _ui_request("GET", "/setup/new-company/migrate")
    assert r.status_code == 200, r.text
    assert t("migration.accepted_files", files="Manager.io backup (.manager)") in r.text
    assert t("migration.export_help.manager_io") in r.text
    field = re.search(r'<input[^>]*id="files"[^>]*>', r.text).group(0)
    assert "multiple" not in field


# ── N5 / N6: import targets named in the reader's language ────────────────────

def _labels(lang: str, cat_schemas: dict | None = None) -> dict[str, str]:
    from ui.routes.inventory import _import_field_labels
    token = _current_lang.set(lang)
    try:
        return _import_field_labels([], cat_schemas)
    finally:
        _current_lang.reset(token)


def test_item_fields_use_their_translated_labels():
    """RED before the change: targets were field keys title-cased ('Sell By', 'Rfid Epc')."""
    from ui.routes.inventory import _IMPORT_SPEC
    labels = _labels("en")
    for key in _IMPORT_SPEC.cols:
        if key in labels:
            assert labels[key] != key.replace("_", " ").title() or labels[key] == t(f"field.label.{key}")
    assert labels["sell_by"] == "Sell Unit" and labels["rfid_epc"] == "RFID / EPC"


def test_item_fields_read_in_thai():
    labels = _labels("th")
    assert (labels["name"], labels["category"], labels["notes"]) == ("ชื่อ", "หมวดหมู่", "หมายเหตุ")


def test_a_category_field_repeating_a_built_in_field_is_offered_once():
    """RED before the change: a category's 'pieces' field showed a second 'Pieces' target."""
    from ui.routes.inventory import _IMPORT_SPEC
    schemas = {"Baskets": [{"key": "pieces", "label": "Pieces"}, {"key": "weave", "label": "Weave"}]}
    out = to_xml(ci.column_mapping_form(
        csv_cols=["Pcs"], target_cols=_IMPORT_SPEC.cols, csv_ref="r", sample_rows=[{"Pcs": "1"}],
        confirm_action="/inventory/import/mapped", back_href="/inventory/import",
        category_attrs=["pieces", "weave"], col_labels=_labels("en", schemas)))
    assert len(re.findall(r'"label": "Pieces"', out)) == 1
    assert '"label": "Weave"' in out


def _locale(lang: str) -> dict:
    return json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("module,name", [("ui.routes.contacts", "_CONTACT_IMPORT_LABEL_KEYS"),
                                         ("ui.routes.docs_import", "_DOC_IMPORT_LABEL_KEYS")])
def test_contact_and_document_targets_are_translated_everywhere(module, name):
    """RED before the change: contact and document targets had no labels at all."""
    import importlib
    keys = getattr(importlib.import_module(module), name)
    for path in sorted(_LOCALES.glob("*.json")):
        missing = [k for k in keys.values() if k not in _locale(path.stem)]
        assert missing == [], (path.stem, missing)


def test_thai_map_columns_reads_as_matching_columns():
    """RED before the change: the Thai title read 'map (cartography) columns'."""
    th = _locale("th")
    assert th["page.map_columns"] == "จับคู่คอลัมน์"
    assert "แผนที่" not in th["import.step_map_columns"]
