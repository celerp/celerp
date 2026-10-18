# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A draft's line links survive the document page's saves.

The page rebuilds every line from its inputs on each save, so a link the page never edits
(the credited invoice line, the asset a line sells or credits) is carried as a hidden input
and sent back as stored. Without it, editing a draft credit note dropped its asset and the
note finalized past the sale's checks.
"""
from __future__ import annotations

from fasthtml.common import to_xml


def _page(line: dict) -> str:
    from ui.routes.documents import _doc_detail
    doc = {"entity_id": "doc:CN-1", "doc_type": "credit_note", "status": "draft", "ref_id": "CN-1",
           "original_doc_id": "doc:INV-1", "line_items": [line]}
    return to_xml(_doc_detail(doc, role="owner"))


def test_draft_line_carries_its_asset_and_source_line():
    html = _page({"line_id": "l1", "description": "FA-0001 Car", "quantity": 1, "unit_price": 900,
                  "asset_id": "fa:asset:1", "source_line_id": "s1", "asset_returned": True})
    assert 'value="fa:asset:1" data-carry="asset_id"' in html
    assert 'value="s1" data-carry="source_line_id"' in html
    assert 'value="true" data-carry="asset_returned"' in html
    assert "...carried," in html


def test_plain_line_carries_nothing():
    html = _page({"line_id": "l1", "description": "Service", "quantity": 1, "unit_price": 900})
    assert 'data-carry="' not in html
