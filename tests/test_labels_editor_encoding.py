# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The label template editor shows a field's key and label exactly as they were typed.

Each value is encoded once for the attribute that holds it, so `&`, `"` and `<` read back
unchanged, never decoded a second time or shown with entity text.
"""
from __future__ import annotations

from html.parser import HTMLParser

from fasthtml.common import Div

from celerp_labels.ui_routes import _field_row_compact
from ui.components.encode import to_markup


class _Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        for name in ("fld-key", "fld-label"):
            if tag == "input" and name in cls.split():
                self.values[name] = a.get("value")
        if "searchable-select" in cls.split() and "data-value" in a:
            self.values["data-value"] = a["data-value"]


def test_labels_editor_values_single_encoded():
    key, label = "&lt;b&gt;", 'a"b<c &amp; d'
    parser = _Inputs()
    parser.feed(str(to_markup(Div(_field_row_compact(0, {"key": key, "label": label})))))
    assert parser.values == {"fld-key": key, "data-value": key, "fld-label": label}
