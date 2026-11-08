# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Drives a running packaged Celerp for the release checks of the in-app update.

    python scripts/ci_update_drive.py sign-in <debugging port> <seconds>
        On fresh data, sets up the company on the setup page as a new owner does,
        which signs the owner in. Celerp keeps the sign-in after a restart, so a
        Celerp reopened by setup shows its update card. Exits 1 if no page is
        signed in within the time.
    python scripts/ci_update_drive.py install <debugging port> <seconds>
        Waits until the updater has downloaded the update and sends the same
        install-update message Restart to install sends. Exits 1, printing the
        update state, if the download fails or does not finish in time.
    python scripts/ci_update_drive.py refusal <debugging port> <seconds>
        On the screen a Celerp shows for data a newer Celerp has opened, selects
        Update Celerp, waits until the update has downloaded and the button says
        Restart to install, and selects it. Exits 1, printing the update state,
        if the screen does not show, or the download fails or does not finish in
        time.
    python scripts/ci_update_drive.py card <debugging port> <seconds> <text>
        Waits until a signed-in page's update card says <text>. Exits 1, printing
        what the card says, if it does not within the time.

All connect to Celerp's window over the Chrome DevTools Protocol (Celerp started
with --remote-debugging-port).
"""

import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import Error, sync_playwright

# The setup form as a new owner fills it in. The blank business type turns on no
# module, so setup needs no restart.
OWNER = {
    "company_name": "CI Company",
    "name": "CI Owner",
    "email": "owner@example.com",
    "password": "ci-update-check",
    "confirm_password": "ci-update-check",
    "currency": "USD",
    "vertical": "blank",
    "country": "",
    "timezone": "UTC",
}


def celerp_page(browser):
    """Celerp's own page in its window, or None while the window is between pages."""
    pages = [p for c in browser.contexts for p in c.pages if p.url.startswith("http://127.0.0.1")]
    return pages[0] if pages else None


def sign_in(browser, end: float) -> int:
    sent = False
    path = None
    while time.monotonic() < end:
        page = celerp_page(browser)
        path = urlparse(page.url).path if page else None
        try:
            if path and not path.startswith(("/setup", "/login")) and page.query_selector(".topbar"):
                print(f"signed in, on {path}")
                return 0
            if path == "/setup" and not sent:
                answer = page.evaluate(
                    "async (owner) => { const r = await fetch('/setup', {method: 'POST',"
                    " body: new URLSearchParams(owner), credentials: 'same-origin'});"
                    " return r.status + ' ' + r.url; }", OWNER)
                print(f"setup sent: {answer}")
                sent = True
                page.goto(page.url.split("/setup")[0] + "/")
        except Error:
            pass   # the window is between pages
        time.sleep(2)
    print(f"no signed-in page in time (last page {path}, setup sent: {sent})")
    return 1


def install(browser, end: float) -> int:
    state = None
    while time.monotonic() < end:
        page = celerp_page(browser)
        try:
            state = page.evaluate("window.celerp.getUpdateState()") if page else None
        except Error:
            state = None   # the window is between pages
        if state and state["status"] == "downloaded":
            page.evaluate("window.celerp.installUpdate()")
            print(f"install-update sent for {state['version']}")
            return 0
        if state and state["status"] == "error":
            break
        time.sleep(2)
    print(f"the update was not downloaded: {state}")
    return 1


def refusal_page(browser):
    """The window's refusal screen (the bundled loading page), or None."""
    pages = [p for c in browser.contexts for p in c.pages
             if p.url.startswith("file:") and p.url.endswith("/loading.html")]
    return pages[0] if pages else None


def refusal(browser, end: float) -> int:
    asked = None   # the button's label when Update Celerp was selected
    state = None
    while time.monotonic() < end:
        page = refusal_page(browser)
        try:
            if page and asked is None and page.evaluate("!document.getElementById('refusal').hidden"):
                asked = page.inner_text("#refusal-update")
                page.click("#refusal-update")
                print(f"selected '{asked}' on: {page.inner_text('#refusal-message')}")
            if page and asked is not None:
                state = page.evaluate("window.celerp.getUpdateState()")
                label = page.inner_text("#refusal-update")
                if state["status"] == "downloaded" and label != asked and page.is_enabled("#refusal-update"):
                    page.click("#refusal-update")
                    print(f"selected '{label}' for {state['version']}")
                    return 0
                if state["status"] == "error":
                    break
        except Error:
            pass   # the window is between pages
        time.sleep(2)
    shown = "shown" if asked is not None else "not shown"
    print(f"the refusal screen was {shown}; the update was not downloaded: {state}")
    return 1


def card(browser, end: float, text: str) -> int:
    said = None
    while time.monotonic() < end:
        page = celerp_page(browser)
        try:
            said = page.evaluate(
                "(document.querySelector('.update-card__state') || {}).textContent") if page else None
        except Error:
            said = None   # the window is between pages
        if said == text:
            print(f"the update card says '{said}'")
            return 0
        time.sleep(2)
    print(f"the update card says '{said}' (want '{text}')")
    return 1


def main(command: str, port: int, seconds: int, *args: str) -> int:
    end = time.monotonic() + seconds
    with sync_playwright() as pw:
        while True:
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                break
            except Error:
                if time.monotonic() > end:
                    print(f"no Celerp window on port {port} in {seconds} s")
                    return 1
                time.sleep(2)
        return {"sign-in": sign_in, "install": install, "refusal": refusal, "card": card}[command](browser, end, *args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), *sys.argv[4:]))
