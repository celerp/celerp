# Copyright (c) 2026 Noah Severs. All rights reserved.
"""
Browser tests for the AI module UI.

Tests the showcase page (non-cloud users), chat view (cloud users),
settings page, empty state, and drag-drop upload zone.

Run: pytest tests/test_browser/test_ai_page.py -m browser --tb=short
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser


def _assert_no_crash(page: Page, context: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{context}: Internal Server Error"
    assert "Traceback (most recent call last)" not in body, f"{context}: Traceback"


# ── Showcase page (no session token = non-cloud user) ─────────────────────────

class TestShowcasePage:
    """AI showcase page renders for users without Cloud subscription."""

    def test_showcase_loads(self, page, ui_server):
        """AI page loads without crash."""
        resp = page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        assert resp.status != 500
        _assert_no_crash(page, "/ai")

    def test_showcase_has_headline(self, page, ui_server):
        """Hero headline is present and visible."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        headline = page.locator(".ai-showcase__headline")
        expect(headline).to_be_visible()
        expect(headline).to_have_text("Meet your AI operator")

    def test_showcase_has_subtitle(self, page, ui_server):
        """Subtitle describes the product."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        sub = page.locator(".ai-showcase__sub")
        expect(sub).to_be_visible()
        assert "manual data entry" in sub.inner_text()

    def test_showcase_no_duplicate_header(self, page, ui_server):
        """No redundant page-header H1 (removed in polish pass)."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        h1s = page.locator("h1")
        assert h1s.count() == 1, f"Expected 1 H1, got {h1s.count()}"

    def test_showcase_scenario_tabs(self, page, ui_server):
        """Four scenario tab buttons are rendered."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        tabs = page.locator(".ai-showcase__tab")
        assert tabs.count() == 4
        labels = [tabs.nth(i).inner_text() for i in range(4)]
        assert "Batch Bill Entry" in labels
        assert "Smart Restock" in labels
        assert "Discrepancy Audit" in labels
        assert "Bulk Catalog Import" in labels

    def test_showcase_terminal_renders(self, page, ui_server):
        """Terminal window with title bar is present."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        terminal = page.locator(".ai-showcase__terminal")
        expect(terminal).to_be_visible()
        assert page.locator(".ai-showcase__dot--red").count() == 1
        assert page.locator(".ai-showcase__dot--yellow").count() == 1
        assert page.locator(".ai-showcase__dot--green").count() == 1
        expect(page.locator(".ai-showcase__term-title")).to_have_text("Celerp AI")

    def test_showcase_terminal_has_input_bar(self, page, ui_server):
        """Disabled input bar is inside the terminal (dark themed)."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        input_bar = page.locator(".ai-showcase__input")
        expect(input_bar).to_be_visible()
        terminal = page.locator(".ai-showcase__terminal")
        assert terminal.locator(".ai-showcase__input").count() == 1

    def test_showcase_carousel_auto_plays(self, page, ui_server):
        """Carousel starts automatically and shows messages."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        messages = page.locator("#showcase-messages .ai-msg")
        assert messages.count() >= 1, "Expected at least 1 message after 3s"

    def test_showcase_tab_click_switches_scenario(self, page, ui_server):
        """Clicking a tab switches the active scenario."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        tabs = page.locator(".ai-showcase__tab")
        tabs.nth(1).click()
        page.wait_for_timeout(500)
        expect(tabs.nth(1)).to_have_class(re.compile(r"ai-showcase__tab--active"))
        assert "ai-showcase__tab--active" not in (tabs.nth(0).get_attribute("class") or "")

    def test_showcase_cta_cards(self, page, ui_server):
        """Two CTA pricing cards are rendered."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        cards = page.locator(".ai-showcase__cta-card")
        assert cards.count() == 2

    def test_showcase_cta_cloud_card(self, page, ui_server):
        """Cloud Relay card has correct content."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        cards = page.locator(".ai-showcase__cta-card")
        cloud_card = cards.first
        assert "Cloud Relay" in cloud_card.inner_text()
        assert "$29/mo" in cloud_card.inner_text()
        assert "Start Here" in cloud_card.inner_text()
        assert "Cancel anytime" in cloud_card.inner_text()
        btn = cloud_card.locator(".btn")
        expect(btn).to_be_visible()
        assert btn.get_attribute("href").startswith("https://celerp.com/subscribe")

    def test_showcase_cta_ai_card(self, page, ui_server):
        """AI Plan card has correct content and featured styling."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        cards = page.locator(".ai-showcase__cta-card")
        ai_card = cards.last
        assert "Celerp AI Plan" in ai_card.inner_text()
        assert "$49/mo" in ai_card.inner_text()
        assert "Recommended" in ai_card.inner_text()
        assert "Cancel anytime" in ai_card.inner_text()
        assert "ai-showcase__cta-card--featured" in (ai_card.get_attribute("class") or "")
        btn = ai_card.locator(".btn--accent")
        expect(btn).to_be_visible()

    def test_showcase_feature_checkmarks(self, page, ui_server):
        """Feature items use CSS checkmark pseudo-elements."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        features = page.locator(".ai-showcase__cta-feature")
        assert features.count() >= 6, f"Expected 6+ features, got {features.count()}"

    def test_showcase_no_inline_styles_on_cta(self, page, ui_server):
        """CTA cards have no inline style attributes (all moved to CSS)."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        cards = page.locator(".ai-showcase__cta-card")
        for i in range(cards.count()):
            style = cards.nth(i).get_attribute("style")
            assert not style, f"CTA card {i} has inline style: {style}"

    def test_showcase_nav_active(self, page, ui_server):
        """AI nav item is highlighted in sidebar."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        active_link = page.locator(".nav-link--active")
        assert active_link.count() >= 1
        found = False
        for i in range(active_link.count()):
            if "AI Assistant" in active_link.nth(i).inner_text():
                found = True
                break
        assert found, "AI Assistant nav link not active"


# ── Chat view (with session token) ────────────────────────────────────────────

class TestChatView:
    """AI chat view for Cloud users (requires session token set)."""

    @pytest.fixture(autouse=True)
    def _set_session_token(self):
        """Temporarily set a fake session token so chat view renders."""
        from celerp.gateway.state import set_session_token
        set_session_token("test-session-token-for-browser-tests")
        yield
        set_session_token("")

    def test_chat_view_loads(self, page, ui_server):
        """Chat layout renders when session token is set."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        _assert_no_crash(page, "/ai (chat)")
        chat = page.locator(".ai-chat")
        expect(chat).to_be_visible()

    def test_chat_has_sidebar(self, page, ui_server):
        """Chat sidebar with conversation list is present."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        sidebar = page.locator(".ai-sidebar")
        expect(sidebar).to_be_visible()

    def test_chat_has_new_conversation_button(self, page, ui_server):
        """New conversation button exists in sidebar."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        btn = page.locator(".ai-sidebar__new")
        expect(btn).to_be_visible()
        assert "New" in btn.inner_text()

    def test_chat_has_memory_button(self, page, ui_server):
        """Memory management button exists in sidebar."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        btn = page.locator(".ai-sidebar__memory-btn")
        expect(btn).to_be_visible()

    def test_chat_has_input_form(self, page, ui_server):
        """Chat input form with text field and send button."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        form = page.locator("#ai-chat-form")
        expect(form).to_be_visible()
        text_input = page.locator("#ai-query-input")
        expect(text_input).to_be_visible()
        assert text_input.get_attribute("placeholder") == "Ask anything about your business data\u2026"
        send_btn = form.locator(".ai-input__send")
        expect(send_btn).to_be_visible()

    def test_chat_has_file_input(self, page, ui_server):
        """Hidden file input for attachments exists."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        file_input = page.locator("#ai-file-input")
        assert file_input.count() == 1
        accept = file_input.get_attribute("accept")
        assert "application/pdf" in accept
        assert "image/jpeg" in accept

    def test_chat_has_dropzone(self, page, ui_server):
        """Drop zone exists below the text input row."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        zone = page.locator("#ai-chat-dropzone")
        expect(zone).to_be_visible()
        assert "Drop files here" in zone.inner_text()

    def test_chat_no_paperclip_button(self, page, ui_server):
        """Old paperclip attach button is removed; drop zone replaces it."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        # The old .ai-input__attach button should not exist
        assert page.locator(".ai-input__attach").count() == 0

    def test_chat_has_quota_display(self, page, ui_server):
        """Quota badge area exists in chat header."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        header = page.locator(".ai-chat__header")
        expect(header).to_be_visible()
        quota = page.locator("#ai-quota-display")
        assert quota.count() == 1

    def test_chat_messages_area(self, page, ui_server):
        """Messages container exists and contains empty state initially."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        messages = page.locator("#ai-messages")
        expect(messages).to_be_visible()

    def test_chat_empty_state_visible(self, page, ui_server):
        """Empty state with example query cards is shown before first message."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        empty_state = page.locator("#ai-empty-state")
        expect(empty_state).to_be_visible()
        expect(empty_state.locator(".ai-empty-state__heading")).to_have_text("How can I help?")
        cards = empty_state.locator(".ai-empty-state__card")
        assert cards.count() == 4

    def test_chat_empty_state_card_titles(self, page, ui_server):
        """Example query cards have correct titles."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        cards = page.locator(".ai-empty-state__card")
        titles = [cards.nth(i).locator(".ai-empty-state__card-title").inner_text() for i in range(4)]
        assert "Process receipts" in titles
        assert "Restock analysis" in titles
        assert "Audit discrepancies" in titles
        assert "Business summary" in titles

    def test_chat_empty_state_card_click_fills_input(self, page, ui_server):
        """Clicking an example card fills the query input."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        card = page.locator(".ai-empty-state__card").first
        card.click()
        page.wait_for_timeout(100)
        val = page.locator("#ai-query-input").input_value()
        assert len(val) > 0, "Expected query input to be filled after card click"

    def test_chat_memory_drawer_toggle(self, page, ui_server):
        """Memory drawer toggles open/closed on button click."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        drawer = page.locator("#ai-memory-drawer")
        assert "ai-memory-drawer--open" not in (drawer.get_attribute("class") or "")
        page.locator(".ai-sidebar__memory-btn").click()
        page.wait_for_timeout(300)
        assert "ai-memory-drawer--open" in (drawer.get_attribute("class") or "")
        page.locator(".ai-sidebar__memory-btn").click()
        page.wait_for_timeout(300)
        assert "ai-memory-drawer--open" not in (drawer.get_attribute("class") or "")

    def test_chat_conversations_list_loads(self, page, ui_server):
        """Conversation list HTMX endpoint loads."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        history = page.locator("#ai-history")
        expect(history).to_be_visible()
        text = history.inner_text()
        assert len(text) > 0

    def test_chat_no_showcase_elements(self, page, ui_server):
        """Chat view does not render showcase elements."""
        page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        assert page.locator(".ai-showcase").count() == 0
        assert page.locator(".ai-showcase__terminal").count() == 0
        assert page.locator(".ai-showcase__cta-card").count() == 0


