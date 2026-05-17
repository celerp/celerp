# Celerp Bug Fix Plan - Split & Category Bugs

**Date:** 2026-05-17
**Status:** Plan - awaiting implementation approval
**Doc:** https://docs.google.com/document/d/1qjeX7e0Luk6XryubuI7GOQAZqLZXpMVn3I5Ecs51P8g/edit

---

## Bug 1: Category shows empty on first selection (detail page row-reload misfires)

### Root Cause

`field_patch` for `field=="category"` returns a hidden `Div` targeting `#row-{safe_id}`. That element is a table row in the inventory list. On the item **detail page** (`/inventory/item:...`), no such element exists - HTMX silently fails, leaving the category cell blank. The attribute fields also stay stale.

Two sub-problems:
- (a) Row-reload div fires on detail page where `#row-...` doesn't exist
- (b) `item_detail` injects `cat_names = sorted(cat_schemas.keys())` (slugs) as category options - display names not applied on detail page

### Fix

**Sub-fix 1a - Context-aware response in `field_patch`:**

Check `HX-Current-URL` request header to determine caller context:
- URL contains `/inventory/item:` → detail page path
- Otherwise → list path (existing row-reload behavior is correct)

On the detail page, after category save:
1. Return the category `display_cell` td normally (so the cell updates immediately)
2. Add an OOB swap to reload the attributes section: `Div(hx_get=f"/api/items/{entity_id}/attributes-section", hx_trigger="load", hx_swap="outerHTML", hx_swap_oob="true", id="item-attributes-section")`

Add `id="item-attributes-section"` to the attributes `detail-card` div in `_item_detail_tabs`.

Add `GET /api/items/{entity_id}/attributes-section` endpoint that re-fetches item + cat_schemas and renders just the attributes card with merged category-specific fields.

**Sub-fix 1b - Display names on detail page:**

In `item_detail`, fetch `category_display_names` alongside existing API calls. Build category options as `[(slug, display_name)]` tuples and pass a `label_map` when rendering the category cell.

### Tests
- `test_category_field_patch_detail_page_no_row_reload` - set HX-Current-URL to detail page URL, assert response does NOT contain `hx-get="/api/items/.../row"`
- `test_category_display_name_on_detail_page` - render item detail with a slug category, assert display name shown

---

## Bug 2: Large child pieces not rejected immediately - only fails on submit

### Root Cause

`child_pieces` has `max` HTML attribute set to `parent_pieces - 1`. Browser `max` validation fires only on form submit, not while typing. `splitRecalcMotherPieces` updates the mother display but does not clamp the input value.

### Fix

In `splitRecalcMotherPieces` in `_BULK_SPLIT_JS`:

```javascript
function splitRecalcMotherPieces(input) {
  var form = input.closest('form');
  if (!form) return;
  var parentPieces = parseFloat(form.dataset.parentPieces || '0');
  var maxVal = Math.max(0, parentPieces - 1);
  // Clamp immediately on input
  var childP = Math.min(Math.max(0, parseFloat(input.value) || 0), maxVal);
  input.value = String(Math.round(childP));
  var remP = Math.max(0, parentPieces - childP);
  var mp = form.querySelector('.mother-pieces-display');
  if (mp) mp.textContent = String(Math.round(remP));
}
```

### Tests
- `test_split_pieces_js_clamps_on_input` - assert `splitRecalcMotherPieces` source contains `Math.min` clamping (regression guard)

---

## Bug 3: Weight not enforced when sell_unit=piece (secondary weight field has no conservation)

### Root Cause

When `sell_unit` is a piece-type unit (not a weight unit), weight is a secondary field. In the split preview:
- `child_weight` is editable - user enters it
- `mother_weight` is also an editable input (`weight_name="mother_weight"` in `_parcel_row`)
- `splitRecalcMotherWeight` has bidirectional recalc but only when `data-user-edited` is set - if user edits child_weight, mother auto-updates, which is correct
- However: no `max` on `child_weight` - user can enter `child_weight > parent_weight`
- No clamping on `child_weight` input
- `SplitBody.mother_weight` is accepted from the client without validation - user can submit arbitrary values

The result: weight conservation is not enforced. User can enter child_weight=999 on a parent with weight=2.47 and it submits fine.

### Agreed model (from earlier weight discussion)

