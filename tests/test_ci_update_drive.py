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
    """The refusal screen: Update Celerp checks; each poll moves the updater on one state."""

    url = "file:///C:/Program%20Files/Celerp/resources/app.asar/assets/loading.html"

    def __init__(self, states):
        self.states = list(states)
        self.state = {"status": "idle"}
        self.clicks = []

    def evaluate(self, js):
        if "hidden" in js:
            return True
        if self.clicks and self.states:
            self.state = self.states.pop(0)
        return self.state

    def inner_text(self, selector):
        if selector == "#refusal-message":
            return "Your data was last opened with Celerp 2.8.2"
        return "Restart to install" if self.state["status"] == "downloaded" else "Update Celerp"

    def is_enabled(self, _selector):
        return True

    def click(self, selector):
        self.clicks.append((selector, self.inner_text(selector)))


def _browser(*pages):
    return types.SimpleNamespace(contexts=[types.SimpleNamespace(pages=list(pages))])


def test_update_celerp_then_restart_to_install(drive):
    screen = _Screen([{"status": "downloading", "percent": 50},
                      {"status": "downloaded", "version": "2.8.2"}])
    ui = types.SimpleNamespace(url="http://127.0.0.1:5000/")
    assert drive.refusal(_browser(ui, screen), drive.time.monotonic() + 60) == 0
    assert screen.clicks == [("#refusal-update", "Update Celerp"), ("#refusal-update", "Restart to install")]


def test_a_failed_download_fails_the_check(drive, capsys):
    screen = _Screen([{"status": "error", "message": "net::ERR"}])
    assert drive.refusal(_browser(screen), drive.time.monotonic() + 60) == 1
    assert screen.clicks == [("#refusal-update", "Update Celerp")]
    assert "the refusal screen was shown; the update was not downloaded" in capsys.readouterr().out


def test_no_refusal_screen_fails_the_check_in_time(drive, capsys):
    ui = types.SimpleNamespace(url="http://127.0.0.1:5000/")
    assert drive.refusal(_browser(ui), drive.time.monotonic() + 0.05) == 1
    assert "the refusal screen was not shown" in capsys.readouterr().out


def test_the_refusal_command_reaches_the_refusal_screen(drive, monkeypatch):
    screen = _Screen([{"status": "downloaded", "version": "2.8.2"}])
    chromium = types.SimpleNamespace(connect_over_cdp=lambda _url: _browser(screen))
    playwright = types.SimpleNamespace(chromium=chromium)
    monkeypatch.setattr(drive, "sync_playwright", lambda: contextlib.nullcontext(playwright))
    assert drive.main("refusal", 9222, 60) == 0
    assert screen.clicks[-1] == ("#refusal-update", "Restart to install")
