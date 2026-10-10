# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A draft row carries no out quantity on any document.

Set as available and take back read each line's quantity from its own field, so no row
carries a separate figure for how much of it is out.
"""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml


def _page(doc_type: str) -> str:
    from ui.routes import documents

    doc = {"id": f"doc:{doc_type}", "entity_id": f"doc:{doc_type}", "doc_type": doc_type, "status": "draft",
           "line_items": [{"sku": "OQ-1", "description": "Ring", "quantity": 3, "unit_price": 10}]}
    return to_xml(documents._doc_detail(doc))


@pytest.mark.parametrize("doc_type", ["bill", "purchase_order", "consignment_in", "quotation", "memo", "invoice"])
def test_draft_rows_carry_no_out_quantity(doc_type):
    page = _page(doc_type)
    assert "li-select" in page
    assert "data-out-qty" not in page
