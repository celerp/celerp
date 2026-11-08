# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser E2E tests for subscriptions UI.

Covers:
  - SUB-E2E-01: List page renders (Sales tab)
  - SUB-E2E-02: List page renders (Purchasing tab)
  - SUB-E2E-03: New form loads without crash
  - SUB-E2E-04: Create subscription via form -> redirects to detail page
  - SUB-E2E-05: Detail page shows correct fields (name, contact, frequency)
  - SUB-E2E-06: Generate Now button present on active subscription
"""
from __future__ import annotations

import pathlib
from datetime import date

import pytest

pytestmark = pytest.mark.browser


def _save(page, name: str) -> None:
    # Debug screenshots disabled — the hardcoded /mnt/storage path is not portable.
    # _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # page.screenshot(path=str(_SCREENSHOT_DIR / f"{name}.png"), full_page=True)
    pass


def _no_crash(page, ctx: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: Internal Server Error in body"
    assert "Traceback" not in body, f"{ctx}: Traceback in body"
    assert "/login" not in page.url, f"{ctx}: redirected to login"


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_sub_list_sales(page, ui_server):
    """SUB-E2E-01: Sales subscriptions list renders without crash."""
    page.goto(f"{ui_server}/subscriptions?direction=sales")
    page.wait_for_load_state("load")
    _save(page, "01-list-sales")
    _no_crash(page, "list-sales")
    assert "Subscription" in page.title() or "Subscription" in page.locator("h1,h2").first.inner_text()


def test_sub_list_purchasing(page, ui_server):
    """SUB-E2E-02: Purchasing subscriptions list renders without crash."""
    page.goto(f"{ui_server}/subscriptions?direction=purchasing")
    page.wait_for_load_state("load")
    _save(page, "02-list-purchasing")
    _no_crash(page, "list-purchasing")


def test_sub_new_form_loads(page, ui_server):
    """SUB-E2E-03: 'New subscription' creates a draft and opens its detail page.

    There is no standalone GET form at /subscriptions/new — the New action does an
    htmx POST that HX-Redirects to the new draft's detail page (edited inline there).
    """
    page.goto(f"{ui_server}/subscriptions?direction=sales", wait_until="domcontentloaded")
    page.wait_for_load_state("load")
    _no_crash(page, "subscriptions-list")
    new_btn = page.locator("[hx-post*='/subscriptions/new']").first
    assert new_btn.count() > 0, "New subscription button missing on the list"
    new_btn.click()
    # htmx follows the HX-Redirect to /subscriptions/{id}
    page.wait_for_url(lambda url: "/subscriptions/" in url, timeout=10000)
    _no_crash(page, "new-subscription-detail")
    assert "/new" not in page.url, f"Should land on the draft detail page, got {page.url}"


def test_sub_create_and_detail(page, ui_server, api):
    """SUB-E2E-04: Create a subscription → its detail page renders the editable
    frequency / start-date controls (inline-edit, not a submit form)."""
    page.goto(f"{ui_server}/subscriptions?direction=sales", wait_until="domcontentloaded")
    page.wait_for_load_state("load")
    new_btn = page.locator("[hx-post*='/subscriptions/new']").first
    assert new_btn.count() > 0, "New subscription button missing on the list"
    new_btn.click()
    page.wait_for_url(lambda url: "/subscriptions/" in url, timeout=10000)
    page.wait_for_load_state("load")
    _no_crash(page, "subscription-detail")
    assert "/new" not in page.url, f"Expected a subscription detail page, got {page.url}"

    # Draft detail must expose the inline-editable subscription controls.
    assert page.locator("select[hx-patch$='/schedule/frequency']").count() > 0, \
        "Frequency control missing on subscription detail"
    assert page.locator("input[hx-patch$='/schedule/anchor_date']").count() > 0, \
        "First-bill-date control missing on subscription detail"


def test_documents_page_shows_the_next_bill_date_its_own_page_shows(page, ui_server, api):
    """An agreement opened from Documents shows the next bill date its periods hold, the one its
    own page shows, not "--"."""
    r = api.post("/docs", json={
        "doc_type": "subscription_invoice", "frequency": "monthly", "start_date": "2027-03-01",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
    })
    assert r.status_code in {200, 201}, r.text
    eid = r.json()["id"]
    assert api.post(f"/subscriptions/{eid}/activate").status_code == 200
    next_bill = api.get(f"/subscriptions/{eid}/periods").json()["next_bill_date"]
    assert next_bill == "2027-03-01"
    for url in (f"{ui_server}/subscriptions/{eid}", f"{ui_server}/docs/{eid}"):
        page.goto(url, wait_until="domcontentloaded")
        _no_crash(page, url)
        assert page.locator("#sub-next-bill").inner_text().split()[-1] == next_bill, url


def test_activate_on_an_empty_draft_names_the_way_out_its_page_offers(page, ui_server):
    """New subscription, then Activate before adding a line: the toast says to change that on this
    page and Activate, not to cancel, and the draft's page offers + Add item."""
    from ui.i18n import t

    page.goto(f"{ui_server}/subscriptions?direction=sales", wait_until="domcontentloaded")
    page.locator("[hx-post*='/subscriptions/new']").first.click()
    page.wait_for_url(lambda url: "/subscriptions/" in url and "/new" not in url, timeout=10000)
    page.wait_for_load_state("load")
    page.get_by_role("button", name=t("btn.activate", "en"), exact=True).click()
    refusal = page.locator(".toast-container .toast", has_text="can't bill")
    refusal.wait_for(timeout=10000)
    toast = refusal.first.inner_text()
    way_out = t("subscriptions.err_bill_refused_draft", "en", ref="", reason="").split("can't bill.")[1].strip()
    assert way_out == "Change that on this page, then Activate it." and way_out in toast, toast
    _no_crash(page, "draft-activate")
    assert page.locator("button", has_text=t("btn._add_item", "en")).count() > 0
    assert "Cancel this subscription" not in toast, toast


