# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 3: 404 handling — nonexistent entity IDs return graceful error, not 500."""
import pytest

pytestmark = pytest.mark.browser

_MISSING_ROUTES = [
    "/inventory/item:nonexistent-abc-def",
    "/docs/doc:nonexistent-abc-def",
    "/crm/contact:nonexistent-abc-def",
    "/lists/list:nonexistent-abc-def",
    "/manufacturing/mfg-order:nonexistent",
    "/manufacturing/boms/bom:nonexistent",
    "/subscriptions/sub:nonexistent",
    "/share/tok_nonexistent_abc_def",
]


@pytest.mark.parametrize("route", _MISSING_ROUTES)
def test_missing_entity_is_graceful(page, ui_server, route):
    """404-01..08: Missing entity IDs → graceful 404 or error message, NOT 500."""
    resp = page.goto(f"{ui_server}{route}", wait_until="domcontentloaded")
    # Must not be 500
    assert resp.status != 500, f"{route} returned HTTP 500"
    # Must not show Python traceback
    body = page.locator("body").inner_text()
    assert "Traceback (most recent call last)" not in body, f"{route} shows traceback"
    assert "Internal Server Error" not in body, f"{route} shows Internal Server Error"
    # Must either be a 404 response OR show a "not found" / "error" message in the UI
    is_graceful = (
        resp.status in {200, 404}
        or "not found" in body.lower()
        or "404" in body
        or "does not exist" in body.lower()
        or "could not be found" in body.lower()
    )
    assert is_graceful, (
        f"{route} returned {resp.status} without graceful error message. Body: {body[:200]}"
    )


def test_ui_404_shows_friendly_page(page, ui_server):
    """404-09: Completely unknown route → friendly 404 page, no traceback."""
    resp = page.goto(f"{ui_server}/this-route-does-not-exist", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Traceback" not in body, "404 page exposes Python traceback"
    assert resp.status != 500, "404 route returned HTTP 500"
    # Must show something friendly
    assert (
        "not found" in body.lower()
        or "404" in body
        or "does not exist" in body.lower()
        or "page" in body.lower()
    ), f"No friendly message on 404 page. Body: {body[:300]}"
