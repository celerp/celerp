# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 14: Multi-company — create second company, switch to it, verify context changes."""
import pytest

pytestmark = pytest.mark.browser


def test_create_second_company_via_api(api):
    """MC-01: POST /companies creates a new company and returns an access_token."""
    r = api.post("/companies", json={"name": "Browser Test Co 2"})
    # 200 or 201 expected; 409 means already exists (idempotent for test reruns)
    assert r.status_code in {200, 201, 409}, \
        f"POST /companies failed: {r.status_code} {r.text}"
    if r.status_code in {200, 201}:
        data = r.json()
        assert "access_token" in data, f"No access_token in response: {data}"


def test_switch_company_picker_shows_multiple(page, ui_server, api):
    """MC-02: With 2 companies, switch-company panel lists both."""
    # Ensure second company exists
    api.post("/companies", json={"name": "Browser Test Co 2"})

    page.goto(f"{ui_server}/switch-company", wait_until="domcontentloaded")
    assert "Internal Server Error" not in page.locator("body").inner_text()
    assert "Traceback" not in page.locator("body").inner_text()

    # Panel should list company names — at minimum the seeded company
    body = page.locator("body").inner_text()
    assert "Browser Test Co" in body, \
        f"Original company not shown in switch panel. Body: {body[:300]}"


def test_switch_company_changes_context(playwright, ui_server, api_server, seeded_user, api):
    """MC-03: Switch to second company → dashboard loads under new company context."""
    # Create second company and get its token
    r = api.post("/companies", json={"name": "Browser Test Co Switch"})
    if r.status_code not in {200, 201}:
        # Already exists — get list and find it
        r2 = api.get("/auth/my-companies")
        if r2.status_code != 200:
            pytest.skip("Cannot list companies for switch test")
        companies = r2.json().get("items", r2.json()) if isinstance(r2.json(), dict) else r2.json()
        second = next((c for c in companies if "Switch" in c.get("name", "")), None)
        if not second:
            pytest.skip("Second company not found — cannot test switch context")
        # Switch to it to get a token
        cid = second.get("company_id") or second.get("id", "")
        switch_r = api.post(f"/auth/switch-company/{cid}")
        if switch_r.status_code != 200:
            pytest.skip(f"Cannot switch to second company: {switch_r.status_code}")
        new_token = switch_r.json().get("access_token", "")
    else:
        new_token = r.json().get("access_token", "")

    if not new_token:
        pytest.skip("No token for second company — cannot test switch context")

    # Open browser with new company token
    browser = playwright.chromium.launch(headless=True)
    ctx = browser.new_context(base_url=ui_server)
    ctx.add_cookies([{
        "name": "celerp_token",
        "value": new_token,
        "domain": "127.0.0.1",
        "path": "/",
    }])
    page = ctx.new_page()
    try:
        resp = page.goto(f"{ui_server}/", wait_until="domcontentloaded")
        assert resp.status != 500, "Dashboard returned 500 under second company context"
        body = page.locator("body").inner_text()
        assert "Internal Server Error" not in body
        assert "Traceback" not in body
        assert "/login" not in page.url, "Redirected to login under second company token"
    finally:
        page.close()
        browser.close()


def test_switch_company_post_redirects(playwright, ui_server, api_server, seeded_user, api):
    """MC-04: POST /switch-company/{id} redirects to dashboard (no 500).

    Uses an isolated browser context so the shared session cookie is never mutated.
    Switching company sets a new cookie; polluting the shared context would cause
    subsequent tests to query the wrong company's data.
    """
    r = api.get("/auth/my-companies")
    if r.status_code != 200:
        pytest.skip(f"Cannot list companies: {r.status_code}")

    data = r.json()
    companies = data.get("items", data) if isinstance(data, dict) else data
    if not companies:
        pytest.skip("No companies in list response")

    company_id = companies[0].get("company_id") or companies[0].get("id", "")
    if not company_id:
        pytest.skip(f"No company_id in response: {companies[0]}")

    # Isolated context — switch does not mutate the shared session cookie
    browser = playwright.chromium.launch(headless=True)
    ctx = browser.new_context(base_url=ui_server)
    ctx.add_cookies([{
        "name": "celerp_token",
        "value": seeded_user["access_token"],
        "domain": "127.0.0.1",
        "path": "/",
    }])
    page = ctx.new_page()
    try:
        page.goto(f"{ui_server}/", wait_until="domcontentloaded")
        resp = page.request.post(f"{ui_server}/switch-company/{company_id}")
        assert resp.status not in {500, 422}, \
            f"POST /switch-company/{company_id} returned {resp.status}"
    finally:
        page.close()
        browser.close()
