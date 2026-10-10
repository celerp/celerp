# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The inventory row menu's Delete confirm says what happens to the drafts it deletes."""
from __future__ import annotations

from fasthtml.common import to_xml

from ui import i18n
from ui.components.table import data_row


def _row_menu_delete(status: str) -> str:
    return to_xml(data_row({"id": "item:d1", "sku": "D1", "status": status}, [{"key": "sku", "label": "SKU"}],
                           entity_type="inventory"))


def test_the_row_menu_delete_says_what_happens_to_a_draft():
    try:
        html = _row_menu_delete("draft")
        assert ("Drafts nothing else uses are erased. Drafts that other records use move to Deleted, "
                "where you can restore them.") in html
        assert "cannot be undone" not in html
        i18n.set_lang("fr")
        # The apostrophe in the French text is escaped for the single-quoted confirm() argument.
        assert "Les brouillons que rien d\\'autre n\\'utilise sont effacés." in _row_menu_delete("draft")
    finally:
        i18n.set_lang("en")
