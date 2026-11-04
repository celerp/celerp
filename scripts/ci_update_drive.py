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

Both connect to Celerp's window over the Chrome DevTools Protocol (Celerp started
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


def main(command: str, port: int, seconds: int) -> int:
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
        return {"sign-in": sign_in, "install": install}[command](browser, end)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
