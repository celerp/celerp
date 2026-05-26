# Per-Line Fulfill/Revert - Visual Verification Plan
Date: 2026-05-25

## Goal
Verify the per-line fulfill/revert feature works correctly end-to-end and looks professional on screen.

## Test Scope

### Screens to verify
1. **Memo Out document (finalized)** - shows per-line checkboxes + status column + bulk toolbar
2. **Consignment In document (finalized)** - shows per-line checkboxes + status column + bulk toolbar
3. **Invoice document (sent/final/paid)** - shows per-line checkboxes + status column + bulk toolbar
4. **Fulfill flow** - select lines, click Fulfill, items update to memo_out/sold status
5. **Revert flow** - select lines, click Revert, items return to available
6. **Memo→Invoice conversion** - only memo_out items convert; reverted items excluded
7. **Non-fulfillable doc types (e.g. bill, purchase_order, credit_note)** - NO fulfill UI shown (regression check)
8. **Fulfillable docs in non-fulfillable status (e.g. invoice in draft/void)** - NO fulfill UI shown

### CSS/UX criteria to check per screen
- [ ] Status badge is styled consistently (color-coded: available=grey, memo_out=orange, sold=green)
- [ ] Checkbox column is aligned and not oversized
- [ ] Bulk toolbar appears/disappears correctly (only when items checked)
- [ ] Column headers are centered
- [ ] Text columns left-aligned, status badge centered
- [ ] No layout overflow or clipping
- [ ] Fulfill/Revert buttons have correct disabled state when nothing selected
- [ ] Mobile-safe (no horizontal scroll on standard table)

## Test Steps

### Step 1: Setup
- Register user + create company
- Create 3 inventory items
- Create memo_out document with 3 line items
- Finalize memo_out document

### Step 2: Memo Out - Initial State Screenshot
- Navigate to finalized memo_out doc
- Screenshot: verify status column shows "available" for all lines
- Verify checkboxes present but toolbar hidden

### Step 3: Memo Out - Select Lines
- Check 2 of 3 checkboxes
- Screenshot: verify toolbar appears with Fulfill + Revert buttons

### Step 4: Memo Out - Fulfill Selected
- Click Fulfill
- Screenshot: 2 lines show "memo_out" status, 1 shows "available"
- Verify partial doc status in header

### Step 5: Memo Out - Revert One
- Check 1 memo_out line
- Click Revert
- Screenshot: that line back to "available"

### Step 6: Consignment In - Same Flow
- Create + finalize consignment_in doc
- Screenshot initial state
- Fulfill all lines
- Screenshot fulfilled state

### Step 7: Memo→Invoice Conversion
- With partially fulfilled memo (some memo_out, some available)
- Convert to invoice
- Verify ONLY memo_out items appear in new invoice
- Screenshot invoice line items

### Step 8: Other Doc Type Regression
- Navigate to finalized invoice/purchase_order
- Screenshot: verify NO checkboxes, NO status column, NO fulfill toolbar

## Failure Documentation
All failures will be documented with:
- Screenshot reference
- Issue description  
- Fix applied
- Verification screenshot after fix

## Output
This document will be updated in-place with screenshots referenced as:
`/mnt/storage/agent_storage/celerp/docs/screenshots/step-N-description.png`
