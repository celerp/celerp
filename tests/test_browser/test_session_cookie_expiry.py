# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every session cookie the browser suite writes lapses with the token it holds, as the UI's
own cookies do. A cookie kept past its token makes the browser send the expired access token,
which the UI answers with the sign-in page, so every page after the token's lifetime fails."""
from __future__ import annotations

import time

import pytest
from jose import jwt

from .conftest import _set_auth_cookie, session_cookies

pytestmark = pytest.mark.browser


class _Context:
    """Stands in for a browser context: records the cookies written to it."""

    def __init__(self) -> None:
        self.cookies: list[dict] = []

    def clear_cookies(self) -> None:
        self.cookies = []

    def add_cookies(self, cookies: list[dict]) -> None:
        self.cookies.extend(cookies)


def _token(exp: int) -> str:
    return jwt.encode({"sub": "user", "exp": exp}, "secret", algorithm="HS256")


def _from_session_cookies(name: str):
    def write(token: str) -> list[dict]:
        other = _token(int(time.time()) + 86400)
        user = {"access_token": token if name == "celerp_token" else other,
                "refresh_token": token if name == "celerp_refresh" else other}
        return [c for c in session_cookies(user) if c["name"] == name]
    return write


def _from_set_auth_cookie(token: str) -> list[dict]:
    ctx = _Context()
    _set_auth_cookie(ctx, token)
    return ctx.cookies


_WRITERS = {
    "session_cookies-access": _from_session_cookies("celerp_token"),
    "session_cookies-refresh": _from_session_cookies("celerp_refresh"),
    "set_auth_cookie": _from_set_auth_cookie,
}
_LIFETIMES = {"expired": -60, "minutes": 15 * 60, "days": 30 * 86400}


@pytest.mark.parametrize("lifetime", list(_LIFETIMES))
@pytest.mark.parametrize("writer", list(_WRITERS))
def test_a_session_cookie_lapses_with_its_token(writer, lifetime):
    exp = int(time.time()) + _LIFETIMES[lifetime]
    token = _token(exp)
    [cookie] = _WRITERS[writer](token)
    assert cookie["value"] == token
    assert cookie.get("expires") == exp, f"{writer} wrote {cookie['name']} without its token's expiry"
