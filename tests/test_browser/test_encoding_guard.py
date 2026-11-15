# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser proofs: stored, request and reply text is shown as text on every page that writes it.

Each test puts a payload that sets ``window.__pwned`` when it runs into a value a user, a link
or the server controls (a tax name, an item unit, a notification, a refusal message, a field
label, a list filter), opens the page that writes it on a demo company, and checks the payload
reads as text while the flag stays unset.
"""
from __future__ import annotations

import re
import uuid
from urllib.parse import urlencode

import pytest

from ui.components.encode import safe_url

from test_encode import URL_VECTORS
from .conftest import _test_db, answer_in_app, set_session_cookies
from .test_from_items_tax_e2e import _demo_company

pytestmark = pytest.mark.browser

_IMG = "<img src=x onerror=window.__pwned=1>"
_BREAK = "</script><img src=x onerror=window.__pwned=1>"
_QUOTE = "x';window.__pwned=1;'"


def _pwned(tab) -> bool:
    return bool(tab.evaluate("window.__pwned === 1"))


def _tab(page, ui_server, token):
    ctx = page.context.browser.new_context(base_url=ui_server)
    set_session_cookies(ctx, token)
    ctx.route("**/health/system", lambda route: route.fulfill(json={"overall": "ok"}))
    return ctx, ctx.new_page()


def _taxed_company(api_server, ui_server, api, name):
    return _demo_company(api_server, ui_server, api, "GB", [
        {"name": name, "rate": 7.0, "tax_type": "both", "is_default": True}])


def _split_item(company) -> str:
    """An item whose stored unit is hostile: the unit API refuses such a name now, but a record
    stored before that rule (or by an import) still carries one, so it is written to the projection."""
    r = company.post("/items", json={"name": f"Split {uuid.uuid4().hex[:6]}", "sku": f"SPL-{uuid.uuid4().hex[:6]}",
                                     "sell_by": "piece", "quantity": 20})
    assert r.status_code in (200, 201), r.text
    item_id = r.json()["id"]
    assert company.post("/items/bulk/make-available", json={"entity_ids": [item_id]}).status_code == 200
    conn = _test_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE projections SET state = jsonb_set(state::jsonb, '{sell_by}', to_jsonb(%s::text))::json "
                        "WHERE company_id = %s AND entity_id = %s",
                        (_BREAK, company.get("/companies/me").json()["id"], item_id))
            assert cur.rowcount == 1
    finally:
        conn.close()
    return item_id


def test_tax_name_shown_as_text_on_document_totals(page, api_server, ui_server, api):
    with _taxed_company(api_server, ui_server, api, _IMG) as (company, ui, token, items):
        r = ui.post("/docs/from-items/new", data={"selected": [items[0]["id"]]})
        assert r.status_code == 204, r.text
        doc_id = r.headers["HX-Redirect"].rsplit("/", 1)[-1]
        ctx, tab = _tab(page, ui_server, token)
        try:
            tab.goto(f"/docs/{doc_id}", wait_until="domcontentloaded")
            cell = tab.locator('table.doc-lines tbody tr input[data-name="unit_price"]').first
            cell.fill("100")
            cell.dispatch_event("input")
            tab.wait_for_timeout(500)
            assert _IMG in tab.locator("#doc-tax-rows").inner_text()
            assert not _pwned(tab)
        finally:
            ctx.close()


def test_sell_by_unit_shown_as_text_in_batch_split(page, api_server, ui_server, api):
    with _demo_company(api_server, ui_server, api, "GB") as (company, ui, token, items):
        item_id = _split_item(company)
        ctx, tab = _tab(page, ui_server, token)
        try:
            tab.goto(f"/inventory/{item_id}", wait_until="domcontentloaded")
            tab.locator("select.split-mode").first.select_option("batch")
            form = tab.locator("form:has([name=batch_count])").first
            form.locator("[name=batch_qty]").fill("2", force=True)
            form.locator("[name=batch_count]").fill("5", force=True)
            form.locator("[name=batch_count]").dispatch_event("input")
            preview = tab.locator(".batch-split-preview").first
            assert _BREAK.capitalize() in preview.inner_text()
            assert not _pwned(tab)
        finally:
            ctx.close()


def test_notification_link_drops_script_scheme(page):
    items = [
        {"id": "n1", "title": _IMG, "body": "<b>bold</b>", "action_url": "javascript:window.__pwned=1",
         "read": False, "priority": "high"},
        {"id": "n2", "title": "Data link", "body": "b", "action_url": "data:text/html,<script>x</script>", "read": False},
        {"id": "n3", "title": "Off site", "body": "b", "action_url": "//evil.example.com/x", "read": False},
        {"id": "n4", "title": "In app", "body": "b", "action_url": "/docs/doc:1", "read": False},
    ]
    page.route(re.compile(r".*/notifications\?unread_only=true.*"),
               lambda route: route.fulfill(json={"items": items, "unread_count": 4}))
    page.goto("/dashboard", wait_until="domcontentloaded")
    page.evaluate("window._loadNotifs()")
    notif = page.locator("#notif-list")
    notif.locator(".notif-item").nth(3).wait_for(state="attached")
    assert _IMG in notif.inner_text() and "<b>bold</b>" in notif.inner_text()
    hrefs = notif.locator("a").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert hrefs == ["/docs/doc:1"], hrefs
    notif.locator(".notif-item", has_text="Data link").wait_for(state="attached")
    notif.locator(".notif-item", has_text=_IMG).locator(".notif-item__link").first.dispatch_event("click")
    assert not _pwned(page)


def _contacts_page(page, api):
    ids = [api.post("/crm/contacts", json={"name": f"Guard {n} {uuid.uuid4().hex[:6]}",
                                           "contact_type": "customer"}).json()["id"] for n in (1, 2)]
    page.goto("/contacts/customers", wait_until="domcontentloaded")
    page.evaluate("ids => ids.forEach((id, i) => CelerpSelection.add(id, {name: 'Guard ' + i}))", ids)
    return ids


def test_contact_replies_written_as_text(page, api):
    _contacts_page(page, api)
    page.route("**/crm/contacts/bulk/delete", lambda route: route.fulfill(json={"detail": _IMG}))
    page.evaluate("contactBulkChanged('delete')")
    answer_in_app(page)
    result = page.locator("#bulk-action-result")
    result.get_by_text(_IMG).wait_for(state="attached")
    assert not _pwned(page)

    page.route("**/crm/contacts/merge", lambda route: route.fulfill(json={"detail": _IMG + " merge"}))
    page.evaluate("contactBulkChanged('contact-merge')")
    page.locator("input[name=contact-merge-winner]").first.evaluate("e => { e.checked = true; }")
    page.evaluate("contactMergeSubmit()")
    result.get_by_text(_IMG + " merge").wait_for(state="attached")
    assert not _pwned(page)

    page.unroute("**/crm/contacts/merge")
    page.route("**/crm/contacts/merge", lambda route: route.fulfill(json={"merged_into": "../../settings"}))
    with page.expect_navigation():
        page.evaluate("contactMergeSubmit()")
    assert re.search(r"/contacts/[^/]+$", page.url.split("?")[0]), page.url


def test_column_labels_encoded_in_column_manager(page, api_server, ui_server, api):
    with _demo_company(api_server, ui_server, api, "GB") as (company, ui, token, items):
        fields = company.get("/companies/me/item-schema").json()
        fields.append({"key": "guard_note", "label": _BREAK, "type": "text", "editable": True,
                       "show_in_table": True, "position": 99})
        r = company.patch("/companies/me/item-schema", json={"fields": fields})
        assert r.status_code == 200, r.text
        ctx, tab = _tab(page, ui_server, token)
        try:
            tab.goto("/inventory", wait_until="domcontentloaded")
            tab.wait_for_load_state("load")
            assert not _pwned(tab)
            assert tab.locator(".column-option", has_text=_BREAK).count() >= 1
        finally:
            ctx.close()


@pytest.mark.parametrize("param", ["category", "q", "status", "sort"])
def test_inventory_list_links_cannot_run_script(page, param):
    for payload in (_QUOTE, _BREAK):
        page.goto("/inventory?" + urlencode({param: payload}), wait_until="load")
        assert page.locator("#inventory-content, .data-table, .empty-state").count() >= 1
        assert not _pwned(page), (param, payload)


def test_stored_text_encoded_on_record_pages(page, api_server, ui_server, api):
    with _taxed_company(api_server, ui_server, api, _IMG) as (company, ui, token, items):
        contact = company.post("/crm/contacts", json={
            "name": _IMG, "contact_type": "customer", "notes": _BREAK, "website": "http://[x"}).json()["id"]
        item_id = _split_item(company)
        doc = company.post("/docs", json={
            "doc_type": "invoice", "status": "draft", "contact_id": contact, "notes": _BREAK,
            "line_items": [{"item_id": items[0]["id"], "sku": items[0]["sku"], "name": _IMG,
                            "description": _BREAK, "quantity": 1, "unit_price": 50.0,
                            "taxes": [{"code": _IMG, "rate": 7.0, "order": 0, "is_compound": False}]}]})
        assert doc.status_code in (200, 201), doc.text
        sub = company.post("/docs", json={
            "doc_type": "subscription_invoice", "frequency": "monthly", "start_date": "2026-01-01",
            "contact_id": contact, "notes": _BREAK,
            "line_items": [{"description": _IMG, "quantity": 1, "unit_price": 100.0, "line_total": 100.0}]})
        assert sub.status_code in (200, 201), sub.text
        ctx, tab = _tab(page, ui_server, token)
        try:
            for path, shown in ((f"/docs/{doc.json()['id']}", _IMG), (f"/subscriptions/{sub.json()['id']}", _IMG),
                                (f"/contacts/{contact}", _IMG), (f"/inventory/{item_id}", _BREAK.capitalize())):
                resp = tab.goto(path, wait_until="load")
                assert resp.status == 200, path
                tab.wait_for_timeout(300)
                assert shown in tab.locator("body").inner_text(), path
                assert not _pwned(tab), path
        finally:
            ctx.close()


def test_url_rule_same_in_python_and_browser(page):
    page.goto("/dashboard", wait_until="domcontentloaded")
    vectors = list(URL_VECTORS) + ["http\x01://example.com"]
    browser = page.evaluate("vs => vs.map(v => celerpSafeUrl(v))", vectors)
    assert browser == [safe_url(v) for v in vectors]
    assert page.evaluate("celerpSafeUrl(null)") == safe_url(None) == "#"


def test_celerp_node_writes_text_not_markup(page):
    page.goto("/dashboard", wait_until="domcontentloaded")
    out = page.evaluate("""() => {
        const a = celerpNode('a', {href: 'javascript:window.__pwned=1', class: 'x'}, ['<b>t</b>', celerpNode('i', {}, ['k'])]);
        const ok = celerpNode('a', {href: '/docs/1'}, ['d']);
        let refused = false;
        try { celerpNode('div', {onclick: 'window.__pwned=1'}, []); } catch (e) { refused = true; }
        let srcdoc = false;
        try { celerpNode('iframe', {srcdoc: '<b>x</b>'}, []); } catch (e) { srcdoc = true; }
        return {text: a.textContent, kids: a.children.length, href: a.getAttribute('href'),
                cls: a.className, ok: ok.getAttribute('href'), refused, srcdoc};
    }""")
    assert out == {"text": "<b>t</b>k", "kids": 1, "href": None, "cls": "x", "ok": "/docs/1",
                   "refused": True, "srcdoc": True}
    assert not _pwned(page)
