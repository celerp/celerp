# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from celerp_contacts.projections import apply_contact_event
from celerp_docs.doc_projections import apply_documents_event
from celerp_manufacturing.projection_handler import apply_manufacturing_event


def test_documents_all_new_branches():
    s = apply_documents_event({}, "doc.created", {"doc_type": "invoice", "total": 100})
    assert s["status"] == "draft"
    s = apply_documents_event(s, "doc.updated", {"fields_changed": {"notes": {"old": None, "new": "n"}}})
    assert s["notes"] == "n"
    s = apply_documents_event(s, "doc.linked", {"entity_id": "item:1", "entity_type": "item"})
    assert s["linked"][0]["entity_id"] == "item:1"
    s = apply_documents_event(s, "doc.sent", {"sent_via": "email", "sent_to": "a@b"})
    assert s["status"] == "sent"
    s = apply_documents_event(s, "doc.finalized", {})
    assert s["status"] == "final"
    s = apply_documents_event(s, "doc.payment.received", {"amount": 40})
    assert s["status"] == "partial"
    s = apply_documents_event(s, "doc.payment.refunded", {"amount": 10})
    assert s["amount_paid"] == 30
    s = apply_documents_event(s, "doc.converted", {"target_doc_id": "doc:2", "target_doc_type": "invoice"})
    assert s["status"] == "converted"
    s = apply_documents_event(s, "doc.voided", {"reason": "x"})
    assert s["status"] == "void"


def test_documents_received_partial_and_full_branches():
    s = apply_documents_event({}, "doc.created", {"doc_type": "purchase_order", "line_items": [{"quantity": 2}, {"quantity": 2}]})
    s = apply_documents_event(s, "doc.received", {"location_id": "loc", "received_items": [{"po_line_index": 0, "quantity_received": 2}]})
    assert s["status"] == "partially_received"
    s = apply_documents_event(s, "doc.received", {"location_id": "loc", "received_items": [{"po_line_index": 1, "quantity_received": 2}]})
    assert s["status"] == "received"


def test_crm_new_branches():
    s = apply_contact_event({}, "crm.memo.created", {"contact_id": "contact:1"})
    s = apply_contact_event(s, "crm.memo.invoiced", {"doc_id": "doc:1", "items_invoiced": ["item:1"]})
    assert s["status"] == "invoiced"
    assert s["doc_id"] == "doc:1"
    s = apply_contact_event(s, "crm.memo.returned", {"items_returned": [{"item_id": "item:1"}]})
    assert s["status"] == "returned"
    assert len(s["returned_items"]) == 1


def test_manufacturing_completed_metadata_branches():
    s = apply_manufacturing_event({}, "mfg.order.created", {"description": "d"})
    s = apply_manufacturing_event(s, "mfg.order.completed", {"actual_outputs": [{"sku": "X"}], "waste": {"quantity": 1}, "labor_hours": 2})
    assert s["status"] == "completed"
    assert s["actual_outputs"][0]["sku"] == "X"
    assert s["waste"]["quantity"] == 1
    assert s["labor_hours"] == 2
