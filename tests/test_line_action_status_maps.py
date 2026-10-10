# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The document page offers a line action exactly where the API accepts it.

The page reads the same status maps the API enforces, so the two cannot drift: every
doc type and status the page offers Set as shipped, Set as available or Set as reserved
for is one the API allows, and every one the API allows is offered."""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml

_STATUSES = ("draft", "sent", "final", "partial", "paid", "awaiting_payment", "received",
             "partially_received", "partial_returned", "closed", "void", "converted")
_DOC_TYPES = ("invoice", "memo", "quotation", "bill", "consignment_in", "purchase_order")


@pytest.mark.parametrize("doc_type", _DOC_TYPES)
def test_page_line_actions_match_the_api_status_maps(doc_type):
    from celerp_docs.doc_constants import FULFILLABLE_STATUSES, RESERVABLE_DOC_STATUSES, REVERTIBLE_STATUSES
    from ui.routes.documents import _doc_line_actions
    for status in _STATUSES:
        acts = _doc_line_actions(doc_type, status)
        assert acts.fulfil == (status in FULFILLABLE_STATUSES.get(doc_type, ())), (doc_type, status)
        assert acts.revert == (status in REVERTIBLE_STATUSES.get(doc_type, ())), (doc_type, status)
        assert acts.reserve == (status in RESERVABLE_DOC_STATUSES.get(doc_type, ())), (doc_type, status)


def _page(status: str) -> str:
    from ui.routes.documents import _doc_detail
    doc = {"entity_id": "doc:memo-1", "doc_type": "memo", "status": status, "ref_id": "M-1",
           "line_items": [{"line_id": "11111111-1111-4111-8111-111111111111", "sku": "S-1",
                           "item_id": "item:1", "quantity": 1, "unit_price": 1}]}
    return to_xml(_doc_detail(doc))


def test_a_closed_memo_offers_no_take_back_and_a_sent_one_does():
    assert 'id="li-bulk-revert-btn"' in _page("sent")
    assert 'id="li-bulk-revert-btn"' not in _page("closed")
