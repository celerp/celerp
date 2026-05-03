# Bulk Actions Implementation Plan
_Date: 2026-05-03 | Status: APPROVED FOR IMPLEMENTATION_

This document is the single authoritative implementation specification.
Every decision is final. No alternatives are listed - the optimal path
for KISS/DRY/HOLY is stated once.

---

## Part 1 - Line Items Bulk Actions (All Document Types)

### What we are building

Two new actions on the line items table of all document detail pages
(invoices, bills, lists, POs, credit notes, receipts, memos, consignment):

| Action | Trigger |
|---|---|
| **Delete selected rows** | Checkbox(es) checked, pick from dropdown |
| **Print selected rows** | Checkbox(es) checked, pick from dropdown |

For **print labels** (barcode/QR via celerp-labels): this option appears in the
line items toolbar ONLY when celerp-labels is installed. It is driven by the same
`bulk_action` slot mechanism used on the inventory page. If celerp-labels is not
installed, the option is absent.

### Mechanic - Delete

Client-side only. No new backend route.

The existing "x" button already does:
```
this.closest('tr').remove(); celerpUpdateTotals(); celerpAutoSave();
```
"Delete selected" does the same to all checked rows at once. `celerpAutoSave()`
submits the updated `line_items` array to the existing save endpoint.

### Mechanic - Print (window.print)

Client-side only. No new backend route.

1. Add CSS class `doc-lines--selecting` to the table when any checkbox is checked.
2. A `@media print` rule hides all rows that do NOT have `.li-select:checked`.
3. "Print selected" calls `window.print()`.

The existing PDF button remains for full-document PDF export.

### Mechanic - Print Labels (celerp-labels slot)

Follows the identical pattern to the inventory bulk print labels action:
- `get_slot("bulk_action")` is called inside `_li_bulk_toolbar()`.
- If an action with `_module == "celerp-labels"` is found, a "Print Labels"
  option is rendered in the toolbar dropdown.
- The option value is `mod:labels_print-bulk` - identical to inventory.
- JS syncs checked line item `entity_id` values into `CelerpSelection`, then
  calls `bulkActionChanged("mod:labels_print-bulk")`.
- No new route, no new template - `/labels/print-bulk` and `tpl-mod-labels_print-bulk`
  already exist and work.

**DRY guarantee:** Labels bulk print path is identical to inventory. The only
new code is reading `get_slot("bulk_action")` inside `_li_bulk_toolbar()`.

### Implementation

**New helper `_li_bulk_toolbar()` in `ui/routes/documents.py`:**

```python
def _li_bulk_toolbar(entity_id: str, is_list: bool) -> FT:
    from celerp.modules.slots import get as get_slot
    labels_action = next(
        (a for a in get_slot("bulk_action") if a.get("_module") == "celerp-labels"),
        None,
    )
    options = [
        Option("Action", value="", disabled=True, selected=True),
        Option("Delete selected", value="li-delete"),
        Option("Print selected", value="li-print"),
    ]
    if labels_action:
        options.append(Option("Print Labels", value="mod:labels_print-bulk"))
    return Div(
        Span("0 rows selected", id="li-bulk-count", cls="bulk-count"),
        Select(*options, id="li-bulk-select", cls="form-input form-input--sm",
               onchange="liActionChanged(this.value)"),
        Div(id="li-bulk-context"),
        id="li-bulk-toolbar",
        cls="bulk-toolbar",
        style="display:none",
    )
```

**`_li_editable_row()` change:**
- First cell: `Td(Input(type="checkbox", cls="li-select", value=li_entity_id), cls="col-checkbox li-checkbox-cell")`.
- Header row: matching `Th(Input(type="checkbox", id="li-select-all"), cls="col-checkbox li-checkbox-cell")`.

**JS block (appended to `lines_section` Script):**

