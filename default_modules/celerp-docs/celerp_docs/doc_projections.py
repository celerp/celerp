# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from copy import deepcopy


def _recalc_list_totals(state: dict) -> dict:
    """Recompute subtotal, discount_amount, tax_amount, total from line_items + header fields."""
    items = state.get("line_items", [])
    subtotal = sum(float(i.get("line_total", 0) or 0) for i in items)
    state["subtotal"] = subtotal

    discount = float(state.get("discount", 0) or 0)
    discount_type = state.get("discount_type", "flat")
    discount_amount = subtotal * discount / 100 if discount_type == "percentage" else discount
    state["discount_amount"] = discount_amount

    taxable = subtotal - discount_amount
    tax_rate = float(state.get("tax", 0) or 0)
    tax_amount = taxable * tax_rate / 100
    state["tax_amount"] = tax_amount
    state["total"] = taxable + tax_amount
    return state


def apply_documents_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type == "doc.created":
        current.update({"entity_type": "doc", **data})
        current.setdefault("status", "draft")
        current.setdefault("linked", [])
        current.setdefault("amount_paid", 0.0)
        current.setdefault("amount_outstanding", float(current.get("total", 0) or 0))
    elif event_type == "doc.updated":
        for field, change in data["fields_changed"].items():
            current[field] = change.get("new")
    elif event_type == "doc.linked":
        current.setdefault("linked", [])
        current["linked"].append({"entity_id": data["entity_id"], "entity_type": data["entity_type"]})
    elif event_type == "doc.sent":
        current["status"] = "sent"
        if data.get("sent_via"):
            current["sent_via"] = data["sent_via"]
        if data.get("sent_to"):
            current["sent_to"] = data["sent_to"]
    elif event_type == "doc.finalized":
        current["status"] = "final"
        # Invoice finalize assigns real ref_id (INV-...) and preserves proforma ref
        if data.get("ref_id"):
            current["ref_id"] = data["ref_id"]
            current["doc_number"] = data["ref_id"]
        if data.get("source_proforma_ref"):
            current["source_proforma_ref"] = data["source_proforma_ref"]
    elif event_type == "doc.converted_to_bill":
        current["status"] = "awaiting_payment"
        if data.get("ref_id"):
            current["ref_id"] = data["ref_id"]
            current["doc_number"] = data["ref_id"]
        if data.get("source_po_ref"):
            current["source_po_ref"] = data["source_po_ref"]
        if data.get("doc_type"):
            current["doc_type"] = data["doc_type"]
    elif event_type == "doc.voided":
        current["status"] = "void"
        if data.get("reason"):
            current["void_reason"] = data["reason"]
        if data.get("pre_void_status"):
            current["pre_void_status"] = data["pre_void_status"]
        if data.get("pre_void_fulfillment"):
            current["pre_void_fulfillment"] = data["pre_void_fulfillment"]
    elif event_type == "doc.reverted_to_draft":
        current["status"] = "draft"
        # PO->bill revert: restore doc_type and original ref_id
        if data.get("doc_type"):
            current["doc_type"] = data["doc_type"]
        if data.get("ref_id"):
            current["ref_id"] = data["ref_id"]
            current["doc_number"] = data["ref_id"]
        current.pop("fulfillment_status", None)
        current.pop("fulfilled_items", None)
        current.pop("fulfilled_at", None)
        current.pop("fulfilled_by", None)
    elif event_type == "doc.unvoided":
        restored = data.get("restored_status", "final")
        current["status"] = restored
        current.pop("void_reason", None)
        current.pop("pre_void_status", None)
    elif event_type == "doc.payment.received":
        paid = float(current.get("amount_paid", 0)) + float(data["amount"])
        total = float(current.get("total", 0) or 0)
        outstanding = max(0.0, total - paid)
        current["amount_paid"] = paid
        current["amount_outstanding"] = outstanding
        current["status"] = "paid" if outstanding <= 0.005 else "partial"
        # Build payments list
        current.setdefault("payments", [])
        current["payments"].append({
            "index": len(current["payments"]),
            "amount": float(data["amount"]),
            "currency": data.get("currency"),
            "method": data.get("method"),
            "reference": data.get("reference"),
            "payment_date": data.get("payment_date"),
            "bank_account": data.get("bank_account", "1110"),
            "source_doc_id": data.get("source_doc_id"),
            "target_doc_id": data.get("target_doc_id"),
            "status": "active",
        })
    elif event_type == "doc.payment.voided":
        idx = data["payment_index"]
        payments = current.get("payments", [])
        if 0 <= idx < len(payments):
            payments[idx]["status"] = "voided"
            payments[idx]["void_reason"] = data.get("void_reason")
            # Recalculate totals from active payments only
            active_total = sum(p["amount"] for p in payments if p["status"] == "active")
            total = float(current.get("total", 0) or 0)
            current["amount_paid"] = active_total
            current["amount_outstanding"] = max(0.0, total - active_total)
            current["status"] = "paid" if current["amount_outstanding"] <= 0.005 else ("partial" if active_total > 0 else "final")
    elif event_type == "doc.payment.refunded":
        refunded = float(data["amount"])
        total = float(current.get("total", 0) or 0)
        paid = max(0.0, float(current.get("amount_paid", 0)) - refunded)
        outstanding = max(0.0, total - paid)
        current["amount_paid"] = paid
        current["amount_outstanding"] = outstanding
        current["status"] = "paid" if outstanding <= 0.005 else "partial"
    elif event_type == "doc.converted":
        current["status"] = "converted"
        current["converted_to"] = data["target_doc_id"]
        current["converted_to_type"] = data.get("target_doc_type")
    elif event_type == "doc.received":
        received = data.get("received_items", [])
        current.setdefault("received_items", [])
        current["received_items"].extend(received)

        line_items = current.get("line_items", [])
        all_received = True
        any_received = False
        for idx, line in enumerate(line_items):
            ordered = float(line.get("quantity", 0) or 0)
            rec_qty = sum(float(x.get("quantity_received", 0) or 0) for x in current["received_items"] if int(x.get("po_line_index", -1)) == idx)
            # Update per-line received tracking
            line["quantity_received"] = rec_qty
            if rec_qty > 0:
                any_received = True
            if rec_qty + 1e-9 < ordered:
                all_received = False

        if line_items and all_received:
            current["status"] = "received"
        elif any_received:
            current["status"] = "partially_received"
        else:
            current["status"] = "final"
    elif event_type == "doc.items_returned":
        returned = data.get("items", [])
        current.setdefault("returned_items", [])
        current["returned_items"].extend(returned)

        # Calculate total received vs total returned per item
        received_items = current.get("received_items", [])
        total_received = sum(float(x.get("quantity_received", 0) or 0) for x in received_items)
        total_returned = sum(float(x.get("quantity_returned", 0) or 0) for x in current["returned_items"])

        if total_received > 0 and total_returned + 1e-9 >= total_received:
            current["status"] = "returned"
        elif total_returned > 0:
            current["status"] = "partial_returned"
    elif event_type == "doc.shared_import":
        # Inbound doc received via p2p share / bundle upload.
        # Carries the sender's full doc state; status forced to "received".
        current.update({"entity_type": "doc", **data})
        current["status"] = "received"
        current.setdefault("linked", [])
        current.setdefault("amount_paid", 0.0)
        current.setdefault("amount_outstanding", float(current.get("total", 0) or 0))
    elif event_type == "doc.note_added":
        current.setdefault("internal_notes", [])
        current["internal_notes"].append({
            "text": data["text"],
            "created_at": data.get("created_at", ""),
            "created_by": data.get("created_by", ""),
        })
    elif event_type == "doc.fulfilled":
        current["fulfillment_status"] = "fulfilled"
        current["fulfilled_items"] = data["fulfilled_items"]
        current["fulfilled_at"] = data.get("fulfilled_at") or ""
        current["fulfilled_by"] = data["fulfilled_by"]
    elif event_type == "doc.partially_fulfilled":
        current["fulfillment_status"] = "partial"
        current["fulfilled_items"] = data["fulfilled_items"]
        current["fulfilled_at"] = data.get("fulfilled_at") or ""
        current["fulfilled_by"] = data["fulfilled_by"]
    elif event_type == "doc.fulfillment_reversed":
        current.pop("fulfillment_status", None)
        current.pop("fulfilled_items", None)
        current.pop("fulfilled_at", None)
        current.pop("fulfilled_by", None)

    # --- List events (entity_type="list") ---
    elif event_type == "list.created":
        current.update({"entity_type": "list", **data})
        current.setdefault("status", "draft")
        current.setdefault("line_items", [])
        current.setdefault("subtotal", 0.0)
        current.setdefault("discount", 0.0)
        current.setdefault("discount_type", "flat")
        current.setdefault("tax", 0.0)
        current.setdefault("total", 0.0)
        current = _recalc_list_totals(current)
    elif event_type == "list.updated":
        for field, change in data["fields_changed"].items():
            if field == "currency" and current.get("currency") is not None:
                continue  # currency is immutable after creation
            current[field] = change.get("new")
        current = _recalc_list_totals(current)
    elif event_type == "list.sent":
        current["status"] = "sent"
        if data.get("sent_via"):
            current["sent_via"] = data["sent_via"]
        if data.get("sent_to"):
            current["sent_to"] = data["sent_to"]
    elif event_type == "list.accepted":
        current["status"] = "accepted"
        if data.get("notes"):
            current["accepted_notes"] = data["notes"]
    elif event_type == "list.completed":
        current["status"] = "completed"
        if data.get("notes"):
            current["completed_notes"] = data["notes"]
    elif event_type == "list.voided":
        current["status"] = "void"
        if data.get("reason"):
            current["void_reason"] = data["reason"]
    elif event_type == "list.converted":
        current["status"] = "converted"
        current["converted_to"] = data["target_doc_id"]
        current["converted_to_type"] = data["target_doc_type"]

    elif event_type in {"doc.patched", "list.patched"}:
        # CSV upsert: merge data fields into existing state
        current.update(data)

    else:
        raise ValueError(f"Unsupported event: {event_type}")

    return current


# Backward compat alias — kept so conftest.py and any direct imports still work.
def apply_list_event(state: dict, event_type: str, data: dict) -> dict:  # noqa: D401
    return apply_documents_event(state, event_type, data)
