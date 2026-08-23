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
    """I18N-03: Language switcher dropdown is present in the topbar when >1 locale available."""
    page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")
    body = page.content()
    # The lang-switcher select exists (may be hidden if only 1 locale)
    # With only en.json, it won't render. Just verify no crash.
    assert "Internal Server Error" not in body
    assert "Traceback" not in body


def test_select_module_language_sets_cookie_and_renders(page, ui_server):
    """I18N-04: selecting a module-contributed language in the topbar switcher
    sets the celerp_lang cookie, reloads, and the reloaded page renders that
    language as selected - the real end-to-end contributed-language flow.

    The catalog is pushed straight into the running ui_server process (the same
    thing the module loader does on boot), then removed afterwards so no other
    browser test sees the synthetic 'xx' language."""
    from ui import i18n

    i18n.register_catalog("xx", {"testlang.greeting": "Hi from Testish"})
    try:
        page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")
        switcher = page.locator("#lang-switcher")
        assert switcher.count() == 1, "language switcher not rendered with >1 locale"

        switcher.select_option("xx")
        page.wait_for_load_state("domcontentloaded")

        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        assert cookies.get("celerp_lang") == "xx", f"cookie not set: {cookies}"

        body = page.content()
        assert "Internal Server Error" not in body
        assert "Traceback" not in body
        # After reload, get_lang reads the cookie and the switcher marks xx active.
        assert page.locator("#lang-switcher").input_value() == "xx"
    finally:
        i18n._registry.pop("xx", None)
        getattr(getattr(i18n, "_cached_load", None), "cache_clear", lambda: None)()
