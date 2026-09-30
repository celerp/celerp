# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests for the update status card in the notifications panel."""
from __future__ import annotations

import json
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


def _pin_empty_inbox(page):
    """Pin the inbox to empty so the bell badge counts only the update.

    Ambient notifications left by earlier tests on the shared session company
    would otherwise make the count non-deterministic.
    """
    page.route(
        "**/notifications*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"items": [], "unread_count": 0}',
        ),
    )


_UPDATE_STATE_JS = Path(__file__).parents[2] / "electron" / "update-state.js"


def _fake_electron(page, seed=()):
    """Stand in for the Electron main process and preload bridge.

    The real electron/update-state.js runs in the page as the main process.
    Every updater event is kept in sessionStorage and replayed into a fresh
    tracker on each page load, so the "main process" outlives navigation just
    as the real one does, while each page load gets a fresh renderer. `seed`
    holds events the updater fired before the first page opened.
    """
    _pin_empty_inbox(page)
    page.add_init_script(
        """
        (function() {
          var KEY = '__fakeUpdaterEvents';
          if (sessionStorage.getItem(KEY) === null) sessionStorage.setItem(KEY, JSON.stringify(%s));
          var module = { exports: {} };
          (function(module) { %s })(module);
          var handlers = {};
          var updater = { on: function(name, fn) { (handlers[name] = handlers[name] || []).push(fn); } };
          function fire(name, payload) { (handlers[name] || []).forEach(function(fn) { fn(payload); }); }
          var windowCallbacks = {};
          var live = false;
          var getUpdateState = module.exports.trackUpdater(updater, function(channel, state) {
            if (live && windowCallbacks[channel]) windowCallbacks[channel](state);
          });
          JSON.parse(sessionStorage.getItem(KEY)).forEach(function(e) { fire(e[0], e[1]); });
          live = true;
          window.__updaterEmit = function(name, payload) {
            var events = JSON.parse(sessionStorage.getItem(KEY));
            events.push([name, payload]);
            sessionStorage.setItem(KEY, JSON.stringify(events));
            fire(name, payload);
          };
          function on(channel) { return function(cb) { windowCallbacks[channel] = cb; }; }
          window.celerp = {
            getVersion: function() { return Promise.resolve('2.0.0'); },
            getUpdateState: function() { return Promise.resolve(getUpdateState()); },
            onUpdateLog: on('update-log'),
            onUpdateAvailable: on('update-available'),
            onDownloadProgress: on('download-progress'),
            onUpdateNotAvailable: on('update-not-available'),
            onUpdateDownloaded: on('update-downloaded'),
            onUpdateError: on('update-error'),
            checkForUpdates: function() { return Promise.resolve(); },
            installUpdate: function() {},
          };
        })();
        """ % (json.dumps(list(seed)), _UPDATE_STATE_JS.read_text())
    )


# What the user can see of the updater: the bell badge and the update card.
_VISIBLE_STATE_JS = """() => {
  var q = function(s) { return document.querySelector(s); };
  var shown = function(el) { return !!el && el.style.display !== 'none'; };
  var badge = q('#notif-badge');
  return {
    badge: shown(badge) ? badge.textContent : '',
    state: q('.update-card__state').textContent,
    restart: shown(q('.update-card__restart-btn')),
    check: shown(q('.update-card__check-btn')),
    progress: shown(q('.update-card__progress-bar')) ? q('.update-card__progress-fill').style.width : '',
    log: shown(q('.update-card__log')) ? q('.update-card__log').textContent : '',
  };
}"""

_FOUND = ("update-available", {"version": "2.0.1"})
_PROGRESS = ("download-progress", {"percent": 40})
_DOWNLOADED = ("update-downloaded", {"version": "2.0.1"})
_ERROR = ("error", {"message": "getaddrinfo ENOTFOUND github.com"})


def _open(page, url):
    """Load `url` and wait until the update card has rendered the replayed state."""
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.update-card__state').textContent !== ''")
    return page.evaluate(_VISIBLE_STATE_JS)


def test_bell_badge_counts_downloaded_update(page, ui_server):
    """A downloaded update must light the bell badge, not just the panel card."""
    _fake_electron(page)
    assert _open(page, f"{ui_server}/")["badge"] == ""
    page.evaluate("() => window.__updaterEmit('update-downloaded', { version: '2.0.1' })")
    assert page.evaluate(_VISIBLE_STATE_JS)["badge"] == "1"


def test_downloaded_state_replays_on_page_load(page, ui_server):
    """An update downloaded before this page loaded shows as ready to install."""
    _fake_electron(page, [_FOUND, _PROGRESS, _DOWNLOADED])
    seen = _open(page, f"{ui_server}/")
    assert seen["badge"] == "1"
    assert "2.0.1" in seen["state"]
    assert seen["restart"] is True
    assert seen["check"] is False
    assert seen["progress"] == "100%"


