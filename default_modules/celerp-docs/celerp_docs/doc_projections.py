# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from celerp.services.money import round_money, to_decimal, to_stored_float


def _recalc_list_totals(state: dict) -> dict:
    """Recompute subtotal, discount_amount, tax_amount, total from line_items + header fields.

    All monetary results are computed with Decimal and rounded to the list's currency precision
    (never raw float) so totals never carry IEEE-754 tails like "346.50000000000006".
    """
    currency = state.get("currency")
    items = state.get("line_items", [])
    subtotal = round_money(sum((to_decimal(i.get("line_total", 0) or 0) for i in items), to_decimal(0)), currency)
    state["subtotal"] = to_stored_float(subtotal)

    discount = to_decimal(state.get("discount", 0) or 0)
    discount_type = state.get("discount_type", "flat")
    discount_amount = round_money(subtotal * discount / 100 if discount_type == "percentage" else discount, currency)
    state["discount_amount"] = to_stored_float(discount_amount)

    taxable = subtotal - discount_amount
    # Use per-line tax rates if present; fall back to header tax rate.
    per_line_tax = sum(
        (to_decimal(i.get("line_total", 0) or 0) * to_decimal(i.get("tax_rate", 0) or 0) / 100 for i in items),
        to_decimal(0),
    )
    if per_line_tax:
        tax_amount = round_money(per_line_tax, currency)
    else:
        tax_rate = to_decimal(state.get("tax", 0) or 0)
        tax_amount = round_money(taxable * tax_rate / 100, currency)
    state["tax_amount"] = to_stored_float(tax_amount)
    state["total"] = to_stored_float(round_money(taxable + tax_amount, currency))
    return state


