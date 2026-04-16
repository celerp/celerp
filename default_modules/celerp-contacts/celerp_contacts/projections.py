# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from copy import deepcopy


def _compose_address(addr: dict) -> str:
    """Build a single-line address string from address dict fields."""
    parts = [
        addr.get("line1", ""),
        addr.get("line2", ""),
        addr.get("city", ""),
        addr.get("state", ""),
        addr.get("postal_code", ""),
        addr.get("country", ""),
    ]
    return ", ".join(p for p in parts if p)


def _sync_primary_address_fields(state: dict) -> None:
    """Update top-level billing_address/shipping_address from the default address of each type."""
    addresses = state.get("addresses") or []
    for addr_type, field in (("billing", "billing_address"), ("shipping", "shipping_address")):
        default = next((a for a in addresses if a.get("address_type") == addr_type and a.get("is_default")), None)
        if default:
            state[field] = _compose_address(default)
        elif not any(a.get("address_type") == addr_type for a in addresses):
            # No addresses of this type remain — clear the field only if it was previously synced
            # (don't wipe manually-entered values unless there were addresses of this type)
            pass  # preserve manually-set values


def apply_contact_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type == "crm.contact.created":
        current.update({"entity_type": "contact", **data})
    elif event_type == "crm.contact.updated":
        for field, change in data["fields_changed"].items():
            current[field] = change.get("new")
    elif event_type == "crm.contact.merged":
        current.setdefault("merged_from", [])
        current["merged_from"] = sorted(set(current["merged_from"]) | set(data["source_contact_ids"]))
    elif event_type == "crm.contact.tagged":
        current.setdefault("tags", [])
        current["tags"] = sorted(set(current["tags"]) | set(data["tags"]))
    elif event_type == "crm.contact.note_added":
        current.update({
            "entity_type": "contact_note",
            "note_id": data.get("note_id"),
            "contact_id": data.get("contact_id"),
            "note": data.get("note"),
            "author_id": data.get("author_id"),
            "author_name": data.get("author_name"),
            "created_at": data.get("created_at"),
            "updated_at": None,
        })
    elif event_type == "crm.contact.note_updated":
        current["note"] = data.get("note")
        current["updated_at"] = data.get("updated_at")
    elif event_type == "crm.contact.note_removed":
        current["deleted"] = True

    elif event_type == "crm.contact.person_added":
        current.setdefault("people", [])
        current["people"].append(data)
        if data.get("is_primary") and data.get("name"):
            current["name"] = data["name"]
    elif event_type == "crm.contact.person_updated":
        people = current.get("people", [])
        for i, p in enumerate(people):
            if p.get("person_id") == data.get("person_id"):
                people[i] = {**p, **data}
                break
        current["people"] = people
        if data.get("is_primary") and data.get("name"):
            current["name"] = data["name"]
    elif event_type == "crm.contact.person_removed":
        current["people"] = [p for p in current.get("people", []) if p.get("person_id") != data.get("person_id")]

    elif event_type == "crm.contact.address_added":
        current.setdefault("addresses", [])
        new_addr = dict(data)
        if new_addr.get("is_default"):
            # Unmark any existing default of the same type
            addr_type = new_addr.get("address_type", "billing")
            for a in current["addresses"]:
                if a.get("address_type") == addr_type:
                    a["is_default"] = False
        current["addresses"].append(new_addr)
        _sync_primary_address_fields(current)
    elif event_type == "crm.contact.address_updated":
        addrs = current.get("addresses", [])
        updated_data = dict(data)
        for i, a in enumerate(addrs):
            if a.get("address_id") == updated_data.get("address_id"):
                merged = {**a, **{k: v for k, v in updated_data.items() if v is not None}}
                # If setting is_default=True, unmark others of same type
                if updated_data.get("is_default"):
                    addr_type = merged.get("address_type", "billing")
                    for j, other in enumerate(addrs):
                        if j != i and other.get("address_type") == addr_type:
                            addrs[j] = {**other, "is_default": False}
                addrs[i] = merged
                break
        current["addresses"] = addrs
        _sync_primary_address_fields(current)
    elif event_type == "crm.contact.address_removed":
        current["addresses"] = [a for a in current.get("addresses", []) if a.get("address_id") != data.get("address_id")]
        _sync_primary_address_fields(current)

    elif event_type == "crm.contact.file_attached":
        current.setdefault("files", [])
        current["files"].append({
            "file_id": data["file_id"],
            "filename": data["filename"],
            "content_type": data.get("content_type"),
            "size": data.get("size"),
            "uploaded_at": data.get("uploaded_at"),
            "description": data.get("description", ""),
        })
    elif event_type == "crm.contact.file_removed":
        current["files"] = [f for f in current.get("files", []) if f["file_id"] != data["file_id"]]

    elif event_type == "crm.memo.created":
        current.update({"entity_type": "memo", **data})
        current.setdefault("status", "draft")
        current.setdefault("items", [])
        current.setdefault("is_on_memo", True)
    elif event_type == "crm.memo.item_added":
        current.setdefault("items", [])
        current["items"].append({"item_id": data["item_id"], "quantity": data.get("quantity")})
    elif event_type == "crm.memo.item_removed":
        current.setdefault("items", [])
        current["items"] = [i for i in current["items"] if i.get("item_id") != data["item_id"]]
    elif event_type == "crm.memo.approved":
        current["status"] = "approved"
        current["is_on_memo"] = False
    elif event_type == "crm.memo.cancelled":
        current["status"] = "cancelled"
        current["is_on_memo"] = False
        if data.get("reason"):
            current["cancel_reason"] = data["reason"]
    elif event_type == "crm.memo.invoiced":
        current["status"] = "invoiced"
        current["is_on_memo"] = False
        current["doc_id"] = data["doc_id"]
        current["items_invoiced"] = data.get("items_invoiced", [])
    elif event_type == "crm.memo.returned":
        current["status"] = "returned"
        current["is_on_memo"] = False
        current.setdefault("returned_items", [])
        current["returned_items"].extend(data.get("items_returned", []))
    else:
        raise ValueError(f"Unsupported contact/memo event: {event_type}")

    return current
