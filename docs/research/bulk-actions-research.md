# Bulk Actions Research
_Date: 2026-05-03_

## Scope

Two separate investigations:
1. Bulk actions on the **List document detail page** (line items inside a list/quotation)
2. Bulk actions on the **Contacts list page** (customers/vendors)

---

## 1. List Detail Page - Line Item Bulk Actions

### Current state

The list detail page (`/lists/{id}`) renders line items via `_li_editable_row()` in
`ui/routes/documents.py`. There are **no checkboxes, no selection state, and no bulk action
toolbar** on individual line items. Each row has a single "x" remove button.

### Historical state

Searching all git history (`git log --all`), there is **no prior commit that ever added
bulk actions to the list detail line items table**. The feature was never built. The
"list-level bulk actions" commit (`15f32d0`, 2026-03-10) added bulk actions to the
*inventory list page*, not to the list document detail page.

The doc line items table (`doc-lines`) has always been a simple editable form: add,
inline-edit, remove one row at a time. No multi-select was ever introduced here.

### What would make sense

The line items table on a list/quotation document is fundamentally a **draft form**, not a
data browser. The user's workflow is:

1. Add items (scan, type SKU, or import via CSV)
2. Edit quantities/prices inline
3. Remove individual lines they don't want
4. Convert the list to an invoice when ready

**Proposed line-item bulk actions (in priority order):**

| Action | Value | Complexity |
|---|---|---|
| **Delete selected rows** | Remove multiple lines at once when trimming a large list | Low - JS only (client-side row removal, save on next autosave) |
| **Reprice selected rows** | Apply a specific price list to only selected items | Medium - needs a partial reprice endpoint |
| **Duplicate selected rows** | Copy 1+ lines (useful for repeat variants) | Low - JS only |
| **Move up/down (reorder)** | Reorder multiple lines at once | Low - drag-and-drop exists for single rows already |

**My opinion:** Only "delete selected rows" is worth doing now. The others have very narrow
use cases. The CSV import button already handles the "add many items quickly" scenario.
Delete-selected is the only action that genuinely saves clicks (removing 10 lines currently
requires 10 button clicks).

**GDR note:** Per GDR rules, the interface must NOT restrict - the "x" button must stay on
every row. The checkbox+delete is additive convenience, not a replacement.

---

## 2. Contacts List Page - Bulk Actions

### Current state

The contacts list (`/customers`, `/vendors`) calls `data_table(..., show_checkboxes=False)`
explicitly. This was set in the contacts overhaul commit (`4adc71f`, 2026-03-26) which
migrated contacts to the `data_table` component. The checkboxes were never wired up in the
new architecture.

There is a dangling handler at `ui/routes/contacts.py:2075`:
```python
return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
```
This is dead code - there is no `bulk-action-result` div rendered on the contacts page,
and no way for a user to trigger this route.

### Historical state

Before the contacts overhaul, the old `crm.py` did not have a bulk toolbar either
(the overhaul replaced custom table markup with the `data_table` component). So there was
no functioning bulk action system on contacts in either architecture. The dangling handler
is an artifact of copy-paste when migrating routes, not a regression.

### What would make sense

Contacts are the least frequently manipulated entity in the system (users add/edit
contacts one at a time, rarely in bulk). However, two scenarios justify a minimal bulk
toolbar:

| Action | Value | Notes |
|---|---|---|
| **Delete selected** | Clean up duplicate/test contacts | Needs confirmation dialog |
| **Export selected (CSV)** | Send a contact subset to an external tool | Additive - existing full-export already works |
| **Change type (customer ↔ vendor)** | Misclassified contacts | Medium - PATCH endpoint needed |
| **Merge** | Deduplicate contacts | High complexity, deferred |

**My opinion:** Implement delete + export-selected only. Type-change is niche. Merge is
complex and rarely needed.

---

## Implementation Plan (not started)

### Contacts bulk actions

**Approach - reuse existing `data_table` + `_bulk_js` machinery:**

1. Remove `show_checkboxes=False` from the `data_table()` call in `_contacts_content()`.
2. Add a contacts bulk toolbar component (analogous to `_bulk_toolbar()` in
   `ui/routes/inventory.py`) rendered in `_contacts_page_shell()`.
3. Wire two actions:
   - `delete`: POST `/api/contacts/bulk/delete` - new backend route in
     `celerp-contacts` (or existing contacts router). Returns `bulk-action-result`.
   - `export`: navigate to `/contacts/{type}s/export/csv?selected=id1&id2...`
     (extend existing CSV export to filter by `selected` param).
4. Remove the dangling `bulk-action-result` handler at contacts.py:2075 or repurpose it
   as the actual delete handler.

**DRY check:** The `CelerpSelection` JS object, `_bulk_js` script block, and bulk toolbar
CSS already exist. No duplication needed. The contacts toolbar is a new Python function
returning `FT`, analogous to `_bulk_toolbar()` in inventory.

**KISS check:** Two actions (delete + export). No new JS beyond what `_bulk_js` already
provides. The `bulkActionChanged` function already handles `delete` and navigate actions.

**HOLY check:**
- DRY: reuse `CelerpSelection`, `_bulk_js`, existing CSS classes
- SOLID: contacts bulk handler is its own route, not mixed into existing routes
- No backward compat: `show_checkboxes=False` is removed, not shimmed
- Deterministic: no fallbacks; missing selection returns clear validation message
- GDR: interface not restricted; delete button calls confirm dialog (GDR compliant)

**Files to change:**
- `ui/routes/contacts.py`: remove `show_checkboxes=False`, add `_contacts_bulk_toolbar()`,
  repurpose/fix dangling handler at line 2075
- `default_modules/celerp-contacts/celerp_contacts/routes.py`: add
  `POST /api/contacts/bulk/delete` + extend CSV export to accept `selected` param
- `tests/test_routers/test_contacts.py` (or equivalent): add 2 bulk action tests

### List detail page - delete selected line items

**Approach - JS-only, no new backend route:**

Line item removal is already client-side (`this.closest('tr').remove(); celerpUpdateTotals();
celerpAutoSave()`). Adding a "delete selected" to line items just needs:

1. Add a checkbox column to `_li_editable_row()` (hidden by default via CSS, shown when
   JS detects 1+ checked).
2. A "Delete selected" button that appears in the line toolbar when checkboxes are checked.
3. Onclick: remove all checked rows, call `celerpUpdateTotals()` and `celerpAutoSave()`.

**No new backend route needed.** The existing autosave endpoint accepts the new line item
array after client-side removal.

**Files to change:**
- `ui/routes/documents.py`: `_li_editable_row()` gets a checkbox `<td>`; line toolbar
  gets a conditional delete-selected button; small JS block added to the `Script` at
  end of `lines_section`.

**GDR:** Individual "x" buttons stay. The checkbox+delete is additive convenience.

---

## Priority Recommendation

1. **Contacts delete + export-selected** (P1) - fixes dead code, gives Noah a real tool
2. **List detail delete-selected rows** (P2) - genuinely useful but not urgent; CSV
   import already handles the "add many" direction, and lists are usually short

Neither should be started without explicit go-ahead from Noah.