```js
(function(){
  var table=document.querySelector('.doc-lines');
  var toolbar=document.getElementById('li-bulk-toolbar');
  var countEl=document.getElementById('li-bulk-count');
  var sel=document.getElementById('li-bulk-select');
  function _n(){return table?table.querySelectorAll('tbody .li-select:checked').length:0;}
  function _update(){
    var n=_n();
    if(countEl) countEl.textContent=n+' row'+(n===1?'':'s')+' selected';
    if(toolbar) toolbar.style.display=n>0?'flex':'none';
    if(sel&&n===0) sel.value='';
  }
  if(table) table.addEventListener('change',function(e){
    if(e.target&&e.target.classList.contains('li-select')) _update();
  });
  var sa=document.getElementById('li-select-all');
  if(sa) sa.addEventListener('change',function(){
    if(table) table.querySelectorAll('tbody .li-select').forEach(function(cb){cb.checked=sa.checked;});
    _update();
  });
  window.liActionChanged=function(action){
    if(!action) return;
    if(action==='li-delete'){
      if(table) table.querySelectorAll('tbody .li-select:checked').forEach(function(cb){cb.closest('tr').remove();});
      celerpUpdateTotals(); celerpAutoSave(); _update(); return;
    }
    if(action==='li-print'){
      var hidden=[];
      if(table) table.querySelectorAll('tbody tr').forEach(function(tr){
        var cb=tr.querySelector('.li-select');
        if(cb&&!cb.checked){tr.style.display='none';hidden.push(tr);}
      });
      window.print();
      hidden.forEach(function(tr){tr.style.display='';});
      if(sel) sel.value=''; return;
    }
    if(action.startsWith('mod:')){
      CelerpSelection.clear();
      if(table) table.querySelectorAll('tbody .li-select:checked').forEach(function(cb){
        if(cb.value) CelerpSelection.add(cb.value,{});
      });
      bulkActionChanged(action);
    }
  };
})();
```

**Files changed:**
- `ui/routes/documents.py`: `_li_bulk_toolbar()` (new), `_li_editable_row()` (checkbox cell + header), `lines_section` div (add toolbar above table + JS block)

**Tests:**
- `tests/test_routers/test_doc_workflows.py`: assert editable rows contain `.li-select` checkbox; assert `li-bulk-toolbar` div present in doc detail response.

---

## Part 2 - Contacts Bulk Actions

### What we are building

Three bulk actions on the customers and vendors list pages:

| Action | Notes |
|---|---|
| **Delete selected** | Guarded: blocked if any contact has linked non-void documents |
| **Export selected (CSV)** | Navigate action - downloads filtered CSV |
| **Merge** | Pick winner, re-point all linked data, tombstone losers |

### Architecture

`_contacts_bulk_toolbar()` in `ui/routes/contacts.py`, modelled on `_bulk_toolbar()`
in inventory. `CelerpSelection` and `_bulk_js` from `data_table()` handle selection
state - no new JS objects.

`show_checkboxes=False` is removed from the `data_table()` call.

**`selection_key` param on `data_table()`:** `_bulk_js` uses a hardcoded
`'celerp_inv_selection'` sessionStorage key. To prevent collision with inventory
selections, `data_table()` accepts a new `selection_key: str = "celerp_inv_selection"`
param threaded into the JS `KEY` constant. Contacts pass `"celerp_contact_selection"`.
Inventory is unchanged (default).

### Backend routes (new in `celerp-contacts/celerp_contacts/routes.py`)

#### `POST /crm/contacts/bulk/delete`

```
Body: {"contact_ids": ["id1", "id2"]}
```

1. Fetch each contact projection; verify exists and not already deleted.
2. Query doc projections for any doc with `contact_id in contact_ids` and
   `status not in ("void", "draft")`. If any found: return 422 with detail
   listing the blocking contacts and their open doc counts.
3. Emit `crm.contact.updated` with `{fields_changed: {deleted: {new: true}}}` for each.
4. Return `{"deleted": N}`.

#### `POST /crm/contacts/merge`

```
Body: {
  "target_contact_id": "uuid-of-winner",
  "source_contact_ids": ["uuid-loser-1", ...]
}
```

Steps (sequential, no branches):

