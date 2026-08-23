# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the shared notes tab and phone input
components. Both modules build every user-facing label by calling ``t()`` at
render time, so a request in a non-English language gets translated output.
These tests prove that by registering a sentinel language ``xx`` (via the
module i18n seam) and asserting the sentinel text reaches the rendered
output while ``xx`` is the active language. They are red against a tree that
resolves any of these strings at import time or hardcodes English.
"""

import pytest
from datetime import datetime, timedelta, timezone

from fasthtml.common import to_xml

from ui import i18n
from ui.components.notes import notes_tab, note_edit_form
from ui.components.phone import phone_input_td

# Sentinel catalog: one unmistakable value per key these components render.
_XX = {
    "btn.add_note": "XX_NOTES_ADD",
    "label.add_note_placeholder": "XX_NOTES_PLACEHOLDER",
    "label.no_notes_yet": "XX_NOTES_EMPTY",
    "btn.edit": "XX_NOTES_EDIT",
    "btn.delete": "XX_NOTES_DELETE",
    "confirm.delete_note": "XX_NOTES_CONFIRM_DELETE",
    "label.note_edited_suffix": " XX_NOTES_EDITED {ts}",
    "btn.save": "XX_PHONE_SAVE",
    "btn.cancel": "XX_PHONE_CANCEL",
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


def test_notes_tab_empty_state_translates():
    html = to_xml(notes_tab(
        entity_id="c1",
        notes=[],
        add_url="/notes/add",
        edit_url="/notes/{note_id}/edit",
        delete_url="/notes/{note_id}",
        refresh_target="#notes-tab",
    ))
    assert "XX_NOTES_ADD" in html
    assert "XX_NOTES_PLACEHOLDER" in html
    assert "XX_NOTES_EMPTY" in html


def test_notes_tab_with_notes_translates_actions_and_edited_suffix():
    created = datetime.now(timezone.utc) - timedelta(days=2)
    updated = datetime.now(timezone.utc) - timedelta(hours=1)
    notes = [{
        "note_id": "n1",
        "note": "Called the customer.",
        "author_name": "Tester",
        "created_at": created.isoformat(),
        "updated_at": updated.isoformat(),
    }]
    html = to_xml(notes_tab(
        entity_id="c1",
        notes=notes,
        add_url="/notes/add",
        edit_url="/notes/{note_id}/edit",
        delete_url="/notes/{note_id}",
        refresh_target="#notes-tab",
    ))
    assert "XX_NOTES_EDIT" in html
    assert "XX_NOTES_DELETE" in html
    assert "XX_NOTES_CONFIRM_DELETE" in html
    assert "XX_NOTES_EDITED" in html


def test_note_edit_form_translates():
    html = to_xml(note_edit_form(
        note_id="n1",
        current_text="Called the customer.",
        save_url="/notes/n1",
        cancel_url="/notes/n1/cancel",
        refresh_target="#notes-tab",
    ))
    assert "XX_PHONE_SAVE" in html
    assert "XX_PHONE_CANCEL" in html


def test_phone_input_td_translates_actions():
    html = to_xml(phone_input_td(
        value="+66812345678",
        patch_url="/contacts/c1/phone",
        cancel_url="/contacts/c1/phone/cancel",
    ))
    assert "XX_PHONE_SAVE" in html
    assert "XX_PHONE_CANCEL" in html
