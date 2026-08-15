# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests for the inventory audit, which is just a `list` with list_type="audit".

The audit renders through the SAME shared list/`_doc_detail` layout as a quotation/transfer and
differs only in type-appropriate columns (On hand + Counted, money hidden) and the type's terminal
action (Adjust stock). It rides the unified lifecycle draft -> finalized -> closed: a draft manifest
is built, Finalize freezes on-hand and opens counting, then Adjust closes it (reversible via Undo).
Reached at `/lists/{id}` and `/lists/new-audit`; the old `/audits/*` page tree no longer exists.

Flow proven end to end: finalize -> scan (highlight) -> count -> Adjust stock (item qty changes)
-> Undo (reverts). Plus that other list types have no audit columns and `/audits/*` is gone.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _seed_audit(api):
    tag = uuid.uuid4().hex[:6]
    loc = api.post("/companies/me/locations", json={"name": f"Aud-{tag}", "type": "warehouse"})
    loc_id = loc.json()["id"]
    bc_a, bc_b = str(uuid.uuid4().int)[:12], str(uuid.uuid4().int)[:12]
    a = api.post("/items", json={"status": "available", "sku": f"AUD-{tag}-A", "name": "Widget A", "sell_by": "piece",
                 "quantity": 10, "barcode": bc_a, "location_id": str(loc_id)})
    api.post("/items", json={"status": "available", "sku": f"AUD-{tag}-B", "name": "Widget B", "sell_by": "piece",
             "quantity": 4, "barcode": bc_b, "location_id": str(loc_id)})
    a_id = a.json()["id"]
    audit = api.post("/lists/audit", json={"location_id": str(loc_id)})
    return audit.json()["id"], a_id, bc_a


def test_audit_renders_as_shared_list_and_adjusts(page, ui_server, api):
    """An audit renders as a normal list (money hidden, On hand/Counted shown). Finalize opens
    counting; scan -> count -> Adjust changes the item quantity; Undo reverts."""
    audit_id, a_id, bc_a = _seed_audit(api)

    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    page.wait_for_selector(".doc-detail", timeout=8000)

    # Draft manifest: the LIST TYPE selector is an editable dropdown (type switchable in draft only).
    assert page.locator(".list-type-bar select").count() >= 1, "LIST TYPE dropdown missing in draft"
    # Type-appropriate columns: On hand + Counted shown; the shared scan bar is present.
    assert page.locator(".col-counted").count() >= 1, "Counted column missing"
    assert page.locator(".col-onhand").count() >= 1, "On hand column missing"
    assert page.locator("#scan-bar-input").count() == 1, "shared scan bar missing"

    # Finalize freezes on-hand and opens the counting stage.
    assert page.request.post(f"{ui_server}/lists/{audit_id}/action/finalize").ok
    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    page.wait_for_selector(".doc-detail", timeout=8000)

    # Scan item A -> its row highlights as audited (presence recorded).
    page.locator("#scan-bar-input").fill(bc_a)
    page.locator("#scan-bar-input").press("Enter")
    page.wait_for_selector(".data-row--audited", timeout=8000)
    assert page.locator(".data-row--audited").count() == 1

    # Set the counted qty to 8 via the line route (what the inline Counted cell posts).
    assert page.request.post(f"{ui_server}/lists/{audit_id}/line/{a_id}", form={"counted_qty": "8"}).ok

    # Adjust stock -> item A quantity becomes the counted 8.
    assert page.request.post(f"{ui_server}/lists/{audit_id}/action/adjust").ok
    assert api.get(f"/items/{a_id}").json()["quantity"] == 8

    # Undo stock adjustment -> back to 10.
    assert page.request.post(f"{ui_server}/lists/{audit_id}/action/undo-adjust").ok
    assert api.get(f"/items/{a_id}").json()["quantity"] == 10


def test_audit_counted_blank_until_set(page, ui_server, api):
    """An uncounted line shows '--' (not 0); a counted 0 is distinct from blank (finalized stage)."""
    audit_id, a_id, _bc = _seed_audit(api)
    assert page.request.post(f"{ui_server}/lists/{audit_id}/action/finalize").ok  # counting needs finalize
    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    page.wait_for_selector(".col-counted", timeout=8000)
    texts = page.locator(".col-counted .editable-cell").all_inner_texts()
    assert texts and all(t.strip() in ("--", "") for t in texts), f"uncounted cells should be --, got {texts}"
    # A count of 0 must persist as 0, not collapse to blank.
    assert page.request.post(f"{ui_server}/lists/{audit_id}/line/{a_id}", form={"counted_qty": "0"}).ok
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(".col-counted", timeout=8000)
    assert page.locator(".col-counted .editable-cell").filter(has_text="0").count() >= 1


def test_other_list_types_have_no_audit_ui(page, ui_server, api):
    """A quotation list shows its own layout and NONE of the audit columns."""
    q = api.post("/lists", json={"list_type": "quotation", "status": "draft"})
    qid = q.json().get("id") or q.json().get("entity_id")
    page.goto(f"{ui_server}/lists/{qid}", wait_until="domcontentloaded")
    page.wait_for_selector(".doc-detail", timeout=8000)
    assert page.locator(".col-counted").count() == 0, "quotation must not show the audit Counted column"
    assert page.locator(".col-onhand").count() == 0, "quotation must not show the On hand column"


def test_new_audit_flow_and_old_urls_gone(page, ui_server, api):
    """`/lists/new-audit` renders the location picker; the removed `/audits` route 404s."""
    tag = uuid.uuid4().hex[:6]
    loc = api.post("/companies/me/locations", json={"name": f"New-{tag}", "type": "warehouse"})
    api.post("/items", json={"status": "available", "sku": f"NEW-{tag}", "name": "Thing", "sell_by": "piece",
             "quantity": 3, "barcode": str(uuid.uuid4().int)[:12], "location_id": str(loc.json()["id"])})
    page.goto(f"{ui_server}/lists/new-audit", wait_until="domcontentloaded")
    assert page.locator("form").count() >= 1  # location picker form renders

    resp = page.request.get(f"{ui_server}/audits")
    assert resp.status == 404, f"/audits should be gone, got {resp.status}"
