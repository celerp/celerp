# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Library item categories read in the user's language while they keep their library
name ("Colored Stone" is "Farbedelstein" in German) on the inventory tabs, the dashboard
chart and the category settings; a category the user renamed always shows the rename."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ui import i18n

_ROOT = Path(__file__).resolve().parent.parent
_LIBRARY = _ROOT / "default_modules/celerp-verticals/celerp_verticals/categories"
_DE = {"Accept-Language": "de"}
_TAB_LABEL = re.compile(r'class="category-tab ?[^"]*"[^>]*>([^<(]+?) \(\d+\)</a>')
_CHART = re.compile(r"labels: (\[[^\]]*\]), datasets: \[\{ label: \"[^\"]*\", data: \[[^\]]*\], backgroundColor: colors\[0\]")


async def _gem_company(owner_ui) -> None:
    assert (await owner_ui.api.post("/companies/me/business-type", json={"vertical": "gemstones"})).status_code == 200
    assert (await owner_ui.api.post("/companies/me/demo/reseed")).status_code == 200


async def _tabs_and_chart(owner_ui) -> tuple[list[str], list[str]]:
    inv = await owner_ui.get("/inventory", headers=_DE)
    assert inv.status_code == 200
    dash = await owner_ui.get("/dashboard", headers=_DE)
    m = _CHART.search(dash.text)
    assert m, "the category chart is on the dashboard"
    return _TAB_LABEL.findall(inv.text), json.loads(m.group(1))


@pytest.mark.asyncio
async def test_library_categories_show_in_the_users_language(owner_ui):
    await _gem_company(owner_ui)
    tabs, chart = await _tabs_and_chart(owner_ui)
    assert "Farbedelstein" in tabs and "Farbedelstein" in chart, (tabs, chart)
    assert "Colored Stone" not in tabs + chart, (tabs, chart)
    settings = await owner_ui.get("/settings/inventory?tab=categories", headers=_DE)
    assert "Farbedelstein" in settings.text
    assert ">Colored Stone<" not in settings.text


@pytest.mark.asyncio
async def test_a_renamed_category_shows_its_new_name(owner_ui):
    await _gem_company(owner_ui)
    r = await owner_ui.api.patch("/companies/me/categories/colored_stone", json={"name": "Bunte Steine"})
    assert r.status_code == 200, r.text
    tabs, chart = await _tabs_and_chart(owner_ui)
    assert "Bunte Steine" in tabs and "Bunte Steine" in chart, (tabs, chart)
    assert "Farbedelstein" not in tabs + chart, (tabs, chart)


def _library() -> dict[str, str]:
    return {d["name"]: d["display_name"]
            for d in (json.loads(p.read_text()) for p in sorted(_LIBRARY.glob("*.json")))}


def test_english_category_labels_match_the_library():
    """The library file is authoritative: the English label of every library category is
    its display name, so a category still carrying that name is recognised as untouched."""
    en = i18n._cached_load("en")
    assert {k: en.get(f"category.{k}") for k in _library()} == _library()


@pytest.mark.parametrize("lang", sorted(i18n._DISK_LANGS))
def test_every_library_category_has_a_label_in_every_locale(lang):
    cat = i18n._cached_load(lang)
    assert [k for k in _library() if not cat.get(f"category.{k}")] == []


