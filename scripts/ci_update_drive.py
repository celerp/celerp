# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Starts the in-app update of a running packaged Celerp, for the release checks.

    python scripts/ci_update_drive.py <debugging port> <seconds>

Connects to Celerp's window over the Chrome DevTools Protocol (Celerp started with
--remote-debugging-port), waits until the updater has downloaded the update, and
sends the same install-update message Restart to install sends. Exits 1, printing
the update state, if the download fails or does not finish in time.
"""

import sys
import time

from playwright.sync_api import Error, sync_playwright


def main(port: int, seconds: int) -> int:
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
        state = None
        while time.monotonic() < end:
            pages = [p for c in browser.contexts for p in c.pages if p.url.startswith("http://127.0.0.1")]
            try:
                state = pages[0].evaluate("window.celerp.getUpdateState()") if pages else None
            except Error:
                state = None   # the window is between pages
            if state and state["status"] == "downloaded":
                pages[0].evaluate("window.celerp.installUpdate()")
                print(f"install-update sent for {state['version']}")
                return 0
            if state and state["status"] == "error":
                break
            time.sleep(2)
        print(f"the update was not downloaded: {state}")
        return 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]), int(sys.argv[2])))
