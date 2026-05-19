# Item Files Unification Plan

**Date:** 2026-05-18  
**Status:** Draft - pending review  
**Branch:** feature/item-files-unification (to be opened against develop)

---

## Problem

Items currently have two separate file mechanisms:

1. **`state["attachments"]`** - a list embedded directly in the item projection. Modified via `item.updated` events with `fields_changed["attachments"]`. No tagging, no search, no pagination. The UI renders a bespoke attachment panel.

2. **Contacts/Docs files system** - individual `crm.contact.file_*` events per file. Stored as `state["files"]` list in the projection. The `_files_section` shared UI component provides tagging (including `certificates`), search, sort, pagination, and description editing.

The result: items cannot tag uploads as certificates (critical for marketplace), have no search or filter over their attachments, and the attachment code is WET relative to the contacts system.

---

## Goal

Migrate items to the shared files system - same event types, same projection pattern, same `_files_section` UI component. One attachment system across all entities.

**Carve-out:** `preview_image_id` stays item-specific. It is a product display concept (hero image for the inventory table thumbnail and marketplace) with no equivalent in contacts. It is written/read directly on the item projection as today.

---

## Design Decisions

### Event naming

Contacts use entity-namespaced events: `crm.contact.file_attached`, `crm.contact.file_tagged`, etc.

Items follow the same pattern: `item.file_attached`, `item.file_tagged`, `item.file_deleted`, `item.file_description_updated`.

These are added to `EventTypes` in `celerp/events/types.py`.

### Projection storage

Items gain a `files: list[dict]` key in their projection state, with identical schema to contacts:

```python
{
    "id": str,           # uuid
    "filename": str,
    "mime": str | None,
    "size": int | None,
    "url": str,
    "description": str,
    "document_tag": str, # slug from _DOCUMENT_TAGS (e.g. "certificates")
    "uploaded_at": str,  # ISO datetime
}
```

`state["attachments"]` is **removed** from the item projection. It existed only as a legacy container; `files` replaces it entirely.

`state["preview_image_id"]` remains. It references a file by `id` (same id space, now drawn from `files` instead of `attachments`).

### API routes

New routes in `celerp-inventory`, mirroring contacts exactly:

```
POST   /items/{entity_id}/files                          — upload one file
POST   /items/{entity_id}/files/{file_id}/tag            — set document_tag
PATCH  /items/{entity_id}/files/{file_id}/description    — set description
DELETE /items/{entity_id}/files/{file_id}                — delete file
GET    /items/{entity_id}/files/{file_id}/download       — proxy/redirect to static URL
PUT    /items/{entity_id}/files/{file_id}/preview        — set as preview_image_id (images only)
GET    /items/{entity_id}/files/_section                 — HTMX partial: re-render files section
```

The old attachment routes (`/items/{entity_id}/attachments`, `/items/attachments/bulk`) are **kept** for bulk ZIP only. The single-file upload route is replaced by `/files`.

### Bulk ZIP endpoint

`POST /items/attachments/bulk` is retained unchanged at the route level. Internally it switches from `_patch_item_attachments` (which wrote `fields_changed["attachments"]`) to emitting `item.file_attached` events - one per file. The SKU-matching loop, ZIP parsing, and filename convention (`-cert-`, `-360-`, `-doc-`) are untouched.

The filename suffix → document_tag mapping:

| Suffix | `document_tag` |
|--------|----------------|
| `-cert-` | `certificates` |
| `-doc-` | `certificates` |
| `-360-` | (stored in `document_tag=""`, MIME stays as image; `360` is a display concern, not a tag) |
| (none) | `""` (no tag) |

`attachment_type` as a concept is removed from the bulk path. Type is now expressed purely through `document_tag`.

### Preview image

`preview_image_id` auto-set on first image upload (same logic as today). On delete of the preview file, falls back to the first remaining image in `state["files"]` where `mime` starts with `image/`.

The `PUT /items/{entity_id}/files/{file_id}/preview` route sets `preview_image_id` explicitly (must reference a file with `image/` mime).

`preview_image_id` is written via a separate `item.updated` event with `fields_changed: {"preview_image_id": {"new": id}}`. This keeps it decoupled from the files event stream.

### UI

`_attachments_panel` in `ui/routes/inventory.py` is **deleted**. It is replaced by a call to `_shared_files_section("item", entity_id, item.get("files") or [])`.

The inline image drag-drop cell in the inventory table (`cell--image`) continues to work. It posts to the new `/items/{entity_id}/files` route. The backend auto-sets `preview_image_id` when the uploaded file is an image and no preview exists yet.

### Data migration

No migration needed. Existing items with `state["attachments"]` data: the projection handler for `item.file_attached` will add to `state["files"]`; on first new upload the files key is created. Old `attachments` key is ignored going forward and will naturally disappear as projections are rebuilt.

Items currently have very few attachments in practice (no production data). The risk is negligible.

---

## Files Changed

### New / modified

