# Send Button Refactor Plan
Date: 2026-05-10

## Summary of 6 tasks

---

## Task 1: Hide Send + Mark as Sent on Vendor Bills (all statuses)

**Problem:** Bills (`doc_type=bill`) and Purchase Orders (`doc_type=purchase_order`) are
internal documents - we never send them to vendors. The Send and Mark as Sent buttons must
be hidden entirely for these doc types, regardless of status.

**Approach:**
- Add `NO_SEND_DOC_TYPES` frozenset to `doc_constants.py`:
  `NO_SEND_DOC_TYPES = frozenset({"bill", "purchase_order"})`
- In `_doc_detail()` in `documents.py`, gate both the Send form block and the Mark as Sent /
  Unmark Sent block behind `doc_type not in NO_SEND_DOC_TYPES`.
- Import `NO_SEND_DOC_TYPES` from `celerp_docs.doc_constants`.

**Files:**
- `default_modules/celerp-docs/celerp_docs/doc_constants.py` - add constant
- `ui/routes/documents.py` - gate send + mark_sent blocks

**Test:** Unit test in `default_modules/celerp-docs/tests/` verifying `bill` and
`purchase_order` doc detail HTML contains neither `btn.send` nor `btn.mark_as_sent`.

---

## Task 2: Hide Send + Mark as Sent on Consignment In (all statuses)

**Problem:** `consignment_in` is an internal receiving document. Same treatment as bills.

**Approach:**
- Add `consignment_in` to `NO_SEND_DOC_TYPES` frozenset (same constant as Task 1).
- No additional code change needed once constant is right.

**Test:** Same test as Task 1 expanded to also cover `consignment_in`.

---

## Task 3 + 4: Send button shown in all non-Paid/Void statuses (relay-gated)

**Problem (current behaviour):** Send button only shows in `status == "draft"`. It should
show on all statuses EXCEPT `paid` and `void`, and only when relay is connected.

**Approach:**
- Define `SEND_ALLOWED_DOC_TYPES` = all doc types that CAN show Send (everything NOT in
  `NO_SEND_DOC_TYPES`): `invoice`, `credit_note`, `consignment_out`, `list`, `proforma`, `memo`, `receipt`.
- In `_doc_detail()`: determine `relay_connected` by calling
  `api.get_relay_status(token)` (already used in topbar). Cache in request state if feasible,
  else one call per page load.
- Show Send button when:
  - `doc_type not in NO_SEND_DOC_TYPES`
  - `status not in ("paid", "void")`
  - `relay_connected == True`
- Currently the send form block is gated on `status == "draft"` - remove that gate and use
  the new condition above.
- Keep Mark as Sent available (no relay requirement) but also respect `NO_SEND_DOC_TYPES`.

**Note on `relay_connected`:** `_doc_detail()` is called as `async def` already. We can
`await api.get_relay_status(token)` there. Must handle auth failure (return `{}`) gracefully
so that if relay check errors, `connected` defaults to False and Send button is hidden.

**Files:**
- `ui/routes/documents.py` - change Send button condition

**Test:** Unit tests covering:
- invoice draft + relay connected → Send visible
- invoice final + relay connected → Send visible
- invoice paid + relay connected → Send NOT visible
- invoice void → Send NOT visible
- invoice draft + relay NOT connected → Send NOT visible
- bill (any status) → Send NOT visible

---

## Task 5: Send button opens modal popup (not inline Details/Summary)

**Problem (current behaviour):** Send shows as an inline `<Details><Summary>` accordion
in the header bar. Looks unprofessional.

**Approach:**
- Replace the `Details(Summary(...), Form(...))` block with a `Button` that triggers a modal.
- The modal (a `<dialog>` element) will contain:
  - TO field: pre-filled from `contact_email`, multiple addresses via comma (hint text below)
  - CC field: empty text input
  - BCC field: empty text input
  - Subject: pre-filled (`{DocType} #{ref_id} from {company_name}`)
  - Message: pre-filled (existing default text)
  - Send button + Cancel button
