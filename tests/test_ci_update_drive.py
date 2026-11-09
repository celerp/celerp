# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The release check's refusal driver, against a stand-in refusal screen."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "ci_update_drive.py"


class _Error(Exception):
    pass


@pytest.fixture()
def drive(monkeypatch):
    # The driver imports Playwright at load; the screen below stands in for its browser.
    api = types.ModuleType("playwright.sync_api")
    api.Error = _Error
    api.sync_playwright = None
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    spec = importlib.util.spec_from_file_location("ci_update_drive", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    return module


class _Screen:
    """The refusal screen: Update Celerp checks; each poll moves the updater on one state.

    The screen can lag the updater as the real one does: the refusal stays hidden for the
    first `hidden` looks, and once the update is downloaded the button keeps its old label
    for `lag` polls, then shows Restart to install disabled for one more poll.
    """

    url = "file:///C:/Program%20Files/Celerp/resources/app.asar/assets/loading.html"

    def __init__(self, states, hidden=0, lag=0):
        self.states = list(states)
        self.state = {"status": "idle"}
        self.clicks = []
        self.hidden = hidden
        self.looks = 0
        self.lag = lag
        self.since = 0   # polls since the state last changed

    def wait_for_timeout(self, _ms):
        pass   # the driver's pause between polls

    def evaluate(self, js):
        if "hidden" in js:
            self.looks += 1
            return self.looks > self.hidden
        assert self.state["status"] != "error", "the driver polled on after a failed update"
        if self.clicks and self.states:
            self.state, self.since = self.states.pop(0), 0
        else:
            self.since += 1
        return self.state

    def _downloaded(self):
        return self.state["status"] == "downloaded" and self.since >= self.lag

    def inner_text(self, selector):
        if selector == "#refusal-message":
            return "Your data was last opened with Celerp 2.8.2"
        return "Restart to install" if self._downloaded() else "Update Celerp"

    def is_enabled(self, _selector):
        return not (self._downloaded() and self.since == self.lag and self.lag > 0)

    def click(self, selector):
        assert self.looks > self.hidden, "clicked while the refusal screen was hidden"
        assert self.is_enabled(selector), "clicked a disabled button"
        self.clicks.append((selector, self.inner_text(selector)))


def _browser(*pages):
    return types.SimpleNamespace(contexts=[types.SimpleNamespace(pages=list(pages))])


def test_update_celerp_then_restart_to_install(drive):
    screen = _Screen([{"status": "downloading", "percent": 50},
                      {"status": "downloaded", "version": "2.8.2"}])
    ui = types.SimpleNamespace(url="http://127.0.0.1:5000/", wait_for_timeout=lambda _ms: None)
    assert drive.refusal(_browser(ui, screen), drive.time.monotonic() + 60) == 0
    assert screen.clicks == [("#refusal-update", "Update Celerp"), ("#refusal-update", "Restart to install")]


def test_a_failed_download_fails_the_check(drive, capsys):
    screen = _Screen([{"status": "error", "message": "net::ERR"}])
    assert drive.refusal(_browser(screen), drive.time.monotonic() + 60) == 1
    assert screen.clicks == [("#refusal-update", "Update Celerp")]
    assert "the refusal screen was shown; the update was not downloaded" in capsys.readouterr().out


def test_the_driver_waits_for_the_screen_to_catch_up(drive):
    screen = _Screen([{"status": "downloaded", "version": "2.8.2"}], hidden=2, lag=2)
    assert drive.refusal(_browser(screen), drive.time.monotonic() + 60) == 0
    assert screen.clicks == [("#refusal-update", "Update Celerp"), ("#refusal-update", "Restart to install")]


def test_no_refusal_screen_fails_the_check_in_time(drive, capsys):
    ui = types.SimpleNamespace(url="http://127.0.0.1:5000/", wait_for_timeout=lambda _ms: None)
    assert drive.refusal(_browser(ui), drive.time.monotonic() + 0.05) == 1
    assert "the refusal screen was not shown" in capsys.readouterr().out


def test_the_refusal_command_reaches_the_refusal_screen(drive, monkeypatch):
    screen = _Screen([{"status": "downloaded", "version": "2.8.2"}])
    chromium = types.SimpleNamespace(connect_over_cdp=lambda _url: _browser(screen))
    playwright = types.SimpleNamespace(chromium=chromium)
    monkeypatch.setattr(drive, "sync_playwright", lambda: contextlib.nullcontext(playwright))
    assert drive.main("refusal", 9222, 60) == 0
    assert screen.clicks[-1] == ("#refusal-update", "Restart to install")


class _Context:
    """A browser context whose window opens a page only while the driver waits on it."""

    def __init__(self, opens=None, timeouts=0):
        self.pages = []
        self.opens = opens
        self.timeouts = timeouts   # waits that end with no page, before the page opens
        self.waits = []

    def wait_for_event(self, event, timeout):
        self.waits.append((event, timeout))
        if self.timeouts:
            self.timeouts -= 1
            raise _Error("Timeout 2000ms exceeded while waiting for event \"page\"")
        self.pages.append(self.opens)
        return self.opens


def test_a_window_that_opens_later_is_seen_through_playwright(drive, monkeypatch):
    slept = []
    monkeypatch.setattr(drive.time, "sleep", slept.append)
    screen = _Screen([{"status": "downloaded", "version": "2.8.2"}])
    context = _Context(opens=screen, timeouts=2)
    browser = types.SimpleNamespace(contexts=[context])
    assert drive.refusal(browser, drive.time.monotonic() + 60) == 0
    assert context.waits == [("page", 2000)] * 3
    assert slept == []   # every pause went through Playwright
    assert screen.clicks[-1] == ("#refusal-update", "Restart to install")


def test_a_page_that_closes_during_the_pause_is_just_another_poll(drive):
    screen = _Screen([{"status": "downloaded", "version": "2.8.2"}], hidden=1)
    closes = [True]

    def wait_for_timeout(_ms):
        if closes:
            closes.pop()
            raise _Error("Target page, context or browser has been closed")

    screen.wait_for_timeout = wait_for_timeout
    assert drive.refusal(_browser(screen), drive.time.monotonic() + 60) == 0
    assert closes == []


def test_with_no_context_the_pause_sleeps(drive, monkeypatch):
    slept = []
    monkeypatch.setattr(drive.time, "sleep", slept.append)
    drive.pause(types.SimpleNamespace(contexts=[]))
    assert slept == [2]


def test_refused_reads_the_refusal_screen_and_selects_nothing(drive, monkeypatch, capsys):
    screen = _Screen([], hidden=2)
    chromium = types.SimpleNamespace(connect_over_cdp=lambda _url: _browser(screen))
    monkeypatch.setattr(drive, "sync_playwright", lambda: contextlib.nullcontext(types.SimpleNamespace(chromium=chromium)))
    assert drive.main("refused", 9222, 60, "Your data was last opened with Celerp 2.8.2") == 0
    assert screen.clicks == []
    assert "the refusal screen says 'Your data was last opened with Celerp 2.8.2'" in capsys.readouterr().out


def test_refused_fails_on_another_message_or_no_screen(drive, capsys):
    screen = _Screen([])
    assert drive.refused(_browser(screen), drive.time.monotonic() + 0.05, "Your data was last opened with Celerp 2.8.3") == 1
    assert "says 'Your data was last opened with Celerp 2.8.2' (want 'Your data was last opened with Celerp 2.8.3')" \
        in capsys.readouterr().out
    ui = types.SimpleNamespace(url="http://127.0.0.1:5000/", wait_for_timeout=lambda _ms: None)
    assert drive.refused(_browser(ui), drive.time.monotonic() + 0.05, "anything") == 1
    assert "the refusal screen was not shown" in capsys.readouterr().out
