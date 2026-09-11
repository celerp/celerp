# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""End-to-end owner/admin gate for the existing-install partner-claim card.

The claim card and its accept control appear on /settings/cloud only for an
owner or admin. A non-owner/admin (viewer) loading the page never sees the card,
and the API refuses the resolve/accept endpoints independently with 403 - the
render gate is not the only guard. This is the auth-boundary proof (P6.5).
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

pytestmark = pytest.mark.browser


def _clear_session_registry() -> None:
    """Wipe session_registry rows so a second user can log in."""
    import psycopg2
    from urllib.parse import urlsplit
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM session_registry;")
    conn.close()


def _set_cookie(browser_context, token: str) -> None:
    browser_context.clear_cookies()
    browser_context.add_cookies([{
        "name": "celerp_token", "value": token, "domain": "127.0.0.1", "path": "/",
    }])


_CARD = "#partner-claim-card"


def test_partner_claim_card_visible_to_owner(page, ui_server, seeded_user, browser_context):
    """The seeded user is the company owner: the partner-claim card renders on
    /settings/cloud with a claim-token input."""
    _set_cookie(browser_context, seeded_user["access_token"])
    try:
        page.goto(f"{ui_server}/settings/cloud", wait_until="domcontentloaded")
        page.wait_for_selector(_CARD, timeout=8000)
        assert page.locator(f'{_CARD} input[name="claim_token"]').count() == 1
    finally:
        _set_cookie(browser_context, seeded_user["access_token"])


def test_partner_claim_card_hidden_from_non_owner_admin(
    page, ui_server, api, api_server, browser_context, seeded_user):
    """A viewer never sees the partner-claim card, and the API refuses both
    partner-claim endpoints with 403 independently of the render gate."""
    tag = uuid.uuid4().hex[:6]
    email = f"viewer-{tag}@celerp.test"
    r = api.post("/companies/me/users",
                 json={"email": email, "name": "Viewer", "role": "viewer", "password": "pw12345"})
    assert r.status_code == 200, r.text

    _clear_session_registry()
    lr = httpx.post(f"{api_server}/auth/login",
                    json={"email": email, "password": "pw12345"}, timeout=10)
    assert lr.status_code == 200, lr.text
    viewer_token = lr.json()["access_token"]

    try:
        _set_cookie(browser_context, viewer_token)
        page.goto(f"{ui_server}/settings/cloud", wait_until="domcontentloaded")
        # Give the page a moment to render, then assert the card is absent.
        page.wait_for_timeout(500)
        assert page.locator(_CARD).count() == 0

        # The API refuses the endpoints independently of the UI render gate.
        for path in ("/settings/partner-claim/resolve", "/settings/partner-claim/accept"):
            resp = httpx.post(
                f"{api_server}{path}",
                headers={"Authorization": f"Bearer {viewer_token}"},
                json={"claim_token": "tok-abc"}, timeout=10)
            assert resp.status_code == 403, f"{path} -> {resp.status_code}"
    finally:
        _clear_session_registry()
        _set_cookie(browser_context, seeded_user["access_token"])