- Implementation:
  - Add a `<dialog id="send-doc-modal-{entity_id}">` element to the page (rendered at
    page-level, outside the header bar, so no z-index issues).
  - Button opens it via `document.getElementById('send-doc-modal-...').showModal()`.
  - Form inside dialog: `hx_post=f"/docs/{entity_id}/action/send"`, `hx_swap="none"`.
  - Cancel closes via `dialog.close()`.
  - On HTMX success (swap=none + 204/200 response), close modal + show toast.
- Instruction text under TO field: `"Separate multiple addresses with a comma (,)"`
- Backend `/docs/{entity_id}/send` already supports `sent_to`, add support for `cc` and
  `bcc` fields in `DocSendBody` model and forward to email service.
- UI action handler for `send` in `doc_action()` needs to read `cc` and `bcc` from form.
- Add `cc` and `bcc` to `send_email()` call in backend routes.

**Files:**
- `ui/routes/documents.py` - replace Details/Summary with Button + modal dialog render
- `default_modules/celerp-docs/celerp_docs/routes.py` - add `cc`, `bcc` to `DocSendBody`
  and email send
- `ui/static/app.css` - minimal dialog styling (if not already present; check first)

**Test:**
- The modal dialog element is rendered in the page HTML when relay connected + eligible status
- Send button is NOT a `<details>` element (assert `<dialog>` present instead)
- CC and BCC fields present in modal

---

## Task 6: Fix "Unmark Sent" (currently fails with patch error)

**Problem:** `unmark_sent` action calls `api.patch_doc(token, entity_id, {"status": "draft"})`.
Backend rejects this because `status` is a lifecycle field, not patchable via `doc.updated`.

**Root cause:** `_doc_detail()` in documents.py sends `PATCH /docs/{id}` with `{"status": "draft"}`
which hits the update endpoint that blocks status changes.

**Fix:**
- The `unmark_sent` action should call `api.revert_doc_to_draft(token, entity_id)` which
  calls `POST /docs/{id}/revert-to-draft` - the correct lifecycle endpoint.
- In `doc_action()` in `documents.py`: change `elif action == "unmark_sent":` to call
  `await api.revert_doc_to_draft(token, entity_id, reason=None)`.
- BUT: "unmark sent" should only go back to draft, not to revert-to-draft which also clears
  JEs and assignments on finalized docs. `sent` status is just `draft` + sent flag; revert
  from `sent` → `draft` via `revert-to-draft` is correct (the endpoint allows it: see
  `_REVERTABLE = {"final", "sent", "awaiting_payment"}`).
- Add `api.unmark_sent_doc()` in `api_client.py` that calls `POST /docs/{id}/revert-to-draft`.
- Actually: just call `revert_doc_to_draft` directly from `doc_action`. No new api_client method
  needed. KISS.

**Files:**
- `ui/routes/documents.py` - change `unmark_sent` branch

**Test:**
- POST to `/docs/{id}/action/unmark_sent` on a `sent` doc → calls revert-to-draft, status
  becomes `draft` (test via mock/unit test, no patch error).

---

## Execution order

1. Task 6 (5-min fix, unblocks manual testing of other tasks)
2. Task 1+2 together (same constant + same gate)
3. Task 3+4 together (relay gate + status widening)
4. Task 5 (modal - largest change)
5. Run tests for all, then commit

## Constants to add to doc_constants.py

```python
# Doc types where Send and Mark as Sent must be hidden.
# These are internal receiving/purchasing documents - never sent externally.
NO_SEND_DOC_TYPES: frozenset[str] = frozenset({"bill", "purchase_order", "consignment_in"})

# Statuses where Send is suppressed even for sendable doc types.
NO_SEND_STATUSES: frozenset[str] = frozenset({"paid", "void"})
```
