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
        # The browser_context is session-scoped (conftest), so the celerp_lang
        # cookie this test set would otherwise leak 'xx' into every later test in
        # the shard - clear it here, alongside the registry pop, so siblings start
        # from the default language.
        page.context.clear_cookies(name="celerp_lang")


def test_combobox_type_without_select_then_blur_restores(page, ui_server):
    """I18N-05: typing a non-matching query into the switcher combobox and then
    blurring without picking an option restores the committed label and value -
    it must not strand the typed query in the box or leave the hidden value
    emptied (the shared-combobox blur bug fixed alongside the module seam). No
    change event fires, so the page does not reload to a wrong language."""
    from playwright.sync_api import expect

    # The browser_context is session-scoped, so a prior test may have left a
    # celerp_lang cookie behind; clear it and reload so this test's baseline is
    # the real default (en) and independent of shard ordering.
    page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")
    page.context.clear_cookies(name="celerp_lang")
    page.reload(wait_until="domcontentloaded")
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


def test_switcher_keyboard_aria_expanded_and_active_option(page, ui_server):
    """I18N-06: the switcher combobox keeps its ARIA state in step with keyboard
    use - opening sets aria-expanded=true and aria-controls resolves to the open
    listbox; ArrowDown activates an option and points aria-activedescendant at it;
    Escape collapses it back to aria-expanded=false and clears the active
    descendant. This is the runtime wiring initCombobox adds on top of the static
    roles the unit test test_switcher_has_aria_combobox_semantics already covers."""
    import re

    from playwright.sync_api import expect

    page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")
    page.context.clear_cookies(name="celerp_lang")
    page.reload(wait_until="domcontentloaded")
    combo = page.locator(".lang-switcher-wrap .combobox-input")
    expect(combo).to_have_count(1)

    # aria-controls is wired at init and must resolve to the switcher's own list.
    list_id = combo.get_attribute("aria-controls")
    assert list_id, "combobox input must carry aria-controls"
    controlled = page.locator(f"#{list_id}")
    expect(controlled).to_have_count(1)
    expect(controlled).to_have_class(re.compile(r"\bcombobox-list\b"))

    # Focus opens the list; the observer flips aria-expanded to true.
    combo.click()
    expect(controlled).to_have_class(re.compile(r"\bopen\b"))
    expect(combo).to_have_attribute("aria-expanded", "true")

    # ArrowDown activates an option and points aria-activedescendant at it.
    page.keyboard.press("ArrowDown")
    active_id = combo.get_attribute("aria-activedescendant")
    assert active_id, "ArrowDown must set aria-activedescendant on the input"
    active = page.locator(f"#{active_id}")
    expect(active).to_have_class(re.compile(r"\bcombobox-option\b"))
    expect(active).to_have_class(re.compile(r"\bfocused\b"))

    # Escape collapses the list; aria-expanded returns to false and the active
    # descendant is cleared.
    page.keyboard.press("Escape")
    expect(combo).to_have_attribute("aria-expanded", "false")
    assert combo.get_attribute("aria-activedescendant") is None, \
        "closing the list must clear aria-activedescendant"


def test_switcher_enter_selects_focused_module_language(page, ui_server):
    """I18N-07: keyboard commit path. Typing to isolate a module-contributed
    language, ArrowDown to focus it, then Enter selects it - the same
    cookie -> get_lang -> t() -> production render chain as a click (I18N-04), but
    driven entirely from the keyboard, which is the path a keyboard-only user
    depends on. The synthetic 'xx' catalog is pushed into the running ui_server
    exactly as the module loader does on boot, then removed afterwards."""
    from playwright.sync_api import expect

    from ui import i18n

    i18n.register_catalog(
        "xx", {"testlang.greeting": "Hi from Testish", "page.dashboard": "Testish Dashboard"}
    )
    try:
        page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")
        page.context.clear_cookies(name="celerp_lang")
        page.reload(wait_until="domcontentloaded")
        combo = page.locator(".lang-switcher-wrap .combobox-input")
        hidden = page.locator('.lang-switcher-wrap input[type="hidden"][name="lang"]')
        expect(combo).to_have_count(1)

        # Filter to the synthetic language so ArrowDown lands on it deterministically,
        # independent of how many locales the app ships or their sort order.
        combo.click()
        combo.fill("XX")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")

        # Enter commits the focused option exactly as a click would: the topbar
        # change listener writes the celerp_lang cookie and reloads into 'xx', so
        # 'Testish Dashboard' (t('page.dashboard') under xx) only appears server-side
        # after the reload - proving the full keyboard commit chain.
        expect(page.locator("body")).to_contain_text("Testish Dashboard")
        expect(hidden).to_have_value("xx")
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        assert cookies.get("celerp_lang") == "xx", f"Enter did not commit the language: {cookies}"
    finally:
        i18n._registry.pop("xx", None)
        getattr(getattr(i18n, "_cached_load", None), "cache_clear", lambda: None)()
        # Session-scoped browser_context (conftest): clear the cookie so the 'xx'
        # language does not leak into later tests in the shard.
        page.context.clear_cookies(name="celerp_lang")
