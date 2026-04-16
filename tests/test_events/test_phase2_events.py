# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from celerp_contacts.projections import apply_contact_event
from celerp_docs.doc_projections import apply_documents_event


def test_document_payment_and_conversion_flow() -> None:
    s = apply_documents_event({}, "doc.created", {"doc_type": "invoice", "total": 100})
    s = apply_documents_event(s, "doc.sent", {"sent_via": "email"})
    assert s["status"] == "sent"

    s = apply_documents_event(s, "doc.payment.received", {"amount": 30})
    assert s["status"] == "partial"
    assert s["amount_outstanding"] == 70

    s = apply_documents_event(s, "doc.payment.refunded", {"amount": 10})
    assert s["amount_paid"] == 20
    assert s["amount_outstanding"] == 80

    s = apply_documents_event(s, "doc.converted", {"target_doc_id": "doc:INV-1", "target_doc_type": "invoice"})
    assert s["converted_to"] == "doc:INV-1"


def test_document_received_status_partial_vs_full() -> None:
    s = apply_documents_event({}, "doc.created", {"doc_type": "purchase_order", "line_items": [{"quantity": 2}, {"quantity": 1}]})
    s = apply_documents_event(s, "doc.received", {"location_id": "loc", "received_items": [{"po_line_index": 0, "quantity_received": 2}]})
    assert s["status"] == "partially_received"
    s = apply_documents_event(s, "doc.received", {"location_id": "loc", "received_items": [{"po_line_index": 1, "quantity_received": 1}]})
    assert s["status"] == "received"


def test_crm_memo_invoiced_and_returned() -> None:
    s = apply_contact_event({}, "crm.memo.created", {"contact_id": "contact:1"})
    s = apply_contact_event(s, "crm.memo.invoiced", {"doc_id": "doc:1", "items_invoiced": ["item:1"]})
    assert s["status"] == "invoiced"
    s = apply_contact_event(s, "crm.memo.returned", {"items_returned": [{"item_id": "item:1"}]})
    assert s["status"] == "returned"