def _draft(api, line: dict) -> str:
    r = api.post("/docs", json={
        "doc_type": "subscription_invoice", "frequency": "monthly", "start_date": "2027-03-01",
        "line_items": [{"description": "Service", "quantity": 1, **line}],
    })
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def _activated(page, api, eid: str) -> dict:
    """Wait for the agreement to leave draft and return it as the API holds it."""
    for _ in range(50):
        doc = api.get(f"/docs/{eid}").json()
        if doc["status"] != "draft":
            return doc
        page.wait_for_timeout(200)
    raise AssertionError(f"{eid} is still a draft: {doc['line_items']}")


def test_activate_after_choosing_only_a_line_tax_activates_on_that_tax(page, ui_server, api):
    """A draft refused for a line with no tax chosen: pick No Tax in the line and click Activate,
    nothing else. Activate saves the choice first and the agreement activates on it."""
    from ui.i18n import t

    eid = _draft(api, {"unit_price": 100.0, "line_total": 100.0, "taxes": None})
    page.goto(f"{ui_server}/subscriptions/{eid}", wait_until="domcontentloaded")
    page.locator("#line-body select[data-name=tax_select]").first.select_option(label=t("doc.no_tax", "en"))
    page.get_by_role("button", name=t("btn.activate", "en"), exact=True).click()
    doc = _activated(page, api, eid)
    assert doc["status"] == "active" and doc["line_items"][0]["taxes"] == [], doc["line_items"]


def test_activate_straight_after_typing_a_price_activates_at_that_price(page, ui_server, api):
    """Type a new unit price and click Activate at once: the agreement activates at the typed price,
    not the stored one."""
    from ui.i18n import t

    eid = _draft(api, {"unit_price": 9800.0, "line_total": 9800.0, "taxes": []})
    page.goto(f"{ui_server}/subscriptions/{eid}", wait_until="domcontentloaded")
    page.locator("#line-body input[data-name=unit_price]").first.fill("12345")
    page.get_by_role("button", name=t("btn.activate", "en"), exact=True).click()
    doc = _activated(page, api, eid)
    assert doc["status"] == "active" and float(doc["line_items"][0]["unit_price"]) == 12345.0, doc["line_items"]


def _scheduled_draft(api, skipped: bool = False) -> str:
    eid = _draft(api, {"unit_price": 9800.0, "line_total": 9800.0, "taxes": []})
    anchor = date.today().replace(day=1).isoformat()
    r = api.patch(f"/subscriptions/{eid}/schedule", json={"anchor_date": anchor, "frequency": "monthly"})
    assert r.status_code == 200, r.text
    if skipped:
        r = api.post(f"/subscriptions/{eid}/periods/0/skip", json={"reason": "holiday"})
        assert r.status_code == 200, r.text
    return eid


