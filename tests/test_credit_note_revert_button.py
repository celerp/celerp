# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Revert to draft is offered where the API takes it: on a credit note settled by its
credit alone, never on an invoice an issued credit note settled, which goes back to draft
only after that credit note does."""

from __future__ import annotations

from fasthtml.common import to_xml

from ui.routes.documents import _doc_detail

_REVERT = "/action/revert_to_draft"


def _doc(doc_type: str, **fields) -> dict:
    return {"entity_id": "doc:r1", "id": "doc:r1", "doc_type": doc_type, "line_items": [], "total": 80.0,
            "amount_paid": 0.0, **fields}


def test_an_invoice_settled_by_a_credit_note_offers_no_revert():
    html = to_xml(_doc_detail(_doc("invoice", status="partial", amount_outstanding=40.0, credited=40.0)))
    assert _REVERT not in html


def test_a_credit_note_settled_by_its_credit_offers_revert():
    html = to_xml(_doc_detail(_doc("credit_note", status="paid", amount_outstanding=0.0, credited=40.0,
                                   original_doc_id="doc:i1")))
    assert _REVERT in html


def test_an_issued_invoice_with_nothing_settled_offers_revert():
    html = to_xml(_doc_detail(_doc("invoice", status="final", amount_outstanding=80.0)))
    assert _REVERT in html
