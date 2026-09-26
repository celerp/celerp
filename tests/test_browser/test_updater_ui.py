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
    """Screenshot: pip install mode (no window.celerp)."""
    # window.celerp is not defined in the browser test context (not Electron)
    # so the card should enter the pip path automatically.
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.locator(".notif-bell-btn").click()
    page.wait_for_selector("#notif-panel", state="visible")
    # Give the async fetch calls a moment to settle
    page.wait_for_timeout(2000)

    # Panel must be present (implicit assertion via the wait above); capture is opt-in.
    assert page.locator("#update-status-card").count() == 1, "update card missing in pip mode"
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


def _status(**over):
    """An update status as GET /system/update returns it."""
    s = {
        "current": "1.0.0", "latest": "1.1.0", "check_error": "",
        "checked_at": "2026-09-26T03:00:00+00:00", "can_install": True, "reason": "",
        "auto": True, "installing": None, "last_result": None,
    }
    s.update(over)
    return s


def _open_card(page, ui_server, status, *, check_status=None):
    """Serve `status` from the update proxy, open the panel and wait for the card."""
    import json

    page.route(
        "**/system/update",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(status)),
    )
    page.route(
        "**/system/update/check",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(check_status or status)),
    )
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.locator(".notif-bell-btn").click()
    page.wait_for_selector("#notif-panel", state="visible")
    page.wait_for_function(
        "() => document.querySelector('.update-card__version').textContent === 'v1.0.0'")


def test_owner_can_install_and_choose_nightly_updates(page, ui_server):
    _open_card(page, ui_server, _status())
    assert page.locator(".update-card__state").text_content() == "Update available: v1.1.0"
    assert page.locator(".update-card__restart-btn").is_visible()
    assert page.locator(".update-card__auto").is_visible()
    assert page.locator(".update-card__auto-input").is_checked()
    assert not page.locator(".update-card__upgrade-cmd").is_visible()
    _capture(page, "owner-update-available")


def test_member_sees_the_update_but_cannot_install(page, ui_server):
    _open_card(page, ui_server, _status(can_install=False, reason="administrator"))
    assert page.locator(".update-card__state").text_content() == "Update available: v1.1.0"
    assert not page.locator(".update-card__restart-btn").is_visible()
    assert not page.locator(".update-card__auto").is_visible()
    assert not page.locator(".update-card__check-btn").is_visible()
    assert not page.locator(".update-card__upgrade-cmd").is_visible()
    assert page.locator(".update-card__release").text_content() == (
        "Your administrator can install this update.")
    _capture(page, "member-update-available")


def test_unsupervised_install_shows_the_pip_command(page, ui_server):
    _open_card(page, ui_server, _status(can_install=False, reason="unsupervised"))
    assert not page.locator(".update-card__restart-btn").is_visible()
    assert page.locator(".update-card__upgrade-cmd").is_visible()
    assert "celerp start" in page.locator(".update-card__release").text_content()


def test_install_without_pip_route_shows_only_the_reason(page, ui_server):
    _open_card(page, ui_server, _status(can_install=False, reason="container"))
    assert not page.locator(".update-card__upgrade-cmd").is_visible()
    assert page.locator(".update-card__release").text_content() == (
        "Update this container by pulling the new image.")


def test_failed_update_is_reported_while_still_on_offer(page, ui_server):
    failed = {"ok": False, "outcome": "rolled_back", "from": "1.0.0", "to": "1.1.0",
              "reason": "install failed", "at": "x", "notified": True}
    _open_card(page, ui_server, _status(last_result=failed))
    assert page.locator(".update-card__release").text_content() == (
        "Could not update to v1.1.0: install failed. Your data was not changed.")


def test_not_checked_yet_is_not_shown_as_up_to_date(page, ui_server):
    _open_card(page, ui_server, _status(latest=None, checked_at=None))
    assert page.locator(".update-card__state").text_content() == "Not checked yet"


def test_check_button_asks_the_server(page, ui_server):
    _open_card(page, ui_server, _status(latest=None),
               check_status=_status(latest="1.2.0"))
    page.locator(".update-card__check-btn").click()
    page.wait_for_function(
        "() => document.querySelector('.update-card__state').textContent"
        " === 'Update available: v1.2.0'")


def test_card_reads_the_real_status_without_contacting_pypi(page, ui_server):
    """Unstubbed: the card is fed by this install's API, never by the browser."""
    requests: list[str] = []
    page.on("request", lambda req: requests.append(req.url))
    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.locator(".notif-bell-btn").click()
    page.wait_for_selector("#notif-panel", state="visible")
    page.wait_for_function(
        "() => document.querySelector('.update-card__version').textContent.startsWith('v')")
    assert any(u.endswith("/system/update") for u in requests)
    assert not [u for u in requests if "pypi.org" in u]


def test_bell_badge_counts_downloaded_update(page, ui_server):
    """A downloaded update must light the bell badge, not just the panel card.

    Without the fix the Electron update-downloaded handler updates the card text
    and restart button but never the badge, so the icon shows nothing and users
    never notice a waiting upgrade. Here we stub the Electron preload, capture the
    update-downloaded callback, fire it as the main process would, and require the
    bell badge to show a count.
    """
    # The badge count is the shared company's unread notifications plus one for a
    # ready update. This test asserts only the update's contribution, so pin the
    # inbox to empty; otherwise ambient notifications left by earlier tests on the
    # shared session company make the count non-deterministic.
    page.route(
        "**/notifications*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"items": [], "unread_count": 0}',
        ),
    )

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
