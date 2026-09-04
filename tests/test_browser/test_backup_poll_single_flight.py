# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The backup-banner poll must be single-flight.

The banner polled /backup/active on a fixed interval with no guard, so when a
response was slow the next interval fired anyway and requests stacked up. The
poll must instead collapse concurrent polls to one in-flight request: while a
poll is outstanding, a new interval tick must not start a second fetch.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def test_poll_single_flight_backup_banner(page, ui_server):
    """With /backup/active held open (the response never arrives), a single-flight
    poller issues exactly one request across many interval ticks. A poller with no
    single-flight guard fires one request per tick, stacking overlapping fetches."""
    requests: list[str] = []
    page.on("request",
            lambda req: requests.append(req.url) if "/backup/active" in req.url else None)

    # Speed the poll cadence so many ticks elapse within the test window: shrink the
    # banner poll interval before the page's own script installs it.
    page.add_init_script(
        "window.setInterval = (function(orig){"
        "  return function(fn, ms){ return orig(fn, ms > 300 ? 200 : ms); };"
        "})(window.setInterval);"
    )
    # Hold every backup poll open (never fulfilled) so the first in-flight request
    # stays outstanding across the whole window; the count is read before teardown.
    held: list = []
    page.route("**/backup/active", lambda route: held.append(route))

    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    # Let many shortened intervals elapse while the first request is still open.
    page.wait_for_timeout(3000)

    issued = len(requests)
    # Release the held routes so teardown does not block on outstanding requests.
    page.unroute("**/backup/active")
    for route in held:
        try:
            route.abort()
        except Exception:
            pass

    assert issued == 1, (
        "the backup poll must be single-flight: exactly one request may be in flight "
        f"across many interval ticks, but {issued} were issued")
