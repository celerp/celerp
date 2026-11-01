# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Behind Connect each visitor is a requester of their own. Every Connect visitor reaches
the UI from the gateway on this computer, as the person at it does; the gateway names the
visitor by the address the relay saw, and the recovery and sign-in limits count each
visitor, and the person at this computer, apart. The UI is driven here as the gateway
reaches it: from loopback, with the Connect mark and the caller header the gateway sets."""
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from celerp.main import app as api_app
from celerp.routers import auth as auth_router
from celerp.services.auth import CALLER_HEADER, CONNECT_HEADER
from email_codes_support import CODE, relay, short_floor  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio

OWNER = "owner@example.com"
OLD_PW = "old-password1"
STRANGER, VISITOR = "198.51.100.7", "2001:db8:5::20"
LAN_PEER = "192.168.1.50"
# What reaches the UI when the relay named no visitor, or a value that is not one address.
UNNAMED = ({}, {CALLER_HEADER: ""}, {CALLER_HEADER: "nonsense"}, {CALLER_HEADER: f"{STRANGER}, {VISITOR}"})


@pytest.fixture
def limits_on():
    auth_router.limiter.enabled = True
    auth_router.limiter._storage.reset()
    yield
    auth_router.limiter.enabled = False
    auth_router.limiter._storage.reset()


@pytest.fixture
def clock(monkeypatch):
    """The time the sign-in waits are measured on, held still so no wait runs out mid-test."""
    monkeypatch.setattr("celerp.services.auth._clock", lambda: 1000.0, raising=False)


def _connect(address: str | None = None, **extra) -> dict:
    """The headers the gateway sets for a visitor the relay saw at *address*."""
    return {CONNECT_HEADER: "1", **({CALLER_HEADER: address} if address else {}), **extra}


@asynccontextmanager
async def _ui(peer: str = "127.0.0.1", headers: dict | None = None):
    from ui.app import app as ui_app

    with patch("ui.api_client._get_transport", lambda: ASGITransport(app=api_app)):
        async with AsyncClient(transport=ASGITransport(app=ui_app, client=(peer, 5000)), base_url="http://ui",
                               follow_redirects=False, headers=headers or {}) as ui:
            yield ui


async def _register(client) -> None:
    r = await client.post("/auth/register", json={"company_name": "Recovery Co", "email": OWNER,
                                                  "name": "Owner Person", "password": OLD_PW})
    assert r.status_code == 200, r.text


def _handle(html: str) -> str | None:
    form = re.search(r'<form\b[^>]*action="/forgot-password/reset".*?</form>', html, re.S)
    match = form and re.search(r'<input\b[^>]*name="handle"[^>]*value="([^"]+)"', form.group(0))
    return match.group(1) if match else None


async def _code_page(ui, email: str = OWNER) -> str:
    r = await ui.post("/forgot-password", data={"email": email})
    assert r.status_code == 200 and _handle(r.text), r.text
    return _handle(r.text)


async def _sends(ui, n: int, email: str = "nobody@example.com") -> list[bool]:
    """Whether each of *n* sends reached the code page."""
    return [_handle((await ui.post("/forgot-password", data={"email": email})).text) is not None for _ in range(n)]


async def _bogus_resets(ui, n: int) -> list[bool]:
    """Whether each of *n* resets with a made-up handle was refused for too many attempts."""
    from ui.i18n import t

    return [t("error.rate_limited") in (await ui.post("/forgot-password/reset", data={
        "email": OWNER, "handle": "x" * 43, "code": "00000000", "password": "zz-password1",
        "confirm": "zz-password1"})).text for _ in range(n)]


async def _reset(ui, handle: str, password: str) -> int:
    r = await ui.post("/forgot-password/reset", data={"email": OWNER, "handle": handle, "code": CODE,
                                                      "password": password, "confirm": password})
    return r.status_code


async def _signs_in(client, session, password: str) -> bool:
    """Whether *password* signs in; the session it opens is ended, so the next sign-in here
    finds this computer's sign-in place free."""
    from test_helpers import clear_sessions as clear

    ok = (await client.post("/auth/login", json={"email": OWNER, "password": password})).status_code == 200
    await clear(session)
    return ok