1. **Validate inputs:** `source_contact_ids` non-empty; `target_contact_id` not in sources; all IDs belong to current company.
2. **Validate target:** projection exists, `deleted != true`.
3. **Validate sources:** for each source, projection exists, `deleted != true`, `merged_into` is null (already-merged contact raises 422: "Contact X is already merged into Y").
4. **Compute merged people:** start with winner's `people` list. For each source, append any person whose `email` (case-insensitive, stripped) is not already present. Persons without email: append if `name` not already present.
5. **Compute merged addresses:** start with winner's `addresses`. For each source, append any address whose `(line1.lower().strip(), postcode.lower().strip())` tuple is not already present.
6. **Compute merged tags:** set union of all tags across winner + sources.
7. **Emit single `crm.contact.merged` event on winner** carrying `source_contact_ids`, `merged_people`, `merged_addresses`, `merged_tags`. The projection applies all field updates from this one event. Extend `CrmContactMerged` schema to include these fields.
8. **Tombstone sources:** for each source, emit `crm.contact.updated` with `{fields_changed: {deleted: {new: true}, merged_into: {new: target_contact_id}}}`.
9. **Re-point documents:** query all doc projections where `contact_id in source_contact_ids`. For each, emit `doc.updated` with `fields_changed: {contact_id: {new: target_contact_id}, contact_name: {new: winner_name}}`. This bypasses `patch_doc` (which blocks on non-draft status) and calls `emit_event` directly, which the projection applies regardless of doc status.
10. **Re-point deals:** same pattern via `crm.deal.updated` for any deals with `contact_id in source_contact_ids`.
11. **Notes:** NOT re-parented. No events emitted. The contact detail page already queries note projections by `contact_id`; update that query to include `winner.merged_from` IDs so all historical notes remain visible on the winner's page.
12. Commit session.
13. Return `{"merged_into": target_contact_id, "sources_merged": N, "docs_updated": N, "warnings": [...]}`.

**Currency mismatch:** if winner and any source have different `currency` values, proceed anyway. Include `"Currency mismatch: winner currency X applies to contact record; existing documents keep their own currencies."` in `warnings`.

**Edge cases:**

| Case | Handling |
|---|---|
| Source already merged (`merged_into` set) | 422: "Contact X is already merged into Y" |
| Target is deleted | 422: "Cannot merge into a deleted contact" |
| Source has open documents | Allowed - docs get re-pointed to winner |
| Currency mismatch | Proceed; warning in response |
| >50 affected documents | Proceed; no limit (each emit is a flush, not a round-trip) |

#### `CrmContactMerged` schema update (`celerp/events/schemas.py`)

```python
class CrmContactMerged(BaseModel):
    source_contact_ids: list[str]
    merged_people: list[dict] = Field(default_factory=list)
    merged_addresses: list[dict] = Field(default_factory=list)
    merged_tags: list[str] = Field(default_factory=list)
```

#### Projection update (`celerp-contacts/celerp_contacts/projections.py`)

```python
elif event_type == "crm.contact.merged":
    current.setdefault("merged_from", [])
    current["merged_from"] = sorted(set(current["merged_from"]) | set(data["source_contact_ids"]))
    if data.get("merged_people"):
        current["people"] = data["merged_people"]
    if data.get("merged_addresses"):
        current["addresses"] = data["merged_addresses"]
    if data.get("merged_tags"):
        current["tags"] = data["merged_tags"]
```

#### Extend `GET /crm/contacts/export/csv`

Add optional `selected: list[str] = Query(default=[])` param. When present,
filter to those IDs only. Existing full-export behavior unchanged when absent.

The UI redirect routes at `/contacts/customers/export/csv` and
`/contacts/vendors/export/csv` forward the `selected` param to `/crm/export/csv`.

### Frontend

**`_contacts_bulk_toolbar(contact_type)` in `ui/routes/contacts.py`:**

```python
def _contacts_bulk_toolbar(contact_type: str) -> FT:
    return Div(
        Span("0 selected", id="contact-bulk-count", cls="bulk-count"),
        Button("Clear", id="contact-bulk-clear", cls="btn btn--ghost btn--sm",
               style="display:none",
               onclick="CelerpSelection.clear();CelerpSelection.syncCheckboxes();"
                       "document.getElementById('contact-bulk-count').textContent='0 selected';"
                       "document.getElementById('contact-bulk-toolbar').classList.remove('is-active');"
                       "this.style.display='none';"),
        Select(
            Option("Action", value="", disabled=True, selected=True),
            Option("Export selected (CSV)", value="contact-export"),
            Option("Merge", value="contact-merge"),
            Option("Delete", value="delete"),
            id="contact-bulk-select", cls="form-input form-input--sm",
            onchange="contactBulkChanged(this.value)",
        ),
        Div(id="contact-bulk-context"),
        Div(id="bulk-action-result"),
        _contact_bulk_templates(contact_type),
        id="contact-bulk-toolbar", cls="bulk-toolbar",
    )
```

**`contactBulkChanged(action)` inline Script on the contacts page:**
- `"contact-export"`: build URL `/{type}s/export/csv?selected=id1&selected=id2...` from `CelerpSelection.ids()`, assign `window.location.href`.
- `"delete"`: confirm dialog, HTMX POST to `/crm/contacts/bulk/delete` with selected IDs, swap into `#bulk-action-result`.
- `"contact-merge"`: clone `tpl-contact-merge` into `#contact-bulk-context`.

