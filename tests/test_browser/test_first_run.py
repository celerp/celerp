# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""First run on a fresh install: one setup form, then the dashboard.

The suite's shared servers already carry a seeded owner, so these tests run on a new
install (fresh_install), emptied again before each test: every test starts exactly
where a new user starts.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import Page, expect

from .fresh_install import API_PORT, REPO, install_env

pytestmark = pytest.mark.browser

_PASSWORD = "FirstRun123!"


def _context(playwright, *, width: int = 1280, locale: str = "en-US", timezone_id: str = "Asia/Bangkok",
             **kw):
    browser = playwright.chromium.launch()
    ctx = browser.new_context(viewport={"width": width, "height": 900}, locale=locale,
                              timezone_id=timezone_id, **kw)
    return browser, ctx


def _no_sideways_scroll(page: Page, where: str) -> None:
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"{where}: page scrolls sideways by {overflow}px"


def _choose(page: Page, name: str, value: str) -> None:
    wrap = page.locator(f'.combobox-wrap:has(input[type="hidden"][name="{name}"])')
    wrap.locator(".combobox-input").click()
    wrap.locator(f'.combobox-option[data-value="{value}"]').click()
    expect(wrap.locator(f'input[type="hidden"][name="{name}"]')).to_have_value(value)


def _fill_text_fields(page: Page, email: str) -> None:
    page.fill("#company_name", "First Run Trading")
    page.fill("#name", "Owner Person")
    page.fill("#email", email)
    page.fill("#password", _PASSWORD)
    page.fill("#confirm_password", _PASSWORD)


def _hidden(page: Page, name: str) -> str:
    return page.locator(f'#setup-form input[type="hidden"][name="{name}"]').input_value()


def test_a_new_install_inherits_nothing_the_other_tests_set(monkeypatch):
    """The shared browser servers turn every module on through ENABLED_MODULES; a new install
    started after them must still load only what a new install loads."""
    for name, value in (("ENABLED_MODULES", "celerp-docs"), ("PYTHONPATH", "/elsewhere"),
                        ("DATA_DIR", "/shared"), ("MODULE_DIR", "/shared/modules")):
        monkeypatch.setenv(name, value)

    env = install_env()

    assert not {"ENABLED_MODULES", "PYTHONPATH", "DATA_DIR", "MODULE_DIR"} & set(env)
    assert env["PATH"] == os.environ["PATH"]


@pytest.mark.parametrize("width", [390, 1280])
def test_first_run_one_form_to_dashboard(playwright, fresh, width):
    """INV-1, INV-12: one form, one submit, then the dashboard; nothing scrolls sideways."""
    browser, ctx = _context(playwright, width=width)
    try:
        page = ctx.new_page()
        posts: list[str] = []
        page.on("request", lambda r: posts.append(r.url)
                if r.method == "POST" and r.is_navigation_request() else None)
        page.goto(f"{fresh.ui}/", wait_until="networkidle")
        assert urlsplit(page.url).path == "/setup", page.url
        assert page.locator("form").count() == 1, "the first screen must hold exactly one form"
        _no_sideways_scroll(page, "/setup")
        _fill_text_fields(page, f"owner{width}@first-run.test")
        _choose(page, "vertical", "blank")
        _choose(page, "currency", "THB")
        page.locator('#setup-form button[type="submit"]').scroll_into_view_if_needed()
        expect(page.locator('#setup-form button[type="submit"]')).to_be_visible()
        page.locator('#setup-form button[type="submit"]').click()
        page.wait_for_url(lambda u: urlsplit(u).path in ("/dashboard", "/setup/activating"), timeout=30000)
        if urlsplit(page.url).path == "/setup/activating":
            _no_sideways_scroll(page, "/setup/activating")
            page.wait_for_url(f"{fresh.ui}/dashboard", timeout=120000)
        page.wait_for_load_state("load")
        _no_sideways_scroll(page, "/dashboard")
        assert posts == [f"{fresh.ui}/setup"], f"expected exactly one form submit, got {posts}"
        assert page.locator("form#setup-form").count() == 0
    finally:
        browser.close()


@pytest.mark.parametrize("locale,timezone_id,expected", [
    ("en-US", "Asia/Bangkok", "THB"),
    ("de-DE", "Europe/Berlin", "EUR"),
    ("en", "UTC", ""),
    # Chrome reports this zone by its old name, Asia/Calcutta.
    ("en-US", "Asia/Kolkata", "INR"),
    ("am-ET", "Africa/Addis_Ababa", "ETB"),
])
def test_currency_guess_uses_timezone_country_first(playwright, fresh, locale, timezone_id, expected):
    """INV-14: the timezone's country decides before the language region; unknown is no guess."""
    browser, ctx = _context(playwright, locale=locale, timezone_id=timezone_id)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        assert _hidden(page, "currency") == expected
        shown = page.locator('.combobox-wrap:has(input[name="currency"]) .combobox-input').input_value()
        assert (expected in shown) if expected else shown == "", shown
    finally:
        browser.close()