def test_downloading_state_replays_on_page_load(page, ui_server):
    """A download in progress before this page loaded shows with its percent."""
    _fake_electron(page, [_FOUND, _PROGRESS])
    seen = _open(page, f"{ui_server}/")
    assert seen["badge"] == "1"
    assert "40" in seen["state"]
    assert seen["progress"] == "40%"
    assert seen["restart"] is False
    assert seen["check"] is False


def test_error_state_replays_on_page_load(page, ui_server):
    """A failed check before this page loaded still shows the failure and its reason."""
    _fake_electron(page, [_ERROR])
    seen = _open(page, f"{ui_server}/")
    assert seen["state"] == "Update check failed"
    assert "getaddrinfo ENOTFOUND github.com" in page.locator(".update-card__log").text_content()
    assert seen["check"] is True
    assert seen["restart"] is False
    assert seen["badge"] == ""


def test_update_found_lights_bell_live(page, ui_server):
    """The bell lights as soon as an update is found, before the download ends."""
    _fake_electron(page)
    assert _open(page, f"{ui_server}/")["badge"] == ""
    page.evaluate("() => window.__updaterEmit('update-available', { version: '2.0.1' })")
    seen = page.evaluate(_VISIBLE_STATE_JS)
    assert seen["badge"] == "1"
    assert "2.0.1" in seen["state"]
    assert seen["check"] is False


def test_downloaded_survives_later_noise_across_reload(page, ui_server):
    """Later re-checks and failures cannot take a downloaded update away."""
    _fake_electron(page, [_FOUND, _DOWNLOADED])
    ready = _open(page, f"{ui_server}/")
    assert ready["restart"] is True and ready["badge"] == "1"
    page.evaluate("""() => {
      window.__updaterEmit('update-not-available', {});
      window.__updaterEmit('download-progress', { percent: 5 });
      window.__updaterEmit('error', { message: 'net::ERR_INTERNET_DISCONNECTED' });
    }""")
    after = page.evaluate(_VISIBLE_STATE_JS)
    # The failure is logged, and nothing else on the card changes.
    assert after == {**ready, "log": ready["log"] + "\nUpdate error: net::ERR_INTERNET_DISCONNECTED"}
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.update-card__state').textContent !== ''")
    assert page.evaluate(_VISIBLE_STATE_JS) == after


def test_recheck_after_downloaded_keeps_the_card(page, ui_server):
    """A later re-check that finds the same update leaves the ready card and its
    log as they were, live and after a reload."""
    _fake_electron(page, [("checking-for-update", None), _FOUND, _DOWNLOADED])
    ready = _open(page, f"{ui_server}/")
    assert ready["log"].split("\n")[-1] == "v2.0.1 ready. Click 'Restart to Install'"
    for name, payload in [("checking-for-update", None), _FOUND, _DOWNLOADED]:
        page.evaluate(f"() => window.__updaterEmit({json.dumps(name)}, {json.dumps(payload)})")
    assert page.evaluate(_VISIBLE_STATE_JS) == ready
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.update-card__state').textContent !== ''")
    assert page.evaluate(_VISIBLE_STATE_JS) == ready


@pytest.mark.parametrize("events", [
    [_FOUND],
    [_FOUND, _PROGRESS],
    [_FOUND, _PROGRESS, _DOWNLOADED],
    [_ERROR],
    [_FOUND, _PROGRESS, _ERROR],
], ids=["found", "downloading", "downloaded", "check-error", "download-error"])
def test_reload_and_navigation_preserve_visible_state(page, ui_server, events):
    """What the live events drew is exactly what a reload or another page shows."""
    _fake_electron(page)
    _open(page, f"{ui_server}/")
    for name, payload in events:
        page.evaluate(f"() => window.__updaterEmit({json.dumps(name)}, {json.dumps(payload)})")
    live = page.evaluate(_VISIBLE_STATE_JS)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.update-card__state').textContent !== ''")
    assert page.evaluate(_VISIBLE_STATE_JS) == live
    assert _open(page, f"{ui_server}/settings") == live


def test_not_available_restores_check_button(page, ui_server):
    """A check that finds nothing says so and offers the check button again."""
    _fake_electron(page)
    _open(page, f"{ui_server}/")
    page.evaluate("() => document.querySelector('.update-card__check-btn').click()")
    assert page.evaluate(_VISIBLE_STATE_JS)["check"] is False
    page.evaluate("() => window.__updaterEmit('update-not-available', {})")
    seen = page.evaluate(_VISIBLE_STATE_JS)
    assert seen["check"] is True
    assert seen["state"] == "Up to date"
    assert seen["badge"] == ""


def test_update_found_without_version_lights_bell(page, ui_server):
    """An update found with no version number still lights the bell."""
    _fake_electron(page)
    _open(page, f"{ui_server}/")
    page.evaluate("() => window.__updaterEmit('update-available', {})")
    seen = page.evaluate(_VISIBLE_STATE_JS)
    assert seen["badge"] == "1"
    assert seen["check"] is False


