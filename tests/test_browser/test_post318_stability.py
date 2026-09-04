# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Post-318 stability regressions, asserted as observable browser behavior.

Two independent defects on the shipped-broken baseline:

TEST 1 (P0#2): the list-detail lines section declares bare `const`/`let`
globals in an inline script. HTMX outerHTML-swaps that whole section when the
pager is used, re-evaluating the inline script; the second evaluation
re-declares the same `const`/`let` and throws a SyntaxError, which kills the
follow-on pager so a second page navigation no longer changes the page.

TEST 2 (P1#8): the shared `celerpPoll` fire() calls `r.json()` with no `r.ok`
check and zeroes the error counters on any parsed body, so a poll endpoint that
answers with a non-OK status is treated as success and the client never backs
off - it keeps polling at the base interval instead of slowing down.
"""
from __future__ import annotations

import json
import time

import pytest

pytestmark = pytest.mark.browser


# ── Seeding ────────────────────────────────────────────────────────────────

def _seed_paged_list(api, n_lines: int = 60) -> str:
    """Create a draft quotation list with enough stored lines to paginate.

    A draft quotation renders the editable line table with the pager whose page
    controls route through celerpPageNav (documents.py:586) - the HTMX
    outerHTML re-swap path the first defect lives on. Lines render from their
    stored values, so no catalog item has to exist.
    """
    line_items = [
        {"name": f"Line item {i:03d}", "description": f"row {i}",
         "quantity": 1, "unit_price": 10 + i}
        for i in range(n_lines)
    ]
    r = api.post("/lists", json={
        "list_type": "quotation",
        "status": "draft",
        "line_items": line_items,
    })
    assert r.status_code in (200, 201), f"create list failed: {r.status_code} {r.text}"
    entity_id = r.json().get("entity_id") or r.json().get("id")
    assert entity_id and str(entity_id).startswith("list:"), f"unexpected id: {r.text[:200]}"
    return entity_id


def _active_page(page) -> str:
    """The page number the pager currently marks active, inside the lines section."""
    active = page.locator("#list-line-detail .btn--active, [id^=list-line-section-] .btn--active").last
    active.wait_for(state="visible", timeout=6000)
    return active.inner_text().strip()


# ── TEST 1 ───────────────────────────────────────────────────────────────────

def test_inline_script_survives_htmx_outerhtml_reswap(page, ui_server, api):
    """Paging a draft list twice must keep working: the outerHTML re-swap of the
    lines section must not throw a redeclaration SyntaxError that kills the pager.

    Observable behavior: after the first pager navigation, a SECOND navigation
    still changes the visible active page. At HEAD the second inline-script eval
    throws `Identifier '_CELERP_EID' has already been declared` and the pager
    dies, so the active page never advances past the first swap.
    """
    entity_id = _seed_paged_list(api, n_lines=60)

    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # limit=25 over 60 lines => 3 pages, so the editable (celerpPageNav) pager renders.
    page.goto(f"{ui_server}/lists/{entity_id}?limit=25", wait_until="domcontentloaded")

    section = page.locator("[id^=list-line-section-]").first
    section.wait_for(state="visible", timeout=8000)

    # Pager present and on page 1.
    assert _active_page(page) == "1", "expected to start on page 1"

    # First navigation: page 1 -> page 2. This is the HTMX outerHTML re-swap of the
    # lines section that re-evaluates the inline script.
    page.get_by_role("button", name="2", exact=True).click()
    page.wait_for_function(
        "() => {const a=document.querySelector('[id^=list-line-section-] .btn--active');"
        " return a && a.textContent.trim() === '2';}",
        timeout=8000,
    )
    assert _active_page(page) == "2", "first navigation did not reach page 2"

    # Second navigation: page 2 -> page 3. At HEAD the pager is already dead after
    # the first re-swap threw, so the active page stays on 2.
    page.get_by_role("button", name="3", exact=True).click()
    page.wait_for_function(
        "() => {const a=document.querySelector('[id^=list-line-section-] .btn--active');"
        " return a && a.textContent.trim() === '3';}",
        timeout=8000,
    )
    reached = _active_page(page)

    redeclare = [
        m for m in (console_errors + page_errors)
        if "already been declared" in m or ("SyntaxError" in m and "_CELERP" in m)
        or "Identifier '_CELERP" in m or "redeclaration" in m.lower()
    ]
    assert not redeclare, f"inline-script redeclaration SyntaxError fired: {redeclare}"
    assert reached == "3", (
        f"second pager navigation did not change the page (stuck on {reached}); "
        "the re-swapped inline script threw and killed the pager"
    )


# ── TEST 2 ───────────────────────────────────────────────────────────────────

def test_poll_backoff_not_reset_by_non_ok_response(page, ui_server):
    """A sustained non-OK poll response must make celerpPoll back off (grow the
    gap between requests), not keep firing at the base interval.

    Exercises the real shared client poller (window.celerpPoll from shell.py) in
    isolation: the poll URL is forced to answer 503 for the whole test, request
    timestamps are recorded, and the cadence is measured. At HEAD fire() does
    r.json() with no r.ok check and zeroes errorCount/skipTicks on any parsed
    body, so every 503 is treated as success and the client never slows down.
    """
    poll_url_marker = "/backup/active"

    # Force the poll endpoint to a non-OK status with a small JSON body for the
    # whole test. Both the banner poller and our probe poller hit this route.
    def _handle(route):
        route.fulfill(status=503, content_type="application/json",
                      body=json.dumps({"state": "error"}))
    page.route(f"**{poll_url_marker}**", _handle)

    request_times: list[float] = []
    inflight_overlap = {"count": 0, "open": 0}

    def _on_request(req):
        if poll_url_marker in req.url and "probe=post318" in req.url:
            request_times.append(time.monotonic())
            if inflight_overlap["open"] > 0:
                inflight_overlap["count"] += 1
            inflight_overlap["open"] += 1

    def _on_response(resp):
        if poll_url_marker in resp.url and "probe=post318" in resp.url:
            if inflight_overlap["open"] > 0:
                inflight_overlap["open"] -= 1

    page.on("request", _on_request)
    page.on("response", _on_response)

    # Any shelled authed page defines window.celerpPoll.
    page.goto(f"{ui_server}/lists", wait_until="domcontentloaded")
    page.wait_for_function("() => typeof window.celerpPoll === 'function'", timeout=8000)

    # Drive the real client poller against a probe URL (routed to 503 above) at a
    # short interval so a modest window covers several ticks. A backing-off client
    # spaces requests out under sustained errors; the broken client does not.
    interval_ms = 250
    page.evaluate(
        "(ms) => { window.__p318 = window.celerpPoll("
        "'post318', '/backup/active?probe=post318', function(){}, {interval: ms});"
        " window.__p318.start(); }",
        interval_ms,
    )

    # Observe for a fixed window of several base intervals.
    window_ticks = 40
    page.wait_for_timeout(interval_ms * window_ticks)
    page.evaluate("() => { if (window.__p318 && window.__p318.stop) window.__p318.stop(); }")

    n = len(request_times)
    assert n >= 3, f"probe poller never fired enough to measure (got {n} requests)"

    # A non-backing-off client fires ~ once per tick: ~ window_ticks requests over
    # the window. A backing-off client (skipTicks grows 1,2,3,... capped at 8)
    # fires far fewer. With backoff the count over `window_ticks` ticks is bounded
    # well under half the ticks; without it the count tracks the tick count.
    #   backing off: fires at ticks 0,2,5,9,14,20,27,35,... -> ~8-9 over 40 ticks.
    #   broken (base interval): ~1 per tick -> ~35-40 over 40 ticks.
    max_if_backing_off = window_ticks // 2  # 20; genuine backoff stays well below
    assert n <= max_if_backing_off, (
        f"poll cadence did not back off under sustained non-OK responses: "
        f"{n} requests over {window_ticks} base intervals (a backing-off client "
        f"would fire well under {max_if_backing_off})"
    )

    # The gap between consecutive polls must be growing, not flat at the base
    # interval - the direct signature of backoff.
    gaps = [request_times[i + 1] - request_times[i] for i in range(len(request_times) - 1)]
    if len(gaps) >= 4:
        first_two = sum(gaps[:2]) / 2
        last_two = sum(gaps[-2:]) / 2
        assert last_two > first_two * 1.5, (
            f"poll gaps did not grow (first~{first_two:.3f}s, last~{last_two:.3f}s); "
            "the client is not backing off under non-OK responses"
        )

    # Single-flight guard (green at HEAD, must stay green): no two probe polls
    # overlap in flight.
    assert inflight_overlap["count"] == 0, (
        f"single-flight violated: {inflight_overlap['count']} overlapping in-flight polls"
    )