async def _colored_stone_item(owner_ui) -> str:
    r = await owner_ui.api.post("/items", json={"sku": "CAT-1", "name": "Label Stone", "quantity": 1,
                                                "sell_by": "piece", "category": "colored_stone"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_settings_library_and_rename_form_show_the_users_language(owner_ui):
    await _gem_company(owner_ui)
    settings = await owner_ui.get("/settings/inventory?tab=categories", headers=_DE)
    assert ">Wein<" in settings.text, "the Browse Library rows are translated"
    edit = await owner_ui.get("/settings/categories/colored_stone/edit", headers=_DE)
    assert 'value="Farbedelstein"' in edit.text, "the rename box starts from the name shown, not the key"


@pytest.mark.asyncio
async def test_saving_the_shown_name_is_not_a_rename(owner_ui):
    await _gem_company(owner_ui)
    r = await owner_ui.patch("/settings/categories/colored_stone", data={"new_name": "Farbedelstein"}, headers=_DE)
    assert r.status_code == 200 and "Farbedelstein" in r.text
    names = (await owner_ui.api.get("/companies/me/category-display-names")).json()
    assert names.get("colored_stone") == "Colored Stone", "the category keeps its library name and keeps translating"


@pytest.mark.asyncio
async def test_item_pages_show_the_category_name(owner_ui):
    await _gem_company(owner_ui)
    item_id = await _colored_stone_item(owner_ui)
    detail = await owner_ui.get(f"/inventory/{item_id}", headers=_DE)
    assert detail.status_code == 200
    assert ">Farbedelstein<" in detail.text and ">colored_stone<" not in detail.text
    sheet = await owner_ui.get(f"/inventory/{item_id}/worksheet/print", headers=_DE)
    assert "·  Farbedelstein" in sheet.text, "the worksheet subtitle"
    tf = await owner_ui.get(f"/api/items/bulk/transform-preview?entity_id={item_id}", headers=_DE)
    assert re.search(r'<option value="colored_stone"[^>]*>Farbedelstein</option>', tf.text), "the transform category picker"


@pytest.mark.asyncio
async def test_expiring_report_shows_the_category_name(owner_ui):
    from unittest.mock import AsyncMock, patch
    await _gem_company(owner_ui)
    lines = {"count": 1, "days_threshold": 30,
             "lines": [{"sku": "EXP-1", "name": "Expiring", "category": "colored_stone", "days_remaining": 3}]}
    with patch("ui.api_client.get_expiring", new=AsyncMock(return_value=lines)):
        r = await owner_ui.get("/reports/expiring", headers=_DE)
    assert "<td>Farbedelstein</td>" in r.text


@pytest.mark.asyncio
async def test_bill_line_category_picker_shows_the_category_name(owner_ui):
    await _gem_company(owner_ui)
    r = await owner_ui.api.post("/docs", json={"doc_type": "bill", "line_items": [
        {"description": "Stone", "quantity": 1, "unit_price": 10, "category": "colored_stone"}]})
    assert r.status_code == 200, r.text
    page = await owner_ui.get(f"/docs/{r.json()['id']}", headers=_DE)
    assert re.search(r'<option value="colored_stone"[^>]*>Farbedelstein</option>', page.text), \
        "the bill line's category select"


def test_activity_and_business_type_summary_show_category_names():
    from ui.components.activity import _fmt_field_value, _origin_detail
    from ui.routes.settings import _business_type_change_lines
    i18n.set_lang("de")
    try:
        assert _fmt_field_value("category", "colored_stone", None) == "Farbedelstein"
        assert _origin_detail({"qty": 1, "category": "colored_stone"}, with_category=True).endswith(": Farbedelstein")
        assert "Hinzugefügte Kategorien: Diamant" in _business_type_change_lines(
            {"categories_added": {"diamond": "Diamond"}})
    finally:
        i18n.set_lang("en")


@pytest.mark.asyncio
async def test_a_library_category_with_no_stored_name_still_reads_translated(owner_ui):
    """A schema saved for a library category without a stored name lists under its
    translated library name, not its key."""
    r = await owner_ui.api.patch("/companies/me/category-schema/wine", json={"fields": []})
    assert r.status_code == 200, r.text
    settings = await owner_ui.get("/settings/inventory?tab=categories", headers=_DE)
    assert re.search(r'class="cat-name-display"[^>]*>Wein<', settings.text), "the Your Categories row"


_DETAIL_LABEL = re.compile(r'<td class="detail-label">\s*([^<]+?)\s*<')


def _library_field_labels() -> set[str]:
    return {f["label"] for p in _LIBRARY.glob("*.json") for f in json.loads(p.read_text())["fields"]}


@pytest.mark.asyncio
async def test_library_category_fields_show_in_the_users_language(owner_ui):
    """The attribute fields a library category brings read in the user's language on
    the item page, like the built-in fields beside them."""
    await _gem_company(owner_ui)
    item_id = await _colored_stone_item(owner_ui)
    labels = _DETAIL_LABEL.findall((await owner_ui.get(f"/inventory/{item_id}", headers=_DE)).text)
    assert {"Steinart", "Herkunft", "Behandlung", "Maße (mm)", "Zertifikatsnr."} <= set(labels), labels
    assert not _library_field_labels() & set(labels) - {"Pieces"}, labels


@pytest.mark.asyncio
async def test_a_renamed_category_field_shows_as_typed(owner_ui):
    await _gem_company(owner_ui)
    fields = (await owner_ui.api.get("/companies/me/category-schema/colored_stone")).json()
    renamed = [{**f, "label": "Steinsorte"} if f["key"] == "stone_type" else f for f in fields]
    r = await owner_ui.api.patch("/companies/me/category-schema/colored_stone", json={"fields": renamed})
    assert r.status_code == 200, r.text
    item_id = await _colored_stone_item(owner_ui)
    labels = _DETAIL_LABEL.findall((await owner_ui.get(f"/inventory/{item_id}", headers=_DE)).text)
    assert "Steinsorte" in labels and "Steinart" not in labels and "Herkunft" in labels, labels


@pytest.mark.parametrize("lang", sorted(i18n._DISK_LANGS))
def test_every_library_category_field_has_a_label_in_every_locale(lang):
    cat = i18n._cached_load(lang)
    assert sorted(label for label in _library_field_labels()
                  if not cat.get(i18n.field_label_key(label) or "")) == []


def test_english_category_field_labels_match_the_library():
    """The library files are authoritative: every English attribute label is a library
    field label, so none is left over once a library field is renamed or removed."""
    en = i18n._cached_load("en")
    assert {v for k, v in en.items() if k.startswith("attr.")} <= _library_field_labels()


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.asyncio
async def test_activity_shows_renamed_and_custom_categories_as_named(owner_ui, lang):
    """The activity feed names a category the way the rest of the app does: a renamed
    library category and a company's own category as typed, never their keys. Red
    statement: the feed showed "Category: books_we_like → gear_box"."""
    api = owner_ui.api
    for key in ("beer", "book"):
        assert (await api.post("/companies/me/apply-category", params={"name": key})).status_code == 200
    assert (await api.patch("/companies/me/categories/book", json={"name": "Books we like"})).status_code == 200
    assert (await api.post("/companies/me/categories", json={"name": "Gear Box"})).status_code == 200
    r = await api.post("/items", json={"sku": "ACT-1", "name": "Feed item", "sell_by": "piece",
                                       "quantity": 1, "category": "beer"})
    assert r.status_code in (200, 201), r.text
    item_id = r.json()["id"]
    for old, new in (("beer", "books_we_like"), ("books_we_like", "gear_box")):
        r = await api.patch(f"/items/{item_id}", json={"fields_changed": {"category": {"old": old, "new": new}}})
        assert r.status_code == 200, r.text
    headers = {"Accept-Language": lang}
    for url in (f"/inventory/{item_id}/history", f"/inventory/{item_id}?tab=activity", "/history", "/dashboard"):
        text = (await owner_ui.get(url, headers=headers)).text
        assert "books_we_like →" not in text and "→ gear_box" not in text, url
        assert re.search(r"Books we like → Gear Box", text), url
        assert re.search(rf"{'Bier' if lang == 'de' else 'Beer'} → Books we like", text), url
