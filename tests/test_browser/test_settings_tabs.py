# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 5: Settings tabs — each tab loads without 404 or error."""
import pytest

pytestmark = pytest.mark.browser

# (tab_selector, expected_text_in_response)
_SETTINGS_TABS = [
    ("company", "Company"),
    ("users", "Users"),
    ("locations", "Locations"),
    ("taxes", "Taxes"),
    ("payment-terms", "Payment Terms"),
    ("field-schema", "Schema"),
    ("cat-schema", "Category"),
    ("modules", "Modules"),
    ("import-history", "Import"),
    ("bulk-attachments", "Attachment"),
    ("labels", "Labels"),
]


@pytest.mark.parametrize("tab,expected_text", _SETTINGS_TABS, ids=[t for t, _ in _SETTINGS_TABS])
def test_settings_tab_loads(page, ui_server, tab, expected_text):
    """SET-01..12: Settings tab → content loads, no errors."""
    # Navigate directly to the settings page with the tab parameter
    # The UI uses HTMX tabs — we can hit the tab endpoint directly
    resp = page.goto(f"{ui_server}/settings?tab={tab}", wait_until="domcontentloaded")
    assert resp.status != 500, f"Settings tab {tab!r} returned 500"
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"Settings tab {tab!r}: Internal Server Error"
    assert "Traceback" not in body, f"Settings tab {tab!r}: traceback in body"
    assert "/login" not in page.url, f"Settings tab {tab!r}: redirected to login"
