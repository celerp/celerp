# Holy Audit: Inbound Doc Receive Flow (Bill / Consignment In)

**Date:** 2026-05-29  
**Branch:** feat/inventory-transform  
**Status:** PRE-FIX - approved for implementation after review

---

## 1. What exists today (current state)

### 1.1 Two separate backend endpoints

| Endpoint | What it does |
|----------|-------------|
| `POST /docs/{id}/receive` | Creates new inventory parcels from bill line items. Emits `item.created` (new parcel) or `item.quantity.adjusted` (PO). Emits `doc.received`. Sets `received_items`, `received_item_ids`, `entity_id` on line items. |
| `POST /docs/{id}/fulfill-lines` | Marks the doc complete. For inbound docs: emits `doc.fulfilled` with `item_id: None` per line - **zero inventory touched**. For outbound docs: emits `item.fulfilled` per item (marks sold). |
| `DELETE /docs/{id}/receive` | Undo receive: archives all parcels created by receive, clears `received_items`, `received_item_ids`, entity_id from line items. |
| `POST /docs/{id}/revert-lines` | Undo fulfill: clears `fulfillment_status`. For inbound: no inventory effect. |

### 1.2 The UI wires two different actions to very different things

The bill page currently shows:
- A **"Record Receipt" collapsible form** (`po_receive_section`) - calls `POST /docs/{id}/receive`
- A **bulk toolbar dropdown** with "Receive Goods" / "Return Goods" options - calls `POST /docs/{id}/fulfill-lines` / `POST /docs/{id}/revert-lines`

There is also a dead legacy path:
- `POST /docs/{entity_id}/receive-goods` (UI route) + `api.receive_goods()` + `_render_receive_goods_section()` - this was the old one-click receive button. Disabled (returns `""`) in commit `efd52a95` but the backend route, api client method, and UI rendering function still exist as dead code.

### 1.3 What Noah actually does

Noah clicks "Receive Goods" in the bulk toolbar dropdown. This hits `fulfill-lines` which **does not create inventory**. Noah expects parcels to be created. Nothing happens.

The "Record Receipt" form (the one that actually creates parcels) is present in the UI but is being bypassed - and the design intent is that users should not need a separate form.

---

## 2. HOLY commandment violations in the current state

### DRY violations
- `_render_receive_goods_section()` exists and is called in 3 places but always returns `""`. Dead code.
- `doc_receive_goods` and `doc_undo_receive_goods` UI routes exist and call `api.receive_goods()` / `api.undo_receive_goods()` which exist in `api_client.py`. All dead code.
- `INBOUND_DOC_TYPES` is defined in **both** `doc_constants.py` (used by backend) and `celerp/services/fulfill.py` (separate frozenset). Two sources of truth.
- `_INBOUND_DOC_TYPES_UI` in `ui/routes/documents.py` line 56 - a third copy, currently empty `frozenset()`. Dead code.
- `_fin_show_fulfill` in UI must be kept in sync with `FULFILLABLE_STATUSES` in `doc_constants.py` manually. WET.

### SOLID violations
- `fulfill-lines` has two completely different behaviors depending on `doc_type in INBOUND_DOC_TYPES`: (a) marks inbound doc complete with no inventory effect, (b) marks outbound items sold. Single function, two responsibilities. The inbound branch should not exist here.
- `POST /receive` handles both "create new parcel" (bill/consignment_in) and "adjust existing item qty" (PO). Two behaviors, one function - acceptable because the split is clean on `is_inbound` and the business logic differs correctly.

### Determinism violation
- The "Receive Goods" bulk action silently does nothing to inventory for inbound docs. User clicks it, thinks goods are received, catalog unchanged. No feedback, no error.

### KISS violation
- The two-step "Record Receipt form" then "Receive Goods button" adds unnecessary complexity. For inbound docs, receiving goods IS the complete action. No separate "close doc" step is needed from a business perspective.
- The collapsible form is redundant if "Receive Goods" in the toolbar does the full job.

---

## 3. Correct design (approved)

### Core principle
For inbound docs (bill, consignment_in): **one action = receive goods + doc closed**. No two-step flow.

- Partial receives are handled **after the fact**: receive all, then adjust inventory quantities on the product card (catalog). This is the approved UX - no in-doc partial receive.
- The collapsible "Record Receipt" form is **removed entirely** - the toolbar action handles everything.

### "Receive Goods" bulk toolbar action (inbound docs)
When Noah clicks "Receive Goods":
1. A **location dropdown modal/prompt** appears (inline in the toolbar or a small overlay) so Noah can select which location stock is going to.
2. On confirm: calls `POST /docs/{id}/receive` with all line items at their full document quantities and the selected location.
3. This creates new inventory parcels, sets `entity_id` on each line item, and advances doc status to `received`.
4. Doc is now closed for inventory purposes. No separate "fulfill" step.