def test_timezone_hidden_field_filled_from_browser(playwright, fresh):
    """A1: the browser's timezone is stored with the company, by its current name even
    though Chrome reports this one by its old name, America/Buenos_Aires."""
    browser, ctx = _context(playwright, timezone_id="America/Argentina/Buenos_Aires")
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        assert _hidden(page, "timezone") == "America/Argentina/Buenos_Aires"
        _fill_text_fields(page, "tz@first-run.test")
        _choose(page, "vertical", "blank")
        # The timezone's country suggests the peso; the owner keeps the books in dollars instead.
        assert _hidden(page, "currency") == "ARS"
        _choose(page, "currency", "USD")
        page.locator('#setup-form button[type="submit"]').click()
        page.wait_for_url(lambda u: urlsplit(u).path in ("/dashboard", "/setup/activating"), timeout=30000)
        company = fresh.company(ctx)
        settings = company.get("settings") or {}
        assert settings.get("timezone") == "America/Argentina/Buenos_Aires"
        assert settings.get("currency") == "USD"
    finally:
        browser.close()


def test_typed_values_survive_leaving_for_an_option(playwright, fresh):
    """INV-3: leaving for Restore and coming back keeps everything but the passwords;
    a successful submit clears what was kept."""
    browser, ctx = _context(playwright)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        _fill_text_fields(page, "kept@first-run.test")
        _choose(page, "vertical", "blank")
        _choose(page, "currency", "EUR")
        page.locator(".start-option a").first.click()
        page.wait_for_load_state("networkidle")
        assert urlsplit(page.url).path != "/setup"
        page.locator('a[href="/setup"]').first.click()
        page.wait_for_url(f"{fresh.ui}/setup")
        page.wait_for_load_state("networkidle")
        assert page.input_value("#company_name") == "First Run Trading"
        assert page.input_value("#name") == "Owner Person"
        assert page.input_value("#email") == "kept@first-run.test"
        assert _hidden(page, "vertical") == "blank"
        assert _hidden(page, "currency") == "EUR"
        assert page.input_value("#password") == ""
        assert page.input_value("#confirm_password") == ""
        page.fill("#password", _PASSWORD)
        page.fill("#confirm_password", _PASSWORD)
        page.locator('#setup-form button[type="submit"]').click()
        page.wait_for_url(lambda u: urlsplit(u).path in ("/dashboard", "/setup/activating"), timeout=30000)
        kept = page.evaluate("() => sessionStorage.getItem('celerp-setup-form')")
        assert kept is None, kept
    finally:
        browser.close()


def test_create_button_disables_on_submit(playwright, fresh):
    """INV-4: Create cannot be pressed twice."""
    browser, ctx = _context(playwright)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        # Runs after the form's own submit handler; the record outlives the navigation.
        page.evaluate("""() => document.getElementById('setup-form').addEventListener('submit', () =>
            sessionStorage.setItem('probe-disabled',
                String(document.querySelector('#setup-form button[type=submit]').disabled)))""")
        _fill_text_fields(page, "twice@first-run.test")
        _choose(page, "vertical", "blank")
        _choose(page, "currency", "EUR")
        page.locator('#setup-form button[type="submit"]').click()
        page.wait_for_url(lambda u: urlsplit(u).path in ("/dashboard", "/setup/activating"), timeout=30000)
        assert page.evaluate("() => sessionStorage.getItem('probe-disabled')") == "true"
    finally:
        browser.close()


def test_activating_page_shows_failure_and_retry_after_bounded_wait(page, ui_server):
    """INV-1: modules that never come up end in an honest failure with a retry, in real
    time and within the bound, never an endless spinner."""
    page.route("**/setup/activating-status*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"phase": "loading", "requested": 1, "loaded": 0,'
             ' "modules": [{"name": "celerp-docs", "label": "Documents", "running": false}]}'))
    page.goto(f"{ui_server}/setup/activating", wait_until="domcontentloaded")
    failed = page.locator("#activating-failed")
    expect(failed).to_be_visible(timeout=45000)
    expect(failed.locator('a[href="/setup/activating"]')).to_be_visible()
    expect(page.locator(".activating-spinner")).to_be_hidden()
    assert urlsplit(page.url).path == "/setup/activating"