| File | Change |
|------|--------|
| `celerp/events/types.py` | Add `ITEM_FILE_ATTACHED`, `ITEM_FILE_TAGGED`, `ITEM_FILE_DELETED`, `ITEM_FILE_DESCRIPTION_UPDATED` |
| `celerp-inventory/celerp_inventory/projections.py` | Add handlers for `item.file_*` events; build `files` list; manage `preview_image_id` fallback on delete |
| `celerp-inventory/celerp_inventory/routes_files.py` | **New file** - item file routes (upload, tag, describe, delete, download, preview, HTMX section partial) |
| `celerp-inventory/celerp_inventory/routes_attachments.py` | Remove single-file upload/delete/preview routes; keep bulk ZIP route; switch bulk emit to `item.file_attached` events |
| `celerp-inventory/__init__.py` | Register `routes_files` router |
| `ui/routes/inventory.py` | Delete `_attachments_panel`; call `_shared_files_section` in item detail; add `/items/{entity_id}/files/_section` HTMX route |
| `ui/api_client.py` | Remove `upload_attachment`; (file upload is now direct fetch from JS, not proxied through UI) |

### Deleted

| File | Reason |
|------|--------|
| `celerp-inventory/celerp_inventory/routes_attachments.py` (partially) | Single-file routes removed; bulk kept |

### Unchanged

| File | Why unchanged |
|------|---------------|
| `ui/components/files.py` | Already generic - no changes needed |
| `celerp/services/attachments.py` | `store_upload`, `remove_attachment`, etc. used as-is |
| `celerp-contacts/` | No changes; contacts files system is the template, not the target |

---

## Unit Tests

### Remove

| Test | Reason |
|------|--------|
| `test_upload_attachment` (if exists in test_items.py) | Route removed |
| `test_delete_attachment` | Route removed |
| `test_set_preview_image` (old path) | Replaced by new route |
| `test_patch_item_attachments` | Internal helper deleted |
| Any test asserting `state["attachments"]` list | Key no longer used |

### Modify

| Test | Change |
|------|--------|
| `test_bulk_attach` | Assert `item.file_attached` events emitted (not `item.updated` with attachments) |
| `test_bulk_attach_cert_suffix` | Assert `document_tag="certificates"` on emitted event |
| Any test reading `item.state["attachments"]` for images | Switch to `item.state["files"]` |

### Add (write-first)

| Test | Asserts |
|------|---------|
| `test_item_file_upload_creates_file_event` | POST `/items/{id}/files` emits `item.file_attached`; file appears in `state["files"]` |
| `test_item_file_upload_sets_preview_on_first_image` | First image upload auto-sets `preview_image_id` |
| `test_item_file_upload_does_not_set_preview_for_non_image` | PDF upload leaves `preview_image_id` unchanged |
| `test_item_file_tag_updates_document_tag` | POST `.../tag` emits `item.file_tagged`; projection updated |
| `test_item_file_description_update` | PATCH `.../description` emits `item.file_description_updated` |
| `test_item_file_delete_removes_from_files` | DELETE emits `item.file_deleted`; file gone from projection |
| `test_item_file_delete_reassigns_preview` | Deleting the preview image falls back to next image |
| `test_item_file_set_preview_explicit` | PUT `.../preview` sets `preview_image_id`; rejects non-image |
| `test_item_file_set_preview_rejects_non_image` | PUT `.../preview` on PDF returns 422 |
| `test_bulk_attach_emits_file_events` | Bulk ZIP emits `item.file_attached` per matched file |
| `test_bulk_attach_cert_suffix_sets_tag` | `-cert-` in filename → `document_tag="certificates"` |
| `test_item_projection_applies_file_attached` | Unit test: `apply_item_event` with `item.file_attached` builds `files` list |
| `test_item_projection_applies_file_deleted` | Unit test: `apply_item_event` with `item.file_deleted` removes entry |
| `test_item_projection_applies_file_tagged` | Unit test: `apply_item_event` with `item.file_tagged` updates tag |

---

## Implementation Order

1. `events/types.py` - add 4 event type constants
2. `projections.py` - add `item.file_*` handlers (write tests first)
3. `routes_files.py` - new item file routes (write tests first)
4. `routes_attachments.py` - remove single-file routes, migrate bulk to new events
5. `__init__.py` - register new router
6. `ui/routes/inventory.py` - replace `_attachments_panel` with `_files_section`; add HTMX partial route
7. `ui/api_client.py` - remove `upload_attachment`
8. Full test suite pass

---

## HOLY Commandments Check

- **DRY:** Single file component, single event projection pattern across all entity types. Zero duplication between contacts and items file handling.
- **SOLID:** `routes_files.py` has single responsibility (item file management). `_files_section` open for extension via `entity_type` param; closed for modification. `apply_item_event` extended, not forked.
- **No backward compat:** `state["attachments"]` key abandoned. Old attachment routes removed. No shims.
- **Determinism:** No fallbacks that silently change behavior. Preview fallback on delete is explicit logic, not a hidden default.
- **Concise:** `routes_files.py` will be ~150 lines (mirrors contacts routes exactly). `_attachments_panel` (~100 lines) deleted entirely.
- **KISS:** The contacts pattern is already proven. This is application of an existing pattern, not invention of a new one.
- **Cruft cleanup:** `_attachments_panel`, `_patch_item_attachments`, `merge_attachments` call sites in inventory, `upload_attachment` in `api_client` all removed.