### "Return Goods" bulk toolbar action (inbound docs)
- Calls `DELETE /docs/{id}/receive`.
- Archives all created parcels, clears entity_ids, resets doc to `final` (pre-receive).

### fulfill-lines / revert-lines
- **Inbound docs must never reach these endpoints.** Remove the inbound branch entirely.
- Bill and consignment_in are removed from `FULFILLABLE_STATUSES`. Any call to `fulfill-lines` on an inbound doc returns 422.

---

## 4. Exact changes required

### A. Backend: `default_modules/celerp-docs/celerp_docs/doc_constants.py`
- Remove `bill` and `consignment_in` from `FULFILLABLE_STATUSES`
- Remove `INBOUND_DOC_TYPES` from this file (consolidate to one definition)

### B. Backend: `default_modules/celerp-docs/celerp_docs/routes.py`
- `fulfill_lines`: remove the `if doc_type in INBOUND_DOC_TYPES` branch entirely. Inbound docs now 422 at the `FULFILLABLE_STATUSES` gate.
- `revert_lines`: same - remove inbound branch.
- Keep `POST /receive` as-is (creates parcels for inbound, adjusts qty for PO). Already correct.
- Keep `DELETE /receive` as-is.

### C. Backend: `celerp/services/fulfill.py`
- Remove `_INBOUND_DOC_TYPES` frozenset. Import from `doc_constants.py` if still needed (audit: if `execute_fulfill` is no longer called for inbound docs after this change, `_INBOUND_DOC_TYPES` becomes entirely unused and can be deleted).
- Verify `execute_fulfill` call sites - if only reachable from `fulfill-lines` (now 422 for inbound), remove the inbound guard entirely from `execute_fulfill`.

### D. UI: `ui/routes/documents.py`
- **Delete** `_render_receive_goods_section()` and all call sites.
- **Delete** `doc_receive_goods` and `doc_undo_receive_goods` route handlers.
- **Delete** `_INBOUND_DOC_TYPES_UI` (line 56, empty frozenset).
- **Delete** the `po_receive_section` collapsible form render (entire form removed per design decision).
- **Rewire** the bulk toolbar "Receive Goods" (`li-fulfill`) for inbound docs: add a location dropdown to the toolbar section for inbound docs; on submit call `POST /docs/{id}/receive` (via a new thin UI route or directly via htmx form).
- **Rewire** the "Return Goods" (`li-revert`) for inbound docs: call `DELETE /docs/{id}/receive`.
- The `_fin_show_fulfill` logic: update to exclude bill/consignment_in.

### E. UI: `ui/api_client.py`
- `receive_goods()` - keep (calls `/receive` correctly, now used by the rewired toolbar action).
- `undo_receive_goods()` - keep (calls `DELETE /receive`).
- The dead `doc_receive_goods`/`doc_undo_receive_goods` route references: deleted when D is done.

### F. `POST /docs/{entity_id}/receive-goods` UI route
- **Delete** this route entirely (was the old one-click handler, now superseded by the rewired toolbar).

---

## 5. Unit test plan

### Tests to REMOVE (test wrong or dead behavior)

| File | Test | Reason |
|------|------|--------|
| `test_doc_workflows.py` | `test_bill_fulfill_lines_after_receive_sets_fulfillment_status` | Tests that fulfill-lines on a received bill closes the doc - this is the wrong endpoint for inbound, being removed |
| `test_doc_workflows.py` | `test_bill_revert_lines_clears_fulfillment_status` | Tests that revert-lines on a bill clears fulfillment_status - wrong endpoint, being removed |
| `test_doc_workflows.py` | `test_bill_fulfill_lines_rejected_before_receive` | Tests partial rejection (only pre-receive). Replace with total rejection test (all bill statuses → 422) |
| `test_doc_workflows.py` | `test_consignment_in_fulfill_lines_after_receive` | Tests consignment_in inbound branch of fulfill-lines - removed |
| `test_fulfillment.py` | `test_consignment_in_fulfill_lines_does_not_deduct_inventory` | Tests the inbound branch of fulfill-lines for consignment_in - correct assertion but wrong endpoint |
| `test_fulfillment.py` | `test_consignment_in_revert_lines_does_not_touch_inventory` | Same - wrong endpoint |
| `test_fulfillment.py` | `test_consignment_in_fulfill_lines_no_cogs_je` | Same |
| `test_ui.py` | `test_revert_goods_received_has_no_goods_received_badge` | Tests `_render_receive_goods_section` which is being deleted |
| `test_ui.py` | `test_receive_goods_section_shows_badge_when_no_item_ids` | Same - tests deleted function |
| `test_ui.py` | `test_bill_checkbox_enabled_without_entity_id` | Comment says "fulfill_lines ignores entity_ids for inbound" - wrong rationale. Checkbox behavior for inbound toolbar needs re-evaluation (inbound toolbar action now operates on whole doc, not per-line selection) |