def _period_state(page, api, eid: str, skipped: bool) -> None:
    """Wait for period 0 of the agreement to be skipped, or not."""
    for _ in range(50):
        rows = api.get(f"/subscriptions/{eid}/periods").json()["rows"]
        if (next(p for p in rows if p["period_index"] == 0)["state"] == "skipped") == skipped:
            return
        page.wait_for_timeout(200)
    raise AssertionError(f"period 0 of {eid} skipped is not {skipped}: {rows}")


def _typed_price(page, price: str = "12345") -> None:
    page.locator("#line-body input[data-name=unit_price]").first.fill(price)


def test_unskip_straight_after_typing_a_price_keeps_the_price(page, ui_server, api):
    """Type a new unit price on a draft agreement and click Un-skip at once: the period comes back and
    the agreement holds the typed price."""
    from ui.i18n import t

    eid = _scheduled_draft(api, skipped=True)
    page.goto(f"{ui_server}/subscriptions/{eid}", wait_until="domcontentloaded")
    _typed_price(page)
    page.get_by_role("button", name=t("btn.unskip", "en"), exact=True).first.click()
    _period_state(page, api, eid, False)
    assert float(api.get(f"/docs/{eid}").json()["line_items"][0]["unit_price"]) == 12345.0


def test_skip_with_a_reason_straight_after_typing_a_price_keeps_the_price(page, ui_server, api):
    """Type a reason and a new unit price, then click Skip at once: the period is skipped and the
    agreement holds the typed price."""
    from ui.i18n import t

    eid = _scheduled_draft(api)
    page.goto(f"{ui_server}/subscriptions/{eid}", wait_until="domcontentloaded")
    skip = page.locator(f"form[hx-post='/subscriptions/{eid}/periods/0/skip']")
    skip.locator("input[name=reason]").fill("holiday")
    _typed_price(page)
    skip.get_by_role("button", name=t("btn.skip", "en"), exact=True).click()
    _period_state(page, api, eid, True)
    assert float(api.get(f"/docs/{eid}").json()["line_items"][0]["unit_price"]) == 12345.0


def test_mark_as_sent_straight_after_typing_a_price_sends_that_price(page, ui_server, api):
    """Type a new unit price on a draft invoice and click Mark as Sent at once: the invoice is sent at
    the typed price."""
    from ui.i18n import t

    r = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"description": "Service", "quantity": 1, "unit_price": 9800.0, "line_total": 9800.0, "taxes": []}]})
    assert r.status_code in {200, 201}, r.text
    eid = r.json()["id"]
    page.goto(f"{ui_server}/docs/{eid}", wait_until="domcontentloaded")
    _typed_price(page)
    page.get_by_role("button", name=t("btn.mark_as_sent", "en"), exact=True).click()
    doc = _activated(page, api, eid)
    assert float(doc["line_items"][0]["unit_price"]) == 12345.0, doc["line_items"]


def test_a_door_folds_in_the_waiting_line_save_and_a_failed_save_sends_nothing(page, ui_server, api):
    """The save a blur leaves waiting folds into the gate's own save (one save, then the request),
    and a save that fails sends nothing."""
    from ui.i18n import t

    eid = _scheduled_draft(api, skipped=True)
    page.goto(f"{ui_server}/subscriptions/{eid}", wait_until="domcontentloaded")
    sent, saves = [], []
    page.on("request", lambda q: q.method != "GET" and sent.append(q.url))
    page.on("console", lambda m: m.text == "line save" and saves.append(m.text))
    stub = """(saved) => {
        window._celerpPersistOnce = async () => {
            console.log("line save");
            if (saved) window._celerpSavedLineRevision = window._celerpLineRevision;
            return saved;
        };
        celerpAutoSave();
    }"""
    unskip = page.get_by_role("button", name=t("btn.unskip", "en"), exact=True).first
    page.evaluate(stub, False)
    unskip.click()
    page.wait_for_timeout(800)
    assert len(saves) == 1 and sent == [], (saves, sent)
    _period_state(page, api, eid, True)
    page.evaluate(stub, True)
    unskip.click()
    _period_state(page, api, eid, False)
    assert len(saves) == 2 and [u for u in sent if u.endswith("/periods/0/unskip")] == sent[:1], (saves, sent)