**Merge template `tpl-contact-merge`:** table of selected contacts (name + type from
`CelerpSelection.all()` metadata). Each row has a "Set as primary" radio. Submit
disabled until radio selected. On submit: POST to `/crm/contacts/merge` with
`target_contact_id` + `source_contact_ids`. On success: server returns
`HX-Redirect` to `/contacts/{winner_id}`.

Contact row checkboxes store `meta = {name, contact_type}` via `data-name` and
`data-contact-type` attributes (analogous to inventory's `data-sku`, `data-name`, etc.).

**Fix dangling handler (contacts.py line 2075):** audit what route owns it. Delete if
truly orphaned. No dead code survives.

### `data_table()` change (`ui/components/table.py`)

Add `selection_key: str = "celerp_inv_selection"` parameter. Thread into `_bulk_js`:
```js
var KEY='celerp_inv_selection'  →  var KEY='{selection_key}'
```
All existing callers unchanged (default value).

### `_contacts_content()` change

```python
table_content = data_table(
    schema, contacts,
    ...
    show_checkboxes=True,                          # was False
    selection_key="celerp_contact_selection",      # new
    ...
)
```

### Files changed

| File | Change |
|---|---|
| `ui/components/table.py` | Add `selection_key` param to `data_table()` |
| `ui/routes/contacts.py` | Remove `show_checkboxes=False`, add `selection_key=`, add `_contacts_bulk_toolbar()`, `_contact_bulk_templates()`, `contactBulkChanged` Script, fix line 2075 |
| `ui/routes/documents.py` | `_li_bulk_toolbar()`, checkbox cell in `_li_editable_row()`, toolbar in `lines_section`, JS block |
| `default_modules/celerp-contacts/celerp_contacts/routes.py` | `POST /crm/contacts/bulk/delete`, `POST /crm/contacts/merge`, extend CSV export |
| `celerp/events/schemas.py` | Extend `CrmContactMerged` with `merged_people`, `merged_addresses`, `merged_tags` |
| `default_modules/celerp-contacts/celerp_contacts/projections.py` | Apply merged fields in `crm.contact.merged` handler |
| `ui/api_client.py` | Add `merge_contacts()` and `bulk_delete_contacts()` client methods |

### Tests (`tests/test_routers/test_contacts_bulk.py`, new file)

- `test_bulk_delete_contacts_success`
- `test_bulk_delete_contacts_blocked_by_open_docs`
- `test_merge_contacts_success_repoints_docs_and_contact_name`
- `test_merge_contacts_merges_people_dedup_by_email`
- `test_merge_contacts_merges_addresses_dedup_by_line1_postcode`
- `test_merge_contacts_rejects_deleted_target`
- `test_merge_contacts_rejects_already_merged_source`
- `test_merge_contacts_warns_on_currency_mismatch`
- `test_export_selected_contacts_csv`
- `test_contacts_bulk_toolbar_rendered_on_list_page`

---

## HOLY Compliance Check

- **DRY**: `CelerpSelection`, `_bulk_js`, bulk toolbar CSS all reused. Labels slot driven by same `get_slot("bulk_action")` call as inventory. `selection_key` added once to `data_table()`. Notes shown via query extension - no data duplication.
- **SOLID**: `_li_bulk_toolbar()` renders toolbar only. Merge route executes merge only. No cross-cutting concerns.
- **No backward compat**: `show_checkboxes=False` removed. `selection_key` defaults to existing value so inventory unchanged.
- **Deterministic**: every action has a single code path. `crm.contact.merged` carries all merged data in one event - one event, one projection update. Doc re-pointing emits `doc.updated` directly (bypassing the route-level draft check which is not a projection constraint).
- **KISS**: Line items JS ~50 lines. Contact merge route ~80 lines of sequential validation + emit calls. No new JS objects.
- **Cruft**: Dangling contacts handler line 2075 removed.
- **GDR**: "x" buttons stay on every line item row (additive). Contact delete blocked by open docs returns validation message, not hidden button.

---

## Implementation Order

1. `data_table()` `selection_key` param - 1 file, unblocks contacts
2. Line items bulk toolbar + JS - doc detail, no backend changes
3. Contacts bulk delete + export-selected - backend + UI, simple logic
4. Contacts merge - most complex; implement after simpler bulk actions verified
