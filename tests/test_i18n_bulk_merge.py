# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The bulk-action script on the inventory table speaks the reader's language.

The merge confirmation, its buttons and the stock-guard choice are built in the
browser, so their words travel inside the rendered script. In German none of the
English source strings may survive, and the merge question reads as German
("2 Artikel", never "2-Artikel").
"""
import json

from fasthtml.common import to_xml

from ui import i18n
from ui.components.table import data_table

_ENGLISH = ("'Merge '", "'a new SKU'", "'Confirm'", "'Cancel'", "'Keep stock on books'",
            "'Write off remaining stock'", "' selected'")


def _script(lang: str) -> str:
    i18n.set_lang(lang)
    try:
        return to_xml(data_table([{"key": "sku", "label": "SKU"}], [{"id": "item:1", "sku": "A"}]))
    finally:
        i18n.set_lang("en")


def test_the_merge_confirmation_is_written_in_the_readers_language():
    html = _script("de")
    for english in _ENGLISH:
        assert english not in html, english
    for german in ("{n} Artikel in {target} zusammenführen?", "Bestätigen", "Abbrechen"):
        assert json.dumps(german) in html, german


def test_the_merge_question_puts_the_count_before_the_items_in_every_language():
    """ "{n} items", never "the items {n}": the count is read as a number of items."""
    import pathlib
    for path in sorted(pathlib.Path("ui/locales").glob("*.json")):
        text = json.loads(path.read_text())["table.confirm_merge"]
        before = text.split("{n}")[0]
        assert not before.rstrip().endswith(("articles", "artículos", "articoli", "itens", "item",
                                             "mặt hàng", "عناصر", "Artikel", "-")), (path.name, text)
