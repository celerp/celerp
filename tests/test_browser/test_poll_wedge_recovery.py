# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A wedged poll fetch must not permanently kill the poller.

The single-flight guard collapses concurrent polls to one in-flight request, but a
fetch that wedges (a half-open socket that never settles) then leaves inFlight true
forever, so every later interval tick is skipped and the poller stops polling until a
page reload. Each fetch must instead carry an abort deadline: a request that has not
settled within it is aborted, flows through the error/backoff branch, and the next
tick starts a fresh request. This drives the shared production poller (window.celerpPoll)
directly against a probe URL that never responds and asserts it keeps polling.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def test_poll_recovers_from_wedged_fetch(page, ui_server):
    """With the probe endpoint held open (every response wedged), a poller that bounds
    each fetch with an abort deadline keeps issuing requests across the window. A poller
    with no deadline issues exactly one request and then wedges forever on the first
    unsettled fetch."""
    requests: list[str] = []
    page.on("request",
            lambda req: requests.append(req.url) if "/poll-probe" in req.url else None)

    # Hold every probe request open (never fulfilled) so each fetch wedges; the fix must
    # abort each at its deadline and try again.
    held: list = []
    page.route("**/poll-probe", lambda route: held.append(route))

    page.goto(f"{ui_server}/", wait_until="domcontentloaded")

    # Start the shared production poller with a short interval and a short fetch deadline
    # against the wedged probe URL. celerpPoll is the exact function the backup banner
    # uses; interval and fetchDeadline are its public knobs.
    page.evaluate(
        "window.celerpPoll('probe', '/poll-probe', function(){}, "
        "{ interval: 150, fetchDeadline: 120 }).start();")

    # Let many shortened intervals elapse while every request wedges.
    page.wait_for_timeout(2000)

    issued = len(requests)
    # Release the held routes so teardown does not block on outstanding requests.
    page.unroute("**/poll-probe")
    for route in held:
        try:
            route.abort()
        except Exception:
            pass

    assert issued >= 2, (
        "a wedged poll fetch must be aborted at its deadline so the poller keeps polling; "
        f"only {issued} request(s) issued across a 2s window, so the first hung fetch left "
        "the poller wedged")
