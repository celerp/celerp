# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A notice whose message says "Select Try again" carries the Try again control, whether the
server drew it, a toast shows it or a response swaps it in, in every language. A message that
asks the user to do the action again gets none. Clicking the control opens the page with GET
(never a reload that could send a form again), and nothing happens until it is clicked."""

from __future__ import annotations

import pytest

from ui.i18n import t

pytestmark = pytest.mark.browser

_TRY_AGAIN_KEY = "acct.line_not_found"
_RETRY_PROSE_KEY = "shell.request_failed"
_CSV = b"sku,name,quantity\nTA-1,Probe ring,1\n"


def _flash(page, text: str, probe_id: str) -> None:
    page.evaluate(
        """([msg, id]) => { const host = document.createElement('div'); document.body.appendChild(host);
                            const d = document.createElement('div'); d.className = 'flash flash--error';
                            d.id = id; d.textContent = msg;
                            htmx.swap(host, d.outerHTML, {swapStyle: 'innerHTML'}); }""", [text, probe_id])


def _in_language(page, ui_server, lang: str):
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "url": ui_server}])


@pytest.mark.parametrize("lang", ["en", "th", "ja"])
def test_only_a_message_that_says_select_try_again_gets_the_control(page, seeded_user, ui_server, lang):
    _in_language(page, ui_server, lang)
    try:
        page.goto("/dashboard")
        _flash(page, t(_TRY_AGAIN_KEY, lang), "probe")
        _flash(page, t(_RETRY_PROSE_KEY, lang), "prose")
        page.evaluate("msg => celerpToast(msg, 'error', true)", t(_TRY_AGAIN_KEY, lang))
        page.evaluate("msg => celerpToast(msg, 'error', true)", t(_RETRY_PROSE_KEY, lang))
        button = page.locator("#probe a.try-again")
        button.wait_for()
        assert button.inner_text() == t("btn.try_again", lang)
        assert page.locator(".toast a.try-again").count() == 1
        assert page.locator("#prose a.try-again").count() == 0
    finally:
        page.context.clear_cookies(name="celerp_lang")


def test_try_again_waits_for_the_click_and_then_opens_the_page_with_get(page, seeded_user):
    page.goto("/dashboard?from=probe")
    page.evaluate("window.__stillHere = true")
    _flash(page, t(_TRY_AGAIN_KEY, "en"), "probe")
    button = page.locator("#probe a.try-again")
    button.wait_for()
    page.wait_for_timeout(500)
    assert page.evaluate("window.__stillHere === true"), "the page went away on its own"
    with page.expect_request(lambda r: r.resource_type == "document") as sent:
        button.click()
    assert (sent.value.method, sent.value.url.split("/", 3)[3]) == ("GET", "dashboard?from=probe")


def test_try_again_on_a_page_a_form_posted_opens_the_start_of_that_form_with_get(page, seeded_user):
    page.goto("/inventory/import")
    page.locator("input[type='file']").first.set_input_files(
        {"name": "stock.csv", "mimeType": "text/csv", "buffer": _CSV})
    with page.expect_navigation():
        page.locator("form[action='/inventory/import/preview'] button[type='submit']").first.click()
    assert page.url.endswith("/inventory/import/preview")
    page.evaluate("msg => celerpToast(msg, 'error', true)", t(_TRY_AGAIN_KEY, "en"))
    button = page.locator(".toast a.try-again")
    button.wait_for()
    documents = []
    page.on("request", lambda r: r.resource_type == "document" and documents.append((r.method, r.url)))
    with page.expect_navigation():
        button.click()
    assert [(m, u.split("/", 3)[3]) for m, u in documents] == [("GET", "inventory/import")]
