# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Browser test: the statement subject picker selects several accounts at once.

The multi-select behaviour lives in initCombobox(), so nothing below can be
proven by rendering HTML server-side: ticking an option, keeping the list open,
unticking, and the summary label are all client-side state. This is the only
layer that shows the picker actually works.

Proves:
1. The picker is a multi-select combobox and offers contacts and chart accounts together.
2. Clicking an option ticks it and keeps the list open for the next one.
3. Each selection adds its own submitted field, which is what a repeated query key needs.
4. Clicking a ticked option unticks it.
5. The closed input names a single selection and counts several.
6. Submitting renders one section per selected subject.
7. The open list follows the field when content loads above it, and a pick made
   while the list moves keeps it open.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from .conftest import css_class

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def statement_url(ui_server, api):
    """Seed the chart plus a customer, so the picker has both kinds to offer."""
    r = api.post("/accounting/chart/seed")
    assert r.status_code in (200, 409), f"Chart seed failed: {r.text}"

    r = api.post("/crm/contacts", json={"name": "Statement Test Co", "contact_type": "customer"})
    assert r.status_code in (200, 201), f"Create contact failed: {r.text}"

    return f"{ui_server}/reports/statement"


# No part of the page is still loading or settling: a banner that lands later moves the
# field down after the list is placed.
_SETTLED = "!document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling')"


def _open_picker(page, url):
    page.goto(url, wait_until="load")
    page.wait_for_function(_SETTLED)
    page.wait_for_selector(".combobox-wrap[data-multiple]", timeout=10000)
    page.locator(".combobox-wrap[data-multiple] .combobox-input").first.click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)


def _dismiss(page):
    """Blur the picker so it redraws its closed label.

    Blurring directly rather than clicking elsewhere: the option list is
    position:fixed over the middle of the page, so a body click lands on an
    option and ticks it, and a corner click lands on the sidebar and navigates.
    """
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.wait_for_timeout(300)


def _options(page):
    return page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)")


def _selected_inputs(page):
    return page.locator(".combobox-selected input[name='account']")


def test_picker_is_multi_select(page, statement_url):
    page.goto(statement_url, wait_until="domcontentloaded")
    wrap = page.locator(".combobox-wrap[data-multiple]")
    assert wrap.count() == 1, "the statement picker should be the one multi-select combobox"


def test_picker_offers_contacts_and_chart_accounts(page, statement_url):
    """One list covers both kinds, which is the whole point of the change."""
    _open_picker(page, statement_url)
    values = [_options(page).nth(i).get_attribute("data-value")
              for i in range(_options(page).count())]
    assert any(v.startswith("c:") for v in values), "no contacts offered"
    assert any(v.startswith("a:") for v in values), "no chart accounts offered"


def test_clicking_an_option_ticks_it_and_keeps_the_list_open(page, statement_url):
    """Picking several in a row is the mode's purpose, so the list must not close."""
    _open_picker(page, statement_url)
    first = _options(page).first
    first.click()
    expect(first).to_have_class(css_class("combobox-option--selected"))
    assert page.locator(".combobox-list.open").is_visible(), "list closed after one pick"


def test_each_selection_adds_its_own_submitted_field(page, statement_url):
    """A repeated query key is what the server reads; one input cannot carry two."""
    _open_picker(page, statement_url)
    assert _selected_inputs(page).count() == 0
    _options(page).nth(0).click()
    assert _selected_inputs(page).count() == 1
    _options(page).nth(1).click()
    assert _selected_inputs(page).count() == 2
    values = {_selected_inputs(page).nth(i).get_attribute("value") for i in range(2)}
    assert len(values) == 2, "two picks must submit two distinct values"


def test_clicking_a_ticked_option_unticks_it(page, statement_url):
    _open_picker(page, statement_url)
    opt = _options(page).first
    opt.click()
    assert _selected_inputs(page).count() == 1
    opt.click()
    assert _selected_inputs(page).count() == 0
    expect(opt).not_to_have_class(css_class("combobox-option--selected"))


_LIST_OFFSET = """(() => {
    const w = document.querySelector('.combobox-wrap[data-multiple]');
    return w.querySelector('.combobox-list').getBoundingClientRect().top
        - w.querySelector('.combobox-input').getBoundingClientRect().bottom;
})()"""


def test_the_open_list_follows_the_field_when_content_loads_above_it(page, statement_url):
    """The list is position:fixed and placed when it opens. A notice that loads above the
    form afterwards moves the field down, and the list has to move with it."""
    _open_picker(page, statement_url)
    offset = page.evaluate(_LIST_OFFSET)
    page.evaluate(
        "htmx.swap('#main-content', '<div style=\"height:80px\"></div>', {swapStyle: 'afterbegin'})")
    page.wait_for_function(f"Math.abs({_LIST_OFFSET} - {offset}) < 1", timeout=3000)


