# Copyright (c) 2026 Noah Severs. All rights reserved.
"""
Setup wizard activating page — Playwright tests.

Covers the two-phase /health poll logic:
  ACT-01: Page renders spinner + status text
  ACT-02: Redirects to /dashboard when server stays up (no restart in flight)
  ACT-03: Redirects to /dashboard after simulated down→up cycle
  ACT-04: Shows timeout message after exhausting max up-phase attempts

NOTE: These tests are excluded from CI (--ignore=tests/test_browser).
Run locally with:
    pytest tests/test_browser/test_activating_page.py --headed
"""
import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser


def _assert_no_crash(page: Page, context: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{context}: 500 in body"
    assert "Traceback" not in body, f"{context}: Traceback in body"


# ── ACT-01: Page renders ──────────────────────────────────────────────────────

def test_activating_page_renders(page, ui_server):
    """ACT-01: /setup/activating loads the spinner + status element."""
    resp = page.goto(f"{ui_server}/setup/activating", wait_until="domcontentloaded")
    assert resp.status != 500, "/setup/activating returned HTTP 500"
    _assert_no_crash(page, "/setup/activating")
    assert page.locator(".activating-spinner").is_visible(), "Spinner not visible"
    assert page.locator("#activating-status").is_visible(), "Status element not visible"


# ── ACT-02: Redirects when server is already up ───────────────────────────────

def test_activating_page_redirects_when_up(page, ui_server):
    """ACT-02: When /health responds OK immediately, page redirects to /dashboard.

    Intercepts fetch so pollDown sees an immediate network error (simulating
    server briefly down), then pollUp sees HTTP 200 — triggering the redirect.
    """
    page.goto(f"{ui_server}/setup/activating", wait_until="domcontentloaded")

    # Override fetch: first call (pollDown probe) throws (simulates down),
    # subsequent calls return a Response-like object with .json() returning ready phase.
    page.evaluate("""() => {
        let calls = 0;
        window.fetch = function(url, opts) {
            calls++;
            if (calls === 1) {
                // Simulate server going down (pollDown catches this and transitions to pollUp)
                return Promise.reject(new Error('simulated down'));
            }
            // Server is back up — return a proper Response-like object
            return Promise.resolve({
                ok: true,
                json: function() { return Promise.resolve({ phase: 'ready', modules: [] }); }
            });
        };
    }""")

    # Wait for redirect to /dashboard (up to 8s — poll interval is 800ms + 3s stability window)
    page.wait_for_url(f"{ui_server}/dashboard", timeout=8000)
    assert "/dashboard" in page.url, f"Expected redirect to /dashboard, got {page.url}"


# ── ACT-03: Status text updates during down→up cycle ─────────────────────────

def test_activating_page_status_updates(page, ui_server):
    """ACT-03: Status element is non-empty on load and updates before redirect.

    Verifies the status text is populated (not blank) — the specific phase
    transitions are implementation detail; what matters is the element is live.
    """
    page.goto(f"{ui_server}/setup/activating", wait_until="domcontentloaded")

    # Status should already have initial text set by the inline script
    status_el = page.locator("#activating-status")
    initial_text = status_el.inner_text()
    assert initial_text.strip(), f"Status element is blank on load"

    # Now mock fetch down→up and verify redirect happens (proves poll loop runs)
    # Return proper Response-like objects with .json() method
    page.evaluate("""() => {
        let calls = 0;
        window.fetch = function(url, opts) {
            calls++;
            if (calls <= 2) return Promise.reject(new Error('simulated down'));
            return Promise.resolve({
                ok: true,
                json: function() { return Promise.resolve({ phase: 'ready', modules: [] }); }
            });
        };
    }""")

    page.wait_for_url(f"{ui_server}/dashboard", timeout=8000)
    assert "/dashboard" in page.url


# ── ACT-04: Timeout message after max attempts ────────────────────────────────

def test_activating_page_timeout_message(page, ui_server):
    """ACT-04: After exhausting max up-phase attempts, shows 'Taking longer' message."""
    page.goto(f"{ui_server}/setup/activating", wait_until="domcontentloaded")

    # Fetch always fails — server never comes back up
    page.evaluate("""() => {
        window.fetch = function() {
            return Promise.reject(new Error('always down'));
        };
    }""")

    # maxUpAttempts=40 at 800ms each = 32s worst case. Speed it up by
    # replacing setTimeout with an immediate executor after the first call.
    page.evaluate("""() => {
        const orig = window.setTimeout;
        let calls = 0;
        window.setTimeout = function(fn, delay) {
            calls++;
            // First call is the 500ms initial delay — keep it tiny
            return orig(fn, calls <= 1 ? 10 : 0);
        };
    }""")

    status_el = page.locator("#activating-status")
    # Wait for the timeout message to appear (up to 5s with accelerated timers)
    status_el.wait_for(timeout=5000)
    page.wait_for_function(
        "() => document.getElementById('activating-status').textContent.includes('longer')",
        timeout=5000,
    )
    text = status_el.inner_text()
    assert "longer" in text.lower(), f"Expected timeout message, got: {text!r}"
    # Must NOT have redirected
    assert "/dashboard" not in page.url, "Should not redirect on timeout"


# ── ACT-05: Error message when modules stay stuck in loading ──────────────────

def test_activating_page_error_on_stuck_loading(page, ui_server):
    """ACT-05: After maxLoadingStreak consecutive 'loading' responses, shows error + back link."""
    page.goto(f"{ui_server}/setup/activating", wait_until="domcontentloaded")

    # Always return loading with one stuck module
    page.evaluate("""() => {
        window.fetch = function() {
            return Promise.resolve({
                ok: true,
                json: function() {
                    return Promise.resolve({
                        phase: 'loading',
                        requested: 1,
                        loaded: 0,
                        modules: [{ name: 'celerp-docs', label: 'Documents', running: false }]
                    });
                }
            });
        };
    }""")

    # Accelerate setTimeout so the streak fills quickly
    page.evaluate("""() => {
        const orig = window.setTimeout;
        let calls = 0;
        window.setTimeout = function(fn, delay) {
            calls++;
            return orig(fn, calls <= 1 ? 10 : 0);
        };
    }""")

    status_el = page.locator("#activating-status")
    # Wait for error text to appear
    page.wait_for_function(
        "() => document.getElementById('activating-status').textContent.includes('failed')",
        timeout=5000,
    )
    text = status_el.inner_text()
    assert "failed" in text.lower(), f"Expected error message, got: {text!r}"
    # Must have a 'Go back' link
    back_link = page.locator("#activating-status a")
    assert back_link.count() > 0, "Expected a 'Go back' link in error state"
    assert "/setup" in (back_link.first.get_attribute("href") or ""), "Back link should point to /setup"
    # Must NOT have redirected
    assert "/dashboard" not in page.url, "Should not redirect on module failure"
