# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A draft row carries how much of its line is out only where goods can be out on it.

The quantity a memo return may take back is read from the row. Rows on documents that never
send goods out (bills, purchase orders, consignments in, quotations) carry no such figure.
"""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml


def _page(doc_type: str) -> str:
    from ui.routes import documents

    doc = {"id": f"doc:{doc_type}", "entity_id": f"doc:{doc_type}", "doc_type": doc_type, "status": "draft",
           "line_items": [{"sku": "OQ-1", "description": "Ring", "quantity": 3, "unit_price": 10}]}
    return to_xml(documents._doc_detail(doc))


@pytest.mark.parametrize("doc_type", ["bill", "purchase_order", "consignment_in", "quotation"])
def test_non_outbound_draft_rows_carry_no_out_quantity(doc_type):
    page = _page(doc_type)
    assert "li-select" in page
    assert "data-out-qty" not in page


@pytest.mark.parametrize("doc_type", ["memo", "invoice"])
def test_outbound_draft_rows_carry_the_out_quantity(doc_type):
    assert 'data-out-qty="3"' in _page(doc_type)