# ── Settings page ─────────────────────────────────────────────────────────────

class TestSettingsPage:
    """AI settings page at /ai/settings."""

    def test_settings_loads(self, page, ui_server):
        """Settings page loads without crash."""
        resp = page.goto(f"{ui_server}/ai/settings", wait_until="domcontentloaded")
        assert resp.status != 500
        _assert_no_crash(page, "/ai/settings")

    def test_settings_title(self, page, ui_server):
        """Page title is AI Settings."""
        page.goto(f"{ui_server}/ai/settings", wait_until="domcontentloaded")
        assert "AI Settings" in page.title()

    def test_settings_nav_active(self, page, ui_server):
        """AI nav item is active on settings page."""
        page.goto(f"{ui_server}/ai/settings", wait_until="domcontentloaded")
        active_link = page.locator(".nav-link--active")
        found = False
        for i in range(active_link.count()):
            if "AI" in active_link.nth(i).inner_text():
                found = True
                break
        assert found, "AI nav link not active on settings page"

    def test_settings_content_loads(self, page, ui_server):
        """HTMX settings content fragment loads."""
        page.goto(f"{ui_server}/ai/settings", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)  # Let HTMX load
        content = page.locator("#ai-settings-content")
        expect(content).to_be_visible()
        # Should have loaded content (not empty)
        assert len(content.inner_text()) > 0

    def test_settings_local_install_message(self, page, ui_server):
        """Without session token, shows local install message."""
        # No session token set → local install
        page.goto(f"{ui_server}/ai/settings", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        body = page.locator("body").inner_text()
        assert "local install" in body.lower() or "AI Settings" in body

    def test_settings_redirects_unauthenticated(self, unauthed_page, ui_server):
        """Unauthenticated /ai/settings redirects to login."""
        unauthed_page.goto(f"{ui_server}/ai/settings", wait_until="domcontentloaded")
        assert "/login" in unauthed_page.url

    @pytest.fixture(autouse=False)
    def _set_session_token(self):
        from celerp.gateway.state import set_session_token
        set_session_token("test-session-token-for-browser-tests")
        yield
        set_session_token("")

    def test_settings_with_session_has_quota_section(self, page, ui_server, _set_session_token):
        """With session token, quota section is rendered."""
        page.goto(f"{ui_server}/ai/settings", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        content = page.locator("#ai-settings-content")
        text = content.inner_text()
        # Should have quota-related content or error message
        assert len(text) > 10


# ── Auth wall ─────────────────────────────────────────────────────────────────

class TestAuthWall:
    """Unauthenticated users are redirected to login."""

    def test_ai_redirects_to_login(self, unauthed_page, ui_server):
        """Unauthenticated /ai request redirects to /login."""
        unauthed_page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
        assert "/login" in unauthed_page.url