@pytest.mark.parametrize("width", [390, 1280])
def test_password_mismatch_shown_at_confirm_field_and_kept(playwright, fresh, width):
    """A mismatch is caught before the form is sent: the message sits under the confirm
    field, focus goes there, and both typed passwords stay. Fixing it clears the mark."""
    from ui.i18n import t
    browser, ctx = _context(playwright, width=width)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        _fill_text_fields(page, "mismatch@celerp.test")
        page.fill("#confirm_password", _PASSWORD + "x")
        posts: list[str] = []
        page.on("request", lambda req: posts.append(req.url) if req.method == "POST" else None)
        page.locator('#setup-form button[type="submit"]').click()
        msg = page.locator("#confirm_password-error")
        expect(msg).to_be_visible()
        expect(msg).to_have_text(t("settings.passwords_do_not_match"))
        expect(page.locator("#confirm_password")).to_have_attribute("aria-invalid", "true")
        expect(page.locator("#confirm_password")).to_be_focused()
        assert page.input_value("#password") == _PASSWORD
        assert page.input_value("#confirm_password") == _PASSWORD + "x"
        assert posts == [], "nothing is sent while the passwords differ"
        assert urlsplit(page.url).path == "/setup"
        _no_sideways_scroll(page, f"mismatch {width}")
        page.fill("#confirm_password", _PASSWORD)
        expect(msg).to_be_hidden()
        expect(page.locator("#confirm_password")).not_to_have_attribute("aria-invalid", "true")
    finally:
        browser.close()


def test_setup_copy_fits_one_line(playwright, fresh):
    """INV-13: at desktop width each hint and note is one line. The option boxes'
    descriptions are left out: the boxes sit side by side, and at half the card's
    width they wrap."""
    browser, ctx = _context(playwright, width=1280)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        lines = page.evaluate("""() => Array.from(document.querySelectorAll(
              '.setup-card .setup-options-note, .setup-card .form-hint'))
          .map(el => {
            const lh = parseFloat(getComputedStyle(el).lineHeight);
            return {text: el.textContent, lines: Math.round(el.getBoundingClientRect().height / lh)};
          })""")
        assert len(lines) >= 4, lines
        wrapped = [l for l in lines if l["lines"] != 1]
        assert not wrapped, wrapped
    finally:
        browser.close()


def test_keyboard_and_focus(playwright, fresh):
    """INV-12: first field focused, Esc closes both dropdowns, Enter submits, and an
    error focuses the field it is about."""
    browser, ctx = _context(playwright, timezone_id="UTC", locale="en")
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        expect(page.locator("#company_name")).to_be_focused()
        for name in ("vertical", "currency"):
            wrap = page.locator(f'.combobox-wrap:has(input[type="hidden"][name="{name}"])')
            wrap.locator(".combobox-input").click()
            expect(wrap.locator(".combobox-list.open")).to_be_visible()
            page.keyboard.press("Escape")
            expect(wrap.locator(".combobox-list.open")).to_have_count(0)
        _fill_text_fields(page, "keys@first-run.test")
        _choose(page, "vertical", "blank")
        # No currency: the server refuses and says which field.
        page.focus("#confirm_password")
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle")
        assert urlsplit(page.url).path == "/setup"
        expect(page.locator(".flash")).to_be_visible()
        expect(page.locator('.combobox-wrap:has(input[type="hidden"][name="currency"]) :focus')).to_have_count(1)
    finally:
        browser.close()


def test_inputs_carry_autocomplete_hints(playwright, fresh):
    """INV-12: browsers can fill the form in."""
    browser, ctx = _context(playwright)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        hints = {f: page.get_attribute(f"#{f}", "autocomplete")
                 for f in ("company_name", "name", "email", "password", "confirm_password")}
        assert hints == {"company_name": "organization", "name": "name", "email": "email",
                         "password": "new-password", "confirm_password": "new-password"}, hints
    finally:
        browser.close()


@pytest.mark.parametrize("width,height", [(390, 700), (1280, 800)])
def test_dropdowns_stay_on_screen(playwright, fresh, width, height):
    """INV-12: an open business type or currency list sits fully on screen, so the last
    option ("Other / general") can be picked without scrolling the page."""
    browser = playwright.chromium.launch()
    ctx = browser.new_context(viewport={"width": width, "height": height})
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        for name in ("vertical", "currency"):
            wrap = page.locator(f'.combobox-wrap:has(input[type="hidden"][name="{name}"])')
            wrap.locator(".combobox-input").scroll_into_view_if_needed()
            wrap.locator(".combobox-input").click()
            box = wrap.locator(".combobox-list.open").bounding_box()
            assert box and box["y"] >= 0 and box["y"] + box["height"] <= height, (name, box)
            page.keyboard.press("Escape")
        _choose(page, "vertical", "blank")
    finally:
        browser.close()