def test_the_open_list_follows_the_field_when_a_notice_shows_above_it(page, statement_url):
    """The system health notice is shown by script once its check answers, which can be
    after the list opened. Nothing is swapped in, but the field still moves down. The check
    is held until the page has settled and the list is open, then answers that disk space is
    low, and the list must follow the field once the notice script has shown the notice."""
    held = []
    page.route("**/health/system", lambda route: held.append(route))
    with page.expect_request(lambda r: r.url.endswith("/health/system")):
        page.goto(statement_url, wait_until="load")
    page.wait_for_function(_SETTLED)
    page.locator(".combobox-wrap[data-multiple] .combobox-input").first.click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    offset = page.evaluate(_LIST_OFFSET)
    assert len(held) == 1, held
    held[0].fulfill(json={"overall": "warning", "disk": {"status": "warning", "message": "Disk space is low."}})
    page.wait_for_function("document.getElementById('sys-health-banner').style.display === 'flex'", timeout=3000)
    page.wait_for_function(f"Math.abs({_LIST_OFFSET} - {offset}) < 1", timeout=3000)


def test_a_pick_keeps_the_list_open_when_the_list_moves_under_the_pointer(page, statement_url):
    """Content that lands above the form while the list is open moves the field at once,
    before the list follows. A pick then places the list under its field mid-click, so the
    button comes up over the page around it. The pick still counts, and the list stays
    open for the next one."""
    _open_picker(page, statement_url)
    page.evaluate("document.querySelector('#main-content')"
                  ".insertAdjacentHTML('afterbegin', '<div style=\"height:80px\"></div>')")
    opt = _options(page).first
    opt.click()
    assert _selected_inputs(page).count() == 1
    assert page.locator(".combobox-list.open").is_visible(), "the pick closed the list"
    opt.click()
    assert _selected_inputs(page).count() == 0
    assert page.locator(".combobox-list.open").is_visible(), "unticking closed the list"


def test_closed_input_names_one_selection_and_counts_several(page, statement_url):
    _open_picker(page, statement_url)
    label = _options(page).first.inner_text().strip()
    _options(page).first.click()
    _dismiss(page)
    shown = page.locator(".combobox-wrap[data-multiple] .combobox-input").input_value()
    assert shown == label, f"one selection should read as its own name, got {shown!r}"

    _open_picker(page, statement_url)
    _options(page).nth(0).click()
    _options(page).nth(1).click()
    _dismiss(page)
    shown = page.locator(".combobox-wrap[data-multiple] .combobox-input").input_value()
    assert "2" in shown, f"several selections should show a count, got {shown!r}"


def test_submitting_renders_one_section_per_subject(page, statement_url):
    """End to end: tick two, apply, and the page reports both."""
    _open_picker(page, statement_url)
    _options(page).nth(0).click()
    _options(page).nth(1).click()
    page.locator("form:has(.combobox-wrap[data-multiple]) button[type=submit]").click()
    page.wait_for_load_state("domcontentloaded")

    assert page.url.count("account=") == 2, f"both subjects should be in the URL: {page.url}"
    sections = page.locator(".report-section .report-total")
    assert sections.count() == 2, (
        f"expected a closing balance per subject, found {sections.count()}")


def _multi_input(page):
    return page.locator(".combobox-wrap[data-multiple] .combobox-input").first


def _wait_for_swap(page, before):
    page.wait_for_function(
        "prev => document.querySelector('.combobox-wrap[data-multiple] .combobox-list')"
        ".innerHTML !== prev",
        arg=before, timeout=8000)


def _option_by_value(page, value):
    return page.locator(
        f'.combobox-wrap[data-multiple] .combobox-list .combobox-option[data-value="{value}"]')


def test_a_search_keeps_the_tick_on_an_already_picked_subject(page, statement_url):
    """The server searches the full contact list, and its results know nothing about
    what was ticked since the page loaded. The tick is a view of the selection bag,
    so a swapped-in list has to be re-marked from the bag or the picker lies about
    what is selected."""
    _open_picker(page, statement_url)
    target = _options(page).filter(has_text="Statement Test Co").first
    value = target.get_attribute("data-value")
    target.click()
    assert _selected_inputs(page).count() == 1

    before = page.locator(".combobox-wrap[data-multiple] .combobox-list").inner_html()
    _multi_input(page).fill("Statement Test")
    _wait_for_swap(page, before)

    swapped = _option_by_value(page, value)
    assert swapped.count() == 1, "the searched-for subject should be in the results"
    expect(swapped.first, "the search results dropped the tick on an already picked subject").to_have_class(
        css_class("combobox-option--selected"))
    assert _selected_inputs(page).count() == 1, "the search changed the submitted set"


def test_clearing_the_search_keeps_the_tick(page, statement_url):
    """Clearing the box puts the original options back. Those are the markup the page
    loaded with, so they carry no tick for anything picked since."""
    _open_picker(page, statement_url)
    target = _options(page).filter(has_text="Statement Test Co").first
    value = target.get_attribute("data-value")
    target.click()

    before = page.locator(".combobox-wrap[data-multiple] .combobox-list").inner_html()
    _multi_input(page).fill("Statement Test")
    _wait_for_swap(page, before)
    _multi_input(page).fill("")
    page.wait_for_timeout(200)

    restored = _option_by_value(page, value)
    assert restored.count() == 1, "clearing the box should bring the full list back"
    expect(restored.first, "the restored list dropped the tick on an already picked subject").to_have_class(
        css_class("combobox-option--selected"))
    assert _selected_inputs(page).count() == 1
