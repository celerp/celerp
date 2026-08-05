# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests for the update status card in the notifications panel."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

# Debug screenshots are opt-in: set CELERP_SCREENSHOT_DIR to capture them.
# Unset / empty (the default) skips capture so the tests depend on no fixed path.
SCREENSHOT_DIR = os.environ.get("CELERP_SCREENSHOT_DIR", "")


def _capture(page, name: str) -> None:
    """Save a debug screenshot of the notifications panel, if a dir is configured."""
    if not SCREENSHOT_DIR:
        return
    d = Path(SCREENSHOT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    page.locator("#notif-panel").screenshot(path=str(d / f"{name}.png"))


def test_update_card_renders_in_notifications_panel(page, ui_server):
    """The update status card must be present inside the notifications panel."""
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    # Open the notifications panel
    page.locator(".notif-bell-btn").click()
    page.wait_for_selector("#notif-panel", state="visible")

    card = page.locator("#update-status-card")
    assert card.count() == 1, "update-status-card not found in notifications panel"


def test_update_card_pypi_mode_screenshot(page, ui_server):
    """Screenshot: PyPI mode (no window.celerp). Shows version from /health."""
    # window.celerp is not defined in the browser test context (not Electron)
    # so the card should enter the PyPI path automatically.
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.locator(".notif-bell-btn").click()
    page.wait_for_selector("#notif-panel", state="visible")
    # Give the async fetch calls a moment to settle
    page.wait_for_timeout(2000)

    # Panel must be present (implicit assertion via the wait above); capture is opt-in.
    assert page.locator("#update-status-card").count() == 1, "update card missing in PyPI mode"
    _capture(page, "pypi-mode")


def test_update_card_electron_mode_screenshot(page, ui_server):
    """Screenshot: Electron mode stub (window.celerp injected via JS)."""
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")

    # Inject a minimal window.celerp stub to simulate the Electron preload
    page.evaluate("""() => {
        window.celerp = {
            getVersion: () => Promise.resolve('1.0.9'),
            onUpdateAvailable: () => {},
            onUpdateDownloaded: () => {},
            checkForUpdates: () => Promise.resolve(),
            installUpdate: () => {},
            showConfirm: () => true,
            openExternal: () => Promise.resolve(),
        };
    }""")

    # Re-trigger the init by dispatching DOMContentLoaded equivalent - reload
    page.reload(wait_until="domcontentloaded")

    # Inject the stub again after reload (before the JS event fires is not possible,
    # so we invoke initUpdateCard logic directly after defining the stub)
    page.evaluate("""() => {
        window.celerp = {
            getVersion: () => Promise.resolve('1.0.9'),
            onUpdateAvailable: () => {},
            onUpdateDownloaded: () => {},
            checkForUpdates: () => Promise.resolve(),
            installUpdate: () => {},
            showConfirm: () => true,
            openExternal: () => Promise.resolve(),
        };
        // Manually set version display
        var versionEl = document.querySelector('.update-card__version');
        if (versionEl) versionEl.textContent = 'v1.0.9';
        var stateEl = document.querySelector('.update-card__state');
        if (stateEl) stateEl.textContent = 'Up to date';
    }""")

    page.locator(".notif-bell-btn").click()
    page.wait_for_selector("#notif-panel", state="visible")
    page.wait_for_timeout(500)

    # Panel must render with the injected Electron stub; capture is opt-in.
    assert page.locator("#update-status-card").count() == 1, "update card missing in Electron mode"
    _capture(page, "electron-mode")


def test_update_card_releases_url(page, ui_server):
    """The Releases link must point to github.com/celerp/celerp/releases."""
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.locator(".notif-bell-btn").click()
    page.wait_for_selector("#notif-panel", state="visible")

    link = page.locator(".update-card__releases-link")
    href = link.get_attribute("href")
    assert href == "https://github.com/celerp/celerp/releases", (
        f"Wrong releases URL: {href}"
    )


def test_update_card_check_btn_present(page, ui_server):
    """Check for updates button must be present in the card."""
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.locator(".notif-bell-btn").click()
    page.wait_for_selector("#notif-panel", state="visible")

    btn = page.locator(".update-card__check-btn")
    assert btn.count() == 1, "Check for updates button not found"


def test_bell_badge_counts_downloaded_update(page, ui_server):
    """A downloaded update must light the bell badge, not just the panel card.

    Without the fix the Electron update-downloaded handler updates the card text
    and restart button but never the badge, so the icon shows nothing and users
    never notice a waiting upgrade. Here we stub the Electron preload, capture the
    update-downloaded callback, fire it as the main process would, and require the
    bell badge to show a count.
    """
    # Define the preload stub before page scripts run so initUpdateCard takes the
    # Electron path and registers against it. The stub stores the downloaded
    # callback so the test can fire it deterministically (no real download).
    page.add_init_script(
        """
        window.__fireUpdateDownloaded = null;
        window.celerp = {
          getVersion: () => Promise.resolve('2.0.0'),
          onUpdateLog: () => {},
          onUpdateAvailable: () => {},
          onDownloadProgress: () => {},
          onUpdateNotAvailable: () => {},
          onUpdateDownloaded: (cb) => { window.__fireUpdateDownloaded = cb; },
          onUpdateError: () => {},
          checkForUpdates: () => Promise.resolve(),
          installUpdate: () => {},
        };
        """
    )
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")

    # Init ran and captured the callback.
    page.wait_for_function("() => typeof window.__fireUpdateDownloaded === 'function'")

    badge = page.locator("#notif-badge")
    assert not badge.is_visible(), "badge should be hidden before any update is ready"

    # Fire the event exactly as the Electron main process does on download.
    page.evaluate("() => window.__fireUpdateDownloaded({ version: '2.0.1' })")

    assert badge.is_visible(), "bell badge did not appear when an update was downloaded"
    assert badge.text_content() == "1", (
        f"expected badge count 1 for a ready update, got {badge.text_content()!r}"
    )