async def _person_and_visitor_recover(client, session) -> None:
    """The person at this computer and a second Connect visitor, at the same time, each get a
    code page and reset with their own handle; the password each set signs in."""
    async with _ui() as local, _ui(headers=_connect(VISITOR)) as visitor:
        mine, theirs = await asyncio.gather(_code_page(local), _code_page(visitor))
        assert await _reset(local, mine, "local-password1") == 303
        assert await _signs_in(client, session, "local-password1")
        assert await _reset(visitor, theirs, "visitor-password1") == 303
        assert await _signs_in(client, session, "visitor-password1")


async def test_a_connect_stranger_never_holds_back_anyone_elses_recovery(client, session, relay, short_floor, limits_on):
    await _register(client)
    async with _ui(headers=_connect(STRANGER)) as stranger:
        sends = await _sends(stranger, 21)
        assert sends == [True] * 3 + [False] * 18
        assert await _bogus_resets(stranger, 11) == [False] * 10 + [True]
    await _person_and_visitor_recover(client, session)


@pytest.mark.parametrize("unnamed", UNNAMED)
async def test_connect_visitors_the_relay_did_not_name_count_apart_from_everyone_else(
        client, session, relay, short_floor, limits_on, unnamed):
    """A Connect-marked request with no valid address counts as Connect visitors together:
    never as the person at this computer, and never as an address of the visitor's choosing."""
    await _register(client)
    async with _ui(headers={CONNECT_HEADER: "1", **unnamed}) as stranger:
        assert await _sends(stranger, 4) == [True] * 3 + [False]
        assert await _bogus_resets(stranger, 11) == [False] * 10 + [True]
    await _person_and_visitor_recover(client, session)


async def test_a_connect_mark_from_another_computer_counts_as_that_computer(client, session, relay, short_floor, limits_on):
    """A computer on the network that sends the Connect mark and a visitor's address is
    counted by its own address, so it cannot spend that visitor's recovery."""
    await _register(client)
    async with _ui(LAN_PEER, headers=_connect(VISITOR)) as lan:
        assert await _sends(lan, 4) == [True] * 3 + [False]
        assert await _bogus_resets(lan, 11) == [False] * 10 + [True]
    await _person_and_visitor_recover(client, session)


async def test_a_visitor_header_forged_in_the_browser_names_no_one(client, session, relay, short_floor, limits_on):
    """The UI never reads the relay's visitor header itself; only the gateway's caller
    header counts. A visitor who sends it, or a caller header, from the browser on this
    computer is still the person at it, and a Connect visitor sending another visitor's
    address in the relay's header is still counted by the gateway's."""
    from celerp.services.auth import VISITOR_HEADER

    await _register(client)
    async with _ui(headers=_connect(STRANGER, **{VISITOR_HEADER: VISITOR})) as stranger:
        assert await _sends(stranger, 4) == [True] * 3 + [False]
    async with _ui(headers={VISITOR_HEADER: STRANGER, CALLER_HEADER: STRANGER}) as local:
        assert await _sends(local, 4) == [True] * 3 + [False]
    async with _ui(headers=_connect(VISITOR)) as visitor:
        assert await _sends(visitor, 1) == [True]


async def _wrong_sign_ins(client, headers: dict, n: int) -> list[int]:
    return [(await client.post("/auth/login", json={"email": f"a{i}@example.com", "password": "wrong-pw"},
                               headers=headers)).status_code for i in range(n)]


@pytest.mark.parametrize("stranger", [_connect(STRANGER), *({CONNECT_HEADER: "1", **u} for u in UNNAMED)])
async def test_a_connect_strangers_failed_sign_ins_never_hold_back_anyone_else(client, session, clock, stranger):
    """One caller takes twenty failed sign-ins across every account, then waits. A Connect
    stranger who reaches that wait holds back neither the person at this computer nor
    another Connect visitor, each signing in and failing on their own count."""
    from test_helpers import clear_sessions as clear

    await _register(client)
    await clear(session)
    assert await _wrong_sign_ins(client, stranger, 21) == [401] * 20 + [429]
    assert await _wrong_sign_ins(client, {}, 1) == [401]
    assert await _wrong_sign_ins(client, _connect(VISITOR), 1) == [401]
    assert await _signs_in(client, session, OLD_PW)
    async with _ui(headers=_connect(VISITOR)) as visitor:
        r = await visitor.post("/login", data={"email": OWNER, "password": OLD_PW})
    assert r.status_code in (302, 303), r.text
