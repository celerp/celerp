# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the shared files section component.

Proves ui.components.files.files_section() resolves every user-facing string via
t() at render time (never at import time), including the values fed into static
JS through data-* attributes (R2) rather than spliced into the script source.
Registers a sentinel language "xx" via the module i18n seam and asserts the
sentinel text reaches the rendered output while "xx" is the active language.
Red against a tree that hardcodes English for any of these strings.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.files import files_section

# Sentinel catalog: one unmistakable value per key the files section renders.
_XX = {
    "th.filename": "XX_FILENAME",
    "th.hero": "XX_HERO",
    "th.size": "XX_SIZE",
    "label.tag": "XX_TAG",
    "label.linked_to": "XX_LINKED_TO",
    "label.files": "XX_FILES_TITLE",
    "label.no_files_yet": "XX_NO_FILES",
    "label.all_tags": "XX_ALL_TAGS",
    "label.search": "XX_SEARCH",
    "label.no_tag": "XX_NO_TAG",
    "label.file_description": "XX_DESCRIPTION",
    "label.dblclick_to_edit": "XX_DBLCLICK",
    "label.upload_date": "XX_UPLOAD_DATE",
    "label.hero_image": "XX_HERO_IMAGE",
    "action.set_hero_image": "XX_SET_HERO",
    "action.delete_file": "XX_DELETE_FILE",
    "msg.drop_files_here": "XX_DROP_HERE",
    "msg.uploading": "XX_UPLOADING",
    "msg.upload_failed": "XX_UPLOAD_FAILED",
    "file_tag.bills": "XX_TAG_BILLS",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the registry
    and the context language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def _render():
    files = [
        {
            "id": "f1",
            "filename": "invoice.png",
            "size": 2048,
            "document_tag": "bills",
            "description": "",
            "uploaded_at": "2026-01-01T00:00:00Z",
            "mime": "image/png",
            "is_hero": True,
        },
        {
            "id": "f2",
            "filename": "note.png",
            "size": 4096,
            "document_tag": "bills",
            "description": "",
            "uploaded_at": "2026-01-02T00:00:00Z",
            "mime": "image/png",
            "is_hero": False,
        },
    ]
    return files_section(
        "contact",
        "c1",
        files,
        can_set_hero=True,
    )


def test_table_header_labels_translate():
    html = to_xml(_render())
    assert "XX_FILENAME" in html
    assert "XX_HERO" in html
    assert "XX_SIZE" in html
    assert "XX_TAG" in html
    assert "XX_LINKED_TO" in html


def test_section_title_and_filter_bar_translate():
    html = to_xml(_render())
    assert "XX_FILES_TITLE" in html
    assert "XX_ALL_TAGS" in html
    assert "XX_SEARCH" in html


def test_tag_module_dict_label_translates():
    """_tag_label() resolves file_tag.<slug> at render time (module-dict mechanism)."""
    html = to_xml(_render())
    assert "XX_TAG_BILLS" in html


def test_hero_toggle_titles_translate():
    """Covers both branches of the hero button title (mined action/label keys)."""
    html = to_xml(_render())
    assert "XX_HERO_IMAGE" in html
    assert "XX_SET_HERO" in html


def test_delete_button_translates():
    html = to_xml(_render())
    assert "XX_DELETE_FILE" in html


def test_dropzone_js_data_attrs_translate():
    """R2: drop_js reads translated text from data-* attributes rather than
    splicing it into the JS source. Assert the sentinel values reach the
    rendered attributes that the script reads via zone.dataset.*."""
    html = to_xml(_render())
    assert "XX_DROP_HERE" in html
    assert "XX_UPLOADING" in html
    assert "XX_UPLOAD_FAILED" in html
    # And the JS source itself must not contain a hardcoded English string -
    # it reads from dataset instead.
    assert "zone.dataset.uploadingText" in html
    assert "zone.dataset.uploadFailedText" in html
    assert "zone.dataset.dropText" in html


def test_empty_state_translates():
    html = to_xml(files_section("contact", "c2", [], can_set_hero=True))
    assert "XX_NO_FILES" in html