Weight follows the same model as pieces:
- **Child weight**: editable. Default = proportional. User enters actual scale reading.
- **Mother weight**: static display only = `parent_weight - child_weight`. Never editable in split UI.
- No server-side sum enforcement (physical scale rounding discrepancy is accepted).
- After split, user can re-weigh and update mother via normal item edit.

### Fix

**Frontend (`ui/routes/inventory.py`):**

In `_parcel_row`, for the mother row: always render weight as static display (like mother pieces):
```python
cells.append(Td(Span(wfmt.format(weight_val or 0), cls="mother-weight-display"), cls="sp-td"))
```
Remove `weight_name="mother_weight"` from the mother row call (pass `weight_name=None`).

Add `min="0"` and `max=str(parent_weight)` to the `child_weight` input in `_editable_td`.

Simplify `splitRecalcMotherWeight` - no bidirectional logic needed (mother is never edited):

```javascript
function splitRecalcMotherWeight(input) {
  var form = input.closest('form');
  if (!form) return;
  var parentWeight = parseFloat(form.dataset.parentWeight || '0');
  var decimals = parseInt(form.dataset.weightDecimals || '2', 10);
  // Clamp child to [0, parentWeight]
  var childVal = Math.min(Math.max(0, parseFloat(input.value) || 0), parentWeight);
  input.value = childVal.toFixed(decimals);
  var mw = form.querySelector('.mother-weight-display');
  if (mw) mw.textContent = Math.max(0, parentWeight - childVal).toFixed(decimals);
}
```

Remove `_mother_weight_oninput` variable and all references to it.

**Backend (`default_modules/celerp-inventory/celerp_inventory/routes.py`):**

In `SplitBody`: remove `mother_weight` field. Mother weight is now always computed server-side.

In `split_item`: compute `mother_weight = parent_weight - total_child_weight` server-side (where `total_child_weight = sum(c.weight for c in children if c.weight is not None)`). Apply it via `item.updated` event if `parent_weight is not None`.

Remove the `if payload.mother_weight is not None` branch - replace with computed value.

### Tests
- `test_split_mother_weight_is_static` - assert no `input[name="mother_weight"]` in split preview HTML
- `test_split_mother_weight_computed_server_side` - split with child_weight, assert mother gets `parent_weight - child_weight`
- `test_split_weight_js_clamps_child` - assert `splitRecalcMotherWeight` source contains clamping (regression guard, same pattern as pieces)

---

## Bug 4: Negative weights accepted

### Root Cause

No validation in `patch_item` (item field edit) or `split_item` (child weight in SplitChild) for `weight >= 0`.

### Fix

**Backend `celerp/routers/items.py` (or wherever `patch_item` processes weight):**
- On weight field update: if `weight < 0`, return 422 "Weight cannot be negative"

**Backend `split_item` in `celerp_inventory/routes.py`:**
- Validate each `child.weight`: if `< 0`, return 422 "Child weight cannot be negative"

**Frontend:**
- `child_weight` input: add `min="0"` (already covered in Bug 3 fix)

### Tests
- `test_patch_item_negative_weight_rejected` - PATCH `weight=-1`, assert 422
- `test_split_negative_child_weight_rejected` - split with `child.weight=-1`, assert 422

---

## Implementation Order

1. **Bug 4** - pure backend validation, smallest scope (~15min)
2. **Bug 3** - make mother_weight static + clamp + remove from SplitBody (~30min)
3. **Bug 2** - JS clamp for pieces on input (~10min)
4. **Bug 1** - context-aware category save + attributes OOB reload + display names on detail page (~45min)

Total estimated: ~1h 40min

---

## DRY/GDR/HOLY Check

- **DRY**: `_parcel_row` is single source for weight/pieces cell rendering. Mother weight made static there = static everywhere. `splitRecalcMotherWeight` simplified to one pattern matching `splitRecalcMotherPieces`.
- **SOLID**: `split_item` takes sole responsibility for computing mother weight - no client input accepted.
- **No backward compat**: `SplitBody.mother_weight` removed. Any client sending it will have it ignored (field not in model).
- **GDR (no restriction)**: Weight/pieces clamping shows user the corrected value rather than blocking input. User can always see what the system accepts.
- **HOLY**: No new abstractions. All fixes are minimal targeted changes.
- **Tests first**: Failing test written before each fix, including JS source regression guards.
