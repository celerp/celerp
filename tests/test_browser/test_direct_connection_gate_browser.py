# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Signing in while another session holds the one direct connection."""
from urllib.parse import urlsplit

import httpx
import pytest

from .conftest import _TEST_EMAIL, _TEST_PASSWORD, clear_session_registry

pytestmark = pytest.mark.browser


def _hold_direct_connection(api_server: str) -> None:
    clear_session_registry()
    r = httpx.post(f"{api_server}/auth/login", json={"email": _TEST_EMAIL, "password": _TEST_PASSWORD}, timeout=10)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("start, landing", [("/login", "/dashboard"), ("/login?next=/settings/general", "/settings/general")])
def test_continue_signing_out_the_other_user_lands_where_sign_in_would(unauthed_page, api_server, keep_sign_ins,
                                                                       start, landing):
    page = unauthed_page
    page.context.clear_cookies()
    _hold_direct_connection(api_server)
    page.goto(start)
    page.fill('input[name="email"]', _TEST_EMAIL)
    page.fill('input[name="password"]', _TEST_PASSWORD)
    page.click('button[type="submit"]')
    page.click('button:has-text("Continue (sign out the other user)")')
    page.wait_for_load_state()
    assert urlsplit(page.url).path == landing, page.url
    assert page.locator("text=Page Not Found").count() == 0, page.content()[-1500:]