def test_dropdown_stays_open_when_focus_comes_straight_back(playwright, fresh):
    """INV-12: leaving a searchable dropdown and coming straight back leaves it open;
    the close from the earlier blur never shuts the list the user just opened."""
    browser, ctx = _context(playwright)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        wrap = page.locator('.combobox-wrap:has(input[type="hidden"][name="vertical"])')
        field = wrap.locator(".combobox-input")
        field.focus()
        expect(wrap.locator(".combobox-list.open")).to_be_visible()
        # Out and back well inside the blur handler's delay.
        page.evaluate("""() => { document.getElementById('company_name').focus();
          document.querySelector('.combobox-wrap:has(input[name=vertical]) .combobox-input').focus(); }""")
        page.wait_for_timeout(400)
        expect(wrap.locator(".combobox-list.open")).to_be_visible()
        _choose(page, "vertical", "blank")
    finally:
        browser.close()


def test_dropdown_reopens_on_click_after_esc(playwright, fresh):
    """INV-12: Esc closes a searchable dropdown and a click on the field opens it again."""
    browser, ctx = _context(playwright)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        wrap = page.locator('.combobox-wrap:has(input[type="hidden"][name="vertical"])')
        wrap.locator(".combobox-input").click()
        expect(wrap.locator(".combobox-list.open")).to_be_visible()
        page.keyboard.press("Escape")
        expect(wrap.locator(".combobox-list.open")).to_have_count(0)
        wrap.locator(".combobox-input").click()
        expect(wrap.locator(".combobox-list.open")).to_be_visible()
    finally:
        browser.close()


_LOCALE_DIR = REPO / "ui" / "locales"
_SETUP_SCREEN_KEYS = (
    "page.set_up_your_workspace", "btn.create_workspace", "setup.choose_business_type",
    "setup.options_heading", "setup.option_restore_title", "setup.option_restore_desc",
    "setup.option_move_title", "setup.option_move_desc", "setup.more_coming_soon",
    "setup.not_sure_yet", "setup.later_from_dashboard", "setup.currency_hint",
    "setup.business_type_hint", "setup.password_hint",
)


@pytest.mark.parametrize("width", [390, 1280])
def test_setup_screen_in_german(playwright, fresh, width):
    """INV-20 German gate: a de-DE browser gets the whole setup screen in German, with
    no English copy left, nothing scrolling sideways, and the explainers still one line
    at desktop width."""
    import json

    en = json.loads((_LOCALE_DIR / "en.json").read_text())
    de = json.loads((_LOCALE_DIR / "de.json").read_text())
    browser, ctx = _context(playwright, width=width, locale="de-DE", timezone_id="Europe/Berlin")
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        # Visible text plus the placeholders, which carry the dropdown prompts.
        text = page.locator("body").inner_text() + "\n" + "\n".join(page.evaluate(
            "() => Array.from(document.querySelectorAll('[placeholder]')).map(el => el.placeholder)"))
        for key in _SETUP_SCREEN_KEYS:
            de_value = de[key].split("{")[0].strip()
            en_value = en[key].split("{")[0].strip()
            assert de_value != en_value, f"{key} is not translated"
            assert de_value in text, f"{key}: {de_value!r} missing"
            assert en_value not in text, f"{key}: English {en_value!r} on the German page"
        _no_sideways_scroll(page, f"setup de {width}")
        if width == 1280:
            wrapped = page.evaluate("""() => Array.from(document.querySelectorAll(
                  '.setup-card .setup-options-note, .setup-card .form-hint'))
              .filter(el => Math.round(el.getBoundingClientRect().height
                                       / parseFloat(getComputedStyle(el).lineHeight)) !== 1)
              .map(el => el.textContent)""")
            assert not wrapped, wrapped
    finally:
        browser.close()