def apply_documents_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type == "doc.created":
        current.update({"entity_type": "doc", **data})
        current.setdefault("status", "draft")
        current.setdefault("linked", [])
        current.setdefault("amount_paid", 0.0)
        current.setdefault("amount_outstanding", float(current.get("total", 0) or 0))
        current.setdefault("files", [])
    elif event_type == "doc.pushed":
        # Outbound write-back: record the id the platform returned so this doc is never
        # pushed (created) there again. Field matches list_unsynced_invoices' skip check.
        platform = data.get("platform")
        ext_id = data.get("external_id")
        if platform and ext_id:
            current[f"{platform}_{data.get('entity', 'invoice')}_id"] = str(ext_id)
    elif event_type == "doc.updated":
        _is_template = current.get("doc_type") in {"subscription_invoice", "subscription_po"}
        _PATCH_PROTECTED = {"entity_type", "company_id"} if _is_template else {"status", "entity_type", "company_id"}
        _is_finalized = current.get("status", "draft") != "draft"
        for field, change in data["fields_changed"].items():
            if field in _PATCH_PROTECTED:
                continue
            if field == "conversion_rate" and _is_finalized:
                continue  # conversion_rate is immutable after finalization
            current[field] = change.get("new")
        # If total changed (e.g. line items added/removed on a draft), recalculate outstanding
        # based on how much has already been paid - never let outstanding go negative.
        if "total" in data.get("fields_changed", {}) or "line_items" in data.get("fields_changed", {}):
            paid = to_decimal(current.get("amount_paid", 0))
            total = to_decimal(current.get("total", 0))
            current["amount_outstanding"] = to_stored_float(max(Decimal(0), total - paid))
    elif event_type == "doc.renumbered":
        # Narrow alias of doc.updated: only ref_id / doc_number may be changed.
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
        # Bills with only non-stock items skip receiving and go straight to awaiting_payment.
        current["status"] = "awaiting_payment" if data.get("skip_receiving") else "final"
        # Durable "has been issued" marker: the status is later overwritten by
        # doc.sent, so the Issue button relies on this flag, not the status.
        current["finalized"] = True
        # Invoice finalize assigns real ref_id (INV-...) and preserves proforma ref
        if data.get("ref_id"):
            current["ref_id"] = data["ref_id"]
            current["doc_number"] = data["ref_id"]
        if data.get("source_proforma_ref"):
            current["source_proforma_ref"] = data["source_proforma_ref"]
    elif event_type == "doc.converted_to_bill":
        current["status"] = "awaiting_payment"
        current["finalized"] = True  # a converted bill is issued
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
        current["finalized"] = False  # back to an editable, un-issued draft
        # PO->bill revert: restore doc_type and original ref_id
        if data.get("doc_type"):
            current["doc_type"] = data["doc_type"]
        if data.get("ref_id"):
            current["ref_id"] = data["ref_id"]
            current["doc_number"] = data["ref_id"]
        # Track how many times this doc has been reverted so re-finalization
        # uses a distinct JE id and idempotency key each cycle.
        current["revert_count"] = int(current.get("revert_count", 0)) + 1
        # Inbound docs: clear receive and fulfillment state on revert so the bill can be received again.
        if current.get("doc_type") in ("bill", "consignment_in"):
            current.pop("fulfilled_items", None)
            current.pop("fulfillment_status", None)
            current.pop("received_items", None)
            current.pop("received_item_ids", None)
            # Clear entity_id from line items so they appear as "Not Received" again.
            for li in current.get("line_items", []):
                li.pop("entity_id", None)
                li.pop("quantity_received", None)
        # Fulfillment state is independent of doc status - do not clear it here.
        # Use the /unfulfill endpoint to explicitly revert fulfillment.
    elif event_type == "doc.unvoided":
        restored = data.get("restored_status", "final")
        current["status"] = restored
        current.pop("void_reason", None)
        current.pop("pre_void_status", None)
    elif event_type == "doc.payment.received":
        paid = to_decimal(current.get("amount_paid", 0)) + to_decimal(data["amount"])
        total = to_decimal(current.get("total", 0))
        outstanding = max(Decimal(0), total - paid)
        current["amount_paid"] = to_stored_float(paid)
        current["amount_outstanding"] = to_stored_float(outstanding)
        current["status"] = "paid" if outstanding <= Decimal("0.005") else "partial"
        # Build payments list
        current.setdefault("payments", [])
        current["payments"].append({
            # The recorder allocates the index and skips any value a journal
            # entry was ever minted with (docs compacted by pre-tombstone
            # deletions can have minted indices beyond the list length), so a
            # payment's index field, not its list position, is its identity.
            "index": data.get("index", len(current["payments"])),
            "amount": float(data["amount"]),
            "currency": data.get("currency"),
            "method": data.get("method"),
            "reference": data.get("reference"),
            "payment_date": data.get("payment_date"),
            "bank_account": data.get("bank_account"),
            "conversion_rate": data.get("conversion_rate"),
            "source_doc_id": data.get("source_doc_id"),
            "target_doc_id": data.get("target_doc_id"),
            # Online charge that exceeded the fresh outstanding (a manual
            # payment raced the checkout): the applied amount is clamped and
            # the real charge stays on record to refund or credit.
            "charged_amount": data.get("charged_amount"),
            "status": "active",
        })
    elif event_type == "doc.payment.voided":
        idx = data["payment_index"]
        payments = current.get("payments", [])
        # Payments are identified by their index FIELD (list position can lag
        # behind on docs compacted by pre-tombstone deletions).
        target = next((p for p in payments if p.get("index") == idx), None)
        if target is not None:
            # Refunds adjust amount_paid without a payments[] row, so their
            # effect is derived before this removal: what the actives summed to
            # minus what amount_paid actually was. Deriving (rather than
            # storing a counter) also covers docs refunded before this logic
            # existed. Doc-level refunds cannot be attributed to one payment,
            # so removing the very payment a refund already returned still
            # subtracts both - the price of doc-level refund semantics.
            _prior_active = to_decimal(sum(p["amount"] for p in payments if p["status"] == "active"))
            refunded = max(Decimal(0), _prior_active - to_decimal(current.get("amount_paid", 0)))
            target["status"] = "voided"
            target["void_reason"] = data.get("void_reason")
            target["refund_date"] = data.get("refund_date")
            active_total = to_decimal(sum(p["amount"] for p in payments if p["status"] == "active"))
            total = to_decimal(current.get("total", 0))
            paid = max(Decimal(0), active_total - refunded)
            outstanding = max(Decimal(0), total - paid)
            current["amount_paid"] = to_stored_float(paid)
            current["amount_outstanding"] = to_stored_float(outstanding)
            current["status"] = "paid" if outstanding <= Decimal("0.005") else ("partial" if paid > 0 else "final")
    elif event_type == "doc.payment.deleted":
        idx = data["payment_index"]
        payments = current.get("payments", [])
        _prior_active = to_decimal(sum(p["amount"] for p in payments if p["status"] == "active"))
        refunded = max(Decimal(0), _prior_active - to_decimal(current.get("amount_paid", 0)))
        if data.get("tombstone"):
            # Tombstone in place, never compact: payment indices are identity.
            # Journal-entry ids and idempotency keys embed the index, so a
            # removed slot must stay occupied or a later payment would reuse
            # the index and its journal entry would dedupe into the old one,
            # silently posting nothing. Lookup is by index FIELD, since list
            # position can lag on docs compacted by pre-tombstone deletions.
            target = next((p for p in payments if p.get("index") == idx), None)
            changed = target is not None
            if changed:
                target["status"] = "deleted"
        else:
            # Deletion events written before the tombstone flag compacted the
            # list positionally; replaying them must keep doing exactly that,
            # because every later event in those streams references the
            # compacted positions.
            changed = 0 <= idx < len(payments)
            if changed:
                del payments[idx]
                for i, p in enumerate(payments):
                    p["index"] = i
        if changed:
            active_total = to_decimal(sum(p["amount"] for p in payments if p["status"] == "active"))
            total = to_decimal(current.get("total", 0))
            paid = max(Decimal(0), active_total - refunded)
            outstanding = max(Decimal(0), total - paid)
            current["amount_paid"] = to_stored_float(paid)
            current["amount_outstanding"] = to_stored_float(outstanding)
            current["status"] = "paid" if outstanding <= Decimal("0.005") else ("partial" if paid > 0 else "final")
    elif event_type == "doc.payment.refunded":
        refunded = to_decimal(data["amount"])
        total = to_decimal(current.get("total", 0))
        paid = max(Decimal(0), to_decimal(current.get("amount_paid", 0)) - refunded)
        outstanding = max(Decimal(0), total - paid)
        current["amount_paid"] = to_stored_float(paid)
        current["amount_outstanding"] = to_stored_float(outstanding)
        current["status"] = "paid" if outstanding <= Decimal("0.005") else "partial"
    elif event_type == "doc.converted":
        current["status"] = "converted"
        current["converted_to"] = data["target_doc_id"]
        current["converted_to_type"] = data.get("target_doc_type")
    elif event_type == "doc.received":
        received = data.get("received_items", [])
        current.setdefault("received_items", [])
        current["received_items"].extend(received)
        current.setdefault("received_item_ids", [])
        current["received_item_ids"].extend(data.get("created_item_ids", []))

        # Write entity_id back onto each line item so per-line fulfillment UI can key off it.
        # For inbound docs (bill, consignment_in), item_id in the payload is the catalog template -
        # the actual new parcel id is always in created_item_ids. Always consume from created_item_ids.
        # For outbound docs (purchase_order), item_id IS the entity that was adjusted.
        line_items = current.get("line_items", [])
        created_ids = list(data.get("created_item_ids", []))
        created_id_iter = iter(created_ids)
        _is_inbound_doc = current.get("doc_type") in ("bill", "consignment_in")
        for recv in received:
            if _is_inbound_doc:
                # Always consume from created_item_ids for inbound docs
                assigned_id = next(created_id_iter, None) if (recv.get("receive_as", "stock") or "stock") == "stock" else None
            else:
                assigned_id = recv.get("item_id")
                if not assigned_id:
                    if (recv.get("receive_as", "stock") or "stock") == "stock":
                        assigned_id = next(created_id_iter, None)
            if not assigned_id:
                continue
            idx = int(recv.get("po_line_index", -1))
            if 0 <= idx < len(line_items):
                line_items[idx].setdefault("entity_id", assigned_id)
            else:
                sku = (recv.get("sku") or "").strip()
                if sku:
                    for li in line_items:
                        if str(li.get("sku") or "").strip() == sku and not li.get("entity_id"):
                            li["entity_id"] = assigned_id
                            break
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
    elif event_type == "doc.return_received":
        # Customer return on a credit note: track what came back (status unchanged - CN stays final/paid)
        items = data.get("items", [])
        current.setdefault("return_received_items", [])
        current["return_received_items"].extend(items)
    elif event_type == "doc.return_undone":
        # Undo a receive-return: clear the received items list
        current["return_received_items"] = []
    elif event_type == "doc.receive_undone":
        current["received_items"] = []
        current["received_item_ids"] = []
        current["status"] = "final"
        # Clear entity_id from line items so per-line status column resets to "Not Received".
        for li in current.get("line_items", []):
            li.pop("entity_id", None)
    elif event_type == "doc.shared_import":
        # Inbound doc received via p2p share / bundle upload.
        # Carries the sender's full doc state; status forced to "received".
        current.update({"entity_type": "doc", **data})
        current["status"] = "received"
        current.setdefault("linked", [])
        current.setdefault("amount_paid", 0.0)
        current.setdefault("amount_outstanding", float(current.get("total", 0) or 0))
    elif event_type == "doc.file_attached":
        current.setdefault("files", [])
        current["files"].append({
            "id": data["file_id"],
            "filename": data["filename"],
            "mime": data.get("mime"),
            "size": data.get("size"),
            "url": data.get("url", ""),
            "description": data.get("description") or "",
            "document_tag": data.get("document_tag") or "",
            "uploaded_at": data.get("uploaded_at") or "",
        })
    elif event_type == "doc.file_tagged":
        for f in current.get("files", []):
            if f.get("id") == data.get("file_id"):
                f["document_tag"] = data.get("document_tag", "")
                break
    elif event_type == "doc.file_description_updated":
        for f in current.get("files", []):
            if f.get("id") == data.get("file_id"):
                f["description"] = data.get("description", "")
                break
    elif event_type == "doc.file_deleted":
        current["files"] = [f for f in current.get("files", []) if f.get("id") != data["file_id"]]
    elif event_type == "doc.note_added":
        # First-class doc_note entity (same pattern as contact_note)
        current.update({
            "entity_type": "doc_note",
            "note_id": data.get("note_id"),
            "doc_id": data.get("doc_id"),
            "note": data.get("note"),
            "author_id": data.get("author_id"),
            "author_name": data.get("author_name"),
            "created_at": data.get("created_at"),
            "updated_at": None,
        })
    elif event_type == "doc.note_updated":
        current["note"] = data.get("note")
        current["updated_at"] = data.get("updated_at")
    elif event_type == "doc.note_removed":
        current["deleted"] = True
    elif event_type == "list.note_added":
        current.update({
            "entity_type": "list_note",
            "note_id": data.get("note_id"),
            "list_id": data.get("list_id"),
            "note": data.get("note"),
            "author_id": data.get("author_id"),
            "author_name": data.get("author_name"),
            "created_at": data.get("created_at"),
            "updated_at": None,
        })
    elif event_type == "list.note_updated":
        current["note"] = data.get("note")
        current["updated_at"] = data.get("updated_at")
    elif event_type == "list.note_removed":
        current["deleted"] = True
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
    elif event_type == "doc.partially_reverted":
        # Some (not all) lines reverted: the doc stays partially fulfilled. Status matches
        # doc.partially_fulfilled; the distinct type only changes how the activity reads.
        current["fulfillment_status"] = "partial"
    elif event_type == "doc.fulfillment_reversed":
        current["fulfill_cycle"] = int(current.get("fulfill_cycle", 0)) + 1
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
    elif event_type == "list.finalized":
        # draft -> finalized. Carries status + finalize milestone (sent_at / issued_at) and, for
        # audits, the frozen on-hand snapshot (line_items with l["on_hand"]).
        current.update(data)
        current["status"] = "finalized"
    elif event_type == "list.reverted":
        # finalized -> draft (go back, before any terminal action). Drop finalize milestones so the
        # badge/UX reflect a clean draft again.
        current["status"] = "draft"
        for k in ("finalized_at", "sent_at", "issued_at", "accepted_at"):
            current.pop(k, None)
    elif event_type == "list.closed":
        # finalized -> closed. The terminal outcome lives in `result`; terminal-specific fields
        # (converted_to / line_items+adjust_count for audit) ride along in data.
        current.update(data)
        current["status"] = "closed"
    elif event_type == "list.reopened":
        # closed -> finalized (undo a terminal action, e.g. undo-adjust). Clear the outcome.
        current.update(data)
        current["status"] = "finalized"
        current.pop("result", None)
    elif event_type == "list.voided":
        current["status"] = "void"
        if data.get("reason"):
            current["void_reason"] = data["reason"]

    elif event_type in {"doc.patched", "list.patched"}:
        # CSV upsert: merge data fields into existing state
        current.update(data)

    else:
        raise ValueError(f"Unsupported event: {event_type}")

    return current


# Backward compat alias — kept so conftest.py and any direct imports still work.
def apply_list_event(state: dict, event_type: str, data: dict) -> dict:  # noqa: D401
    return apply_documents_event(state, event_type, data)