@pytest.mark.parametrize("events, state", [
    (["update-available"], "Downloading update..."),
    (["update-available", "update-downloaded"], "Update ready to install"),
])
def test_update_without_version_reads_plainly(page, ui_server, events, state):
    """An update with no version number is named plainly, never as "vupdate"."""
    _fake_electron(page)
    _open(page, f"{ui_server}/")
    for event in events:
        page.evaluate(f"() => window.__updaterEmit('{event}', {{}})")
    assert page.evaluate(_VISIBLE_STATE_JS)["state"] == state


def test_back_to_cached_page_shows_current_state(page, ui_server):
    """Going Back to a page htmx restores from its history cache shows the
    updater as it is now, and the card keeps following later events."""
    _fake_electron(page, [_FOUND, _PROGRESS])
    _open(page, f"{ui_server}/inventory")
    page.fill("#search-input", "zzzz")
    page.wait_for_function("() => location.search.indexOf('q=zzzz') !== -1")
    page.evaluate("() => window.__updaterEmit('update-downloaded', { version: '2.0.1' })")
    ready = page.evaluate(_VISIBLE_STATE_JS)
    assert ready["restart"] is True
    page.go_back()
    page.wait_for_function("() => location.search.indexOf('q=zzzz') === -1")
    page.wait_for_function("() => document.querySelector('.update-card__restart-btn').style.display !== 'none'")
    assert page.evaluate(_VISIBLE_STATE_JS) == ready


def test_card_follows_live_events_after_back_to_cached_page(page, ui_server):
    """After Back restores a cached page, later updater events still reach its card."""
    _fake_electron(page, [_FOUND])
    _open(page, f"{ui_server}/inventory")
    page.fill("#search-input", "zzzz")
    page.wait_for_function("() => location.search.indexOf('q=zzzz') !== -1")
    page.go_back()
    page.wait_for_function("() => location.search.indexOf('q=zzzz') === -1")
    page.evaluate("() => window.__updaterEmit('download-progress', { percent: 70 })")
    page.wait_for_function("() => document.querySelector('.update-card__state').textContent.indexOf('70') !== -1")
    assert page.evaluate(_VISIBLE_STATE_JS)["progress"] == "70%"


def test_back_to_cached_page_does_not_repeat_the_error_log(page, ui_server):
    """Going Back after a failed check shows the failure once, as it was shown live."""
    _fake_electron(page)
    _open(page, f"{ui_server}/inventory")
    page.evaluate("() => window.__updaterEmit('error', { message: 'getaddrinfo ENOTFOUND github.com' })")
    failed = page.evaluate(_VISIBLE_STATE_JS)
    assert failed["log"].count("getaddrinfo ENOTFOUND github.com") == 1
    page.fill("#search-input", "zzzz")
    page.wait_for_function("() => location.search.indexOf('q=zzzz') !== -1")
    page.go_back()
    page.wait_for_function("() => location.search.indexOf('q=zzzz') === -1")
    page.go_forward()
    page.wait_for_function("() => location.search.indexOf('q=zzzz') !== -1")
    page.go_back()
    page.wait_for_function("() => location.search.indexOf('q=zzzz') === -1")
    assert page.evaluate(_VISIBLE_STATE_JS) == failed


def test_update_log_lines_survive_reload(page, ui_server):
    """The update log, including a failure after the download finished, shows the
    same lines after a reload as it did live."""
    _fake_electron(page)
    _open(page, f"{ui_server}/")
    for name, payload in [("checking-for-update", None), _FOUND, _DOWNLOADED,
                          ("error", {"message": "net::ERR_INTERNET_DISCONNECTED"})]:
        page.evaluate(f"() => window.__updaterEmit({json.dumps(name)}, {json.dumps(payload)})")
    live = page.evaluate(_VISIBLE_STATE_JS)
    assert live["log"].split("\n") == [
        "Checking for update...",
        "Found v2.0.1, downloading...",
        "v2.0.1 ready. Click 'Restart to Install'",
        "Update error: net::ERR_INTERNET_DISCONNECTED",
    ]
    assert live["restart"] is True
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('.update-card__state').textContent !== ''")
    assert page.evaluate(_VISIBLE_STATE_JS) == live


@pytest.mark.parametrize("percent, bar, state", [
    (-3, "0%", "2.0.1"), ("abc", "0%", "2.0.1"), (150, "100%", "100%"),
])
def test_malformed_progress_shows_a_percent_in_range(page, ui_server, percent, bar, state):
    """A download percent outside 0 to 100, or not a number, still shows a sane bar."""
    _fake_electron(page, [_FOUND])
    _open(page, f"{ui_server}/")
    page.evaluate(f"() => window.__updaterEmit('download-progress', {{ percent: {json.dumps(percent)} }})")
    seen = page.evaluate(_VISIBLE_STATE_JS)
    assert seen["progress"] == bar
    assert state in seen["state"]