@pytest.mark.parametrize("width,locale", [(1280, "en-US"), (390, "en-US"), (1280, "de-DE")])
def test_setup_options_side_by_side_small_and_flush_left(playwright, fresh, width, locale):
    """Owner request: the Additional options section is much smaller than the form, and
    its two boxes sit side by side (Restore left, Move your books right), text flush
    left, the same as on the dashboard card."""
    import json

    from .start_options_checks import assert_start_options_layout

    cat = json.loads((_LOCALE_DIR / f"{locale[:2]}.json").read_text())
    browser, ctx = _context(playwright, width=width, locale=locale)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        assert_start_options_layout(page, ".setup-options", cat["setup.option_restore_title"],
                                    cat["setup.option_move_title"])
        sizes = page.evaluate("""() => {
          const px = sel => Array.from(document.querySelectorAll(sel))
            .map(el => parseFloat(getComputedStyle(el).fontSize));
          return {heading: px('.setup-options-heading'), title: px('.setup-options .start-option-title'),
                  text: px('.setup-options .start-option-desc, .setup-options .start-option-note,'
                           + ' .setup-options .setup-options-note'),
                  label: px('#setup-form .form-label')};
        }""")
        assert sizes["heading"] and max(sizes["heading"]) <= 13, sizes
        assert sizes["title"] and max(sizes["title"]) <= 12, sizes
        assert sizes["text"] and max(sizes["text"]) <= 11, sizes
        assert max(sizes["text"]) < min(sizes["label"]), sizes
    finally:
        browser.close()


_PRESETS = REPO / "default_modules" / "celerp-verticals" / "celerp_verticals" / "presets"


def _offered_types() -> dict[str, list[str]]:
    """Every business type the setup page offers, with the modules its preset names."""
    import json

    presets = (json.loads(p.read_text("utf-8")) for p in sorted(_PRESETS.glob("*.json")))
    return {p["name"]: p.get("modules", []) for p in presets if not p.get("hidden")}


@pytest.mark.parametrize("vertical", sorted(_offered_types()))
def test_each_business_type_chosen_first_on_a_new_install(playwright, fresh, vertical):
    """Each business type, chosen on the setup form as the first thing a new install does,
    reaches a dashboard that shows its figures with no error. Celerp restarts to load the
    type's modules exactly when one of them (or a module it needs) is not among those a new
    install runs, and afterwards every module the company uses is running."""
    from celerp.modules.loader import is_core_folded
    from celerp.services.starter_modules import STARTER_MODULES
    from migration_support import smoke_records

    boot = httpx.get(f"{fresh.api}/health", timeout=10).json()["boot"]
    browser, ctx = _context(playwright)
    try:
        page = ctx.new_page()
        visited: list[str] = []
        failures: list[str] = []
        page.on("framenavigated", lambda f: visited.append(urlsplit(f.url).path) if f == page.main_frame else None)
        page.on("pageerror", lambda e: failures.append(str(e)))
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        _fill_text_fields(page, f"{vertical}@first-run.test")
        _choose(page, "vertical", vertical)
        _choose(page, "currency", "USD")
        page.locator('#setup-form button[type="submit"]').click()
        page.wait_for_url(f"{fresh.ui}/dashboard", timeout=180000)
        page.wait_for_load_state("load")

        expect(page.locator(".kpi-grid").first).to_be_visible()
        assert page.locator(".kpi-grid").first.locator(":scope > *").count() > 0, "the dashboard shows no figures"
        expect(page.locator(".charts-section")).to_be_visible()
        expect(page.locator(".quick-links-section")).to_be_visible()
        assert page.locator(".error-banner").count() == 0, page.locator(".error-banner").all_inner_texts()
        assert not failures, failures

        token = next(c["value"] for c in ctx.cookies() if c["name"] == "celerp_token")
        listed = httpx.get(f"{fresh.api}/companies/me/modules", headers={"Authorization": f"Bearer {token}"},
                           timeout=30)
        listed.raise_for_status()
        modules = {m["name"]: m for m in listed.json()}
        uses = smoke_records().turned_on(_offered_types()[vertical], modules)
        not_running_at_first = sorted(m for m in uses if m not in STARTER_MODULES and not is_core_folded(m))
        restarted = "/setup/activating" in visited
        assert restarted == bool(not_running_at_first), (vertical, visited, not_running_at_first)
        assert (httpx.get(f"{fresh.api}/health", timeout=10).json()["boot"] != boot) == restarted
        stopped = {m: modules[m] for m in uses if not (modules[m]["enabled"] and modules[m]["running"])}
        assert not stopped, stopped
        assert fresh.company(ctx)["settings"]["vertical"] == vertical
    finally:
        browser.close()


def test_every_business_type_sets_up_on_a_new_install(fresh, tmp_path):
    """Every business type the setup page offers sets up a company on one new install, one
    after another: Celerp restarts exactly when a type needs modules that are not running,
    every module each type needs runs afterwards, and every company keeps its type. The
    packaged builds take the same walk."""
    from migration_support import smoke_records

    port_file = tmp_path / "api-port"
    port_file.write_text(str(API_PORT))
    smoke_records().first_run(str(port_file), str(_PRESETS))
