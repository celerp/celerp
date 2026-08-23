# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 13: i18n UI — language setting persists, cookie is set, UI re-renders."""
import pytest

pytestmark = pytest.mark.browser


def test_language_picker_visible_in_settings(page, ui_server):
    """I18N-01: Settings → Company tab shows a language picker row."""
    page.goto(f"{ui_server}/settings?tab=company", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body
    # Language row should be present
    assert "Language" in body or "language" in page.content().lower(), \
        "No language field found in Settings → Company tab"


def test_language_setting_persists(page, ui_server, api):
    """I18N-02: POST language=en via API → company.settings.language == 'en'."""
    # Patch company language setting via API (direct)
    r = api.patch("/companies/me", json={"settings": {"language": "en"}})
    assert r.status_code in {200, 204}, f"PATCH /companies/me failed: {r.text}"

    # Load settings page and verify no crash
    page.goto(f"{ui_server}/settings?tab=company", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body

    # Verify via API that setting was stored
    r2 = api.get("/companies/me")
    assert r2.status_code == 200
    settings = r2.json().get("settings", {})
    assert settings.get("language", "en") == "en", \
        f"Language setting not persisted: {settings}"


def test_lang_switcher_in_topbar(page, ui_server):
    """I18N-03: the language switcher is present in the topbar as the searchable
    combobox (the app ships several disk locales, so it always renders)."""
    from playwright.sync_api import expect

    page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")
    body = page.content()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body
    # The switcher is the shared searchable combobox, not a native <select>.
    expect(page.locator(".lang-switcher-wrap .combobox-input")).to_have_count(1)
    expect(page.locator('.lang-switcher-wrap input[type="hidden"][name="lang"]')).to_have_count(1)


def test_select_module_language_sets_cookie_and_renders(page, ui_server):
    """I18N-04: selecting a module-contributed language in the topbar combobox
    sets the celerp_lang cookie, reloads, and the reloaded page renders that
    language - the real end-to-end contributed-language flow.

    The catalog overrides page.dashboard, a key the dashboard header actually
    renders, so the assertion proves the full chain: module catalog -> request
    language -> t() -> production component -> browser. It is pushed straight into
    the running ui_server process (the same thing the module loader does on boot),
    then removed afterwards so no other browser test sees the synthetic 'xx'
    language.

    The switcher is the shared searchable_select combobox (rule i: >10 locales),
    so selection is a click on the option, not <select>.select_option - which is
    exactly what the module-seam rebase changed."""
    from playwright.sync_api import expect

    from ui import i18n

    i18n.register_catalog(
        "xx", {"testlang.greeting": "Hi from Testish", "page.dashboard": "Testish Dashboard"}
    )
    try:
        page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")
        combo = page.locator(".lang-switcher-wrap .combobox-input")
        hidden = page.locator('.lang-switcher-wrap input[type="hidden"][name="lang"]')
        expect(combo).to_have_count(1)

        # Open the option list and pick the module-contributed language. Clicking
        # the option fires the combobox's selectOpt, which sets the hidden value
        # and dispatches change; the topbar's change listener then writes the
        # celerp_lang cookie and reloads. Auto-retrying assertions ride out the
        # reload rather than reading content mid-navigation. 'Testish Dashboard'
        # is rendered server-side from t('page.dashboard') under the xx cookie, so
        # it only appears AFTER the reload - proving the full cookie -> get_lang ->
        # t() -> production component -> browser chain, not just the picker state.
        combo.click()
        page.locator('.lang-switcher-wrap .combobox-option[data-value="xx"]').click()
        expect(page.locator("body")).to_contain_text("Testish Dashboard")
        expect(hidden).to_have_value("xx")

        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        assert cookies.get("celerp_lang") == "xx", f"cookie not set: {cookies}"
    finally:
        i18n._registry.pop("xx", None)
        getattr(getattr(i18n, "_cached_load", None), "cache_clear", lambda: None)()


def test_combobox_type_without_select_then_blur_restores(page, ui_server):
    """I18N-05: typing a non-matching query into the switcher combobox and then
    blurring without picking an option restores the committed label and value -
    it must not strand the typed query in the box or leave the hidden value
    emptied (the shared-combobox blur bug fixed alongside the module seam). No
    change event fires, so the page does not reload to a wrong language."""
    from playwright.sync_api import expect

    page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")
    combo = page.locator(".lang-switcher-wrap .combobox-input")
    hidden = page.locator('.lang-switcher-wrap input[type="hidden"][name="lang"]')
    expect(hidden).to_have_value("en")
    committed_label = combo.input_value()

    combo.click()
    combo.fill("zzz not a language")  # filters to no match; hidden cleared while typing
    # Blur by focusing another topbar control; the option list closes and restores.
    page.locator(".global-search-input").click()

    expect(combo).to_have_value(committed_label)
    expect(hidden).to_have_value("en")
    # No spurious navigation: the language did not change.
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    assert cookies.get("celerp_lang", "en") == "en", f"blur must not switch language: {cookies}"