### Tests to ADD

| File | Test | What it verifies |
|------|------|-----------------|
| `test_doc_workflows.py` | `test_bill_fulfill_lines_always_rejected` | `fulfill-lines` on bill at any status (draft, final, received) returns 422 |
| `test_doc_workflows.py` | `test_bill_revert_lines_always_rejected` | `revert-lines` on bill at any status returns 422 |
| `test_doc_workflows.py` | `test_consignment_in_fulfill_lines_always_rejected` | Same for consignment_in |
| `test_doc_workflows.py` | `test_consignment_in_revert_lines_always_rejected` | Same for consignment_in |
| `test_doc_workflows.py` | `test_bill_receive_with_location_sets_location_on_parcel` | `POST /receive` with `location` field creates parcel with correct location |
| `test_doc_workflows.py` | `test_consignment_in_receive_with_location_sets_location_on_parcel` | Same for consignment_in |
| `test_fulfillment.py` | `test_consignment_in_receive_does_not_deduct_inventory` | Receiving consignment_in via `POST /receive` creates new parcels (net positive), does NOT deduct existing inventory |
| `test_fulfillment.py` | `test_consignment_in_receive_no_cogs_je` | `POST /receive` on consignment_in must not create a COGS JE |
| `test_ui.py` | `test_bill_bulk_toolbar_receive_has_location_dropdown` | Inbound doc toolbar section contains a location selector element |
| `test_ui.py` | `test_collapsible_receive_form_absent_on_bill` | `po_receive_section` must not appear in bill detail HTML after the form is removed |

### Tests to KEEP (already correct, no changes)

| File | Test | Why kept |
|------|------|---------|
| `test_doc_workflows.py` | All `test_bill_receive_*` tests | Use `POST /receive` - correct endpoint |
| `test_doc_workflows.py` | `test_bill_receive_writes_entity_id_to_line_items` | Correct |
| `test_doc_workflows.py` | `test_bill_receive_sku_fallback_writes_entity_id` | Correct |
| `test_doc_workflows.py` | `test_bill_undo_receive_clears_entity_id` | Correct |
| `test_doc_workflows.py` | `test_bill_receive_known_sku_creates_new_parcel_not_adjust` | Correct |
| `test_doc_workflows.py` | `test_consignment_in_receive_known_sku_creates_new_parcel` | Correct |
| `test_doc_workflows.py` | `test_po_receive_known_item_still_adjusts_qty` | Correct - PO is unchanged |
| `test_doc_workflows.py` | `test_bill_revert_to_draft_from_final_then_re_finalize` | Correct |
| `test_fulfillment.py` | All `test_fulfill_lines_*` outbound tests (invoice, memo) | Outbound path unchanged |
| `test_fulfillment.py` | `test_consignment_in_fulfill_does_not_deduct_inventory` (old fulfill endpoint) | If still present as 404 test, can be removed as cruft |
| `test_user_journeys.py` | `test_po_receive_goods` | Tests PO receive flow - PO unchanged |

### Notes on consignment_in legacy tests in test_fulfillment.py (lines 629, 669, 698)
These (`test_consignment_in_fulfill_does_not_deduct_inventory`, `test_consignment_in_unfulfill_does_not_touch_inventory`, `test_consignment_in_fulfill_no_cogs_je`) use the old `/fulfill` endpoint that was already removed. If they currently pass as 404-assertion tests, they are cruft and must be deleted. If they still call `/fulfill-lines`, they test the inbound branch being removed - also delete.

---

## 6. Summary of net code change

| File | Action |
|------|--------|
| `doc_constants.py` | Remove bill/consignment_in from FULFILLABLE_STATUSES; consolidate INBOUND_DOC_TYPES to one location |
| `routes.py` (docs) | Remove inbound branch from fulfill-lines + revert-lines |
| `fulfill.py` | Remove `_INBOUND_DOC_TYPES`; audit and remove guard in `execute_fulfill` if unreachable for inbound |
| `documents.py` (UI) | Delete `_render_receive_goods_section`, dead routes, `_INBOUND_DOC_TYPES_UI`, `po_receive_section` form; rewire toolbar "Receive Goods" → `POST /receive` with location dropdown |
| `api_client.py` | Keep `receive_goods` + `undo_receive_goods`; dead route wrappers removed when D is done |
| `test_doc_workflows.py` | Remove 4 tests (wrong endpoint); add 6 tests (rejection + location) |
| `test_fulfillment.py` | Remove 3-6 tests (inbound fulfill-lines branch + old endpoint cruft); add 2 tests (consignment_in receive) |
| `test_ui.py` | Remove 3 tests (deleted functions + wrong rationale); add 2 tests (location dropdown + no collapsible form) |

**Net result:** one action = one endpoint = one outcome. No ambiguity about what "Receive Goods" does.
