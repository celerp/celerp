# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshot: a production run needing reconciliation is reached from its
product's Manufacturing tab, its value is entered on the page (Escape leaves the field), and
sending it lets the run carry on without the page reloading."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from .test_inventory_merge_after_disconnect_browser import _db

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/manufacturing")


def _mark_unresolved(company_id: str, run_id: str) -> None:
    state = _db("SELECT state FROM projections WHERE company_id = %s AND entity_id = %s", company_id, run_id)[0][0]
    _db("UPDATE projections SET state = CAST(%s AS json) WHERE company_id = %s AND entity_id = %s",
        json.dumps({**state, "wip_unresolved": "books disagree"}), company_id, run_id)


def _run_needing_reconciliation(api) -> tuple[str, str, str]:
    """A product, its issued run and the company, the run in the state an older release leaves
    it in when its history cannot prove the value of its materials (setup only: how a run
    arrives there is covered by test_mfg_wip_upgrade)."""
    from celerp.services.auth import decode_access_token

    tag = uuid.uuid4().hex[:6].upper()
    part = api.post("/items", json={"status": "available", "sku": f"REC-{tag}-PART", "name": "Part", "quantity": 10,
                                    "sell_by": "piece", "inventory_type": "component"}).json()["id"]
    product = api.post("/items", json={"status": "available", "sku": f"REC-{tag}", "name": "Assembly", "quantity": 0,
                                       "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{product}/recipe",
            json={"output_qty": 1, "components": [{"item_id": part, "quantity": 2}], "labor": [], "overhead": []})
    run = api.post(f"/manufacturing/items/{product}/build", json={"quantity": 1, "idempotency_key": tag}).json()
    run_id = run.get("run_id") or run["id"]
    assert api.post(f"/manufacturing/{run_id}/issue", json={"idempotency_key": tag}).status_code == 200
    company_id = decode_access_token(api.headers["Authorization"].split(" ", 1)[1])["company_id"]
    _mark_unresolved(company_id, run_id)
    return product, run_id, company_id


def test_a_run_needing_reconciliation_is_reconciled_on_its_page(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    product, run_id, _ = _run_needing_reconciliation(api)

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/inventory/{product}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#production-block a:has-text('Needs reconciling')", timeout=10000)
    page.click("#production-block a:has-text('Needs reconciling')")
    page.wait_for_selector("#reconcile-panel form", timeout=10000)
    assert "the books hold a different amount than its history shows" in page.inner_text("#reconcile-panel")
    page.evaluate("window.__stay = 1")  # gone if sending reloads the page

    value = page.locator("#reconcile-panel input[name='value']")
    value.fill("0")
    value.press("Escape")
    assert page.evaluate("document.activeElement.name") != "value", "Escape did not leave the field"
    page.screenshot(path=str(SHOTS / "reconcile-run.png"), full_page=True)

    page.click("#reconcile-panel button[type='submit']")
    page.wait_for_selector("#reconcile-panel .flash--success", timeout=10000)
    assert "The run can issue, return, receive and complete again." in page.inner_text("#reconcile-panel")
    assert page.evaluate("window.__stay") == 1
    assert not api.get(f"/manufacturing/{run_id}").json().get("wip_unresolved")


def test_a_key_spent_on_another_action_is_replaced_so_the_user_can_send_again(page, ui_server, api):
    _, run_id, company_id = _run_needing_reconciliation(api)
    key = "#reconcile-panel input[name='idempotency_key']"
    page.goto(f"{ui_server}/manufacturing/runs/{run_id}/reconcile", wait_until="domcontentloaded")
    page.wait_for_selector("#reconcile-panel form", timeout=10000)
    spent = page.input_value(key)
    page.fill("#reconcile-panel input[name='value']", "0")
    page.click("#reconcile-panel button[type='submit']")
    page.wait_for_selector("#reconcile-panel .flash--success", timeout=10000)

    # The run needs reconciling again, and the form offered carries the key already spent.
    _mark_unresolved(company_id, run_id)
    page.goto(f"{ui_server}/manufacturing/runs/{run_id}/reconcile", wait_until="domcontentloaded")
    page.wait_for_selector("#reconcile-panel form", timeout=10000)
    page.evaluate(f"document.querySelector(\"{key}\").value = {json.dumps(spent)}")
    page.fill("#reconcile-panel input[name='value']", "1")
    page.click("#reconcile-panel button[type='submit']")
    page.wait_for_selector("#reconcile-panel .flash--error", timeout=10000)
    assert "already sent with different details" in page.inner_text("#reconcile-panel")
    fresh = page.input_value(key)
    assert fresh and fresh != spent

    page.fill("#reconcile-panel input[name='value']", "0")
    page.click("#reconcile-panel button[type='submit']")
    page.wait_for_selector("#reconcile-panel .flash--success", timeout=10000)
    assert not api.get(f"/manufacturing/{run_id}").json().get("wip_unresolved")
