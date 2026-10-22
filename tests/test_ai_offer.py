# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The relay's included AI question counts, as account-methods and ai-offer serve them.

Both routes read one cached copy of the relay's /auth/methods answer: a fresh copy
is reused, concurrent misses share one fetch, a failed refetch keeps the last good
answer, and with no good answer the counts are null rather than a guessed number.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_METHODS = {"google": False, "free_email_quota": 10, "free_ai_questions": 20,
            "plan_ai_questions": {"cloud": 100, "ai": 200, "team": 200}}


def _relay(get):
    client = MagicMock()
    client.get = get
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


def _answer(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=payload)
    return resp


def _patches(factory, tier="free"):
    return (
        patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"),
        patch("celerp.gateway.state.get_subscription_state", return_value=(tier, "active")),
        patch("celerp.routers.health.is_install_owner", new=AsyncMock(return_value=False)),
        patch("httpx.AsyncClient", factory),
    )


async def _methods(factory):
    from celerp.routers.health import account_methods_api
    p1, p2, p3, p4 = _patches(factory)
    with p1, p2, p3, p4:
        return await account_methods_api(user=MagicMock(id="u1"), session=MagicMock())


async def _offer(factory, tier="free", calls=1):
    """ai_offer_api under one set of patches; ``calls`` concurrent requests share them
    (nested patches entered and left out of order would leak a mock past the test)."""
    from celerp.routers.health import ai_offer_api
    p1, p2, p3, p4 = _patches(factory, tier)
    with p1, p2, p3, p4:
        answers = await asyncio.gather(*(ai_offer_api() for _ in range(calls)))
    return answers if calls > 1 else answers[0]


async def test_account_methods_returns_ai_counts():
    data = await _methods(_relay(AsyncMock(return_value=_answer(_METHODS))))
    assert data["free_ai_questions"] == 20
    assert data["plan_ai_questions"] == {"cloud": 100, "ai": 200, "team": 200}
    assert data["free_email_quota"] == 10

    from celerp.routers.health import _relay_methods
    _relay_methods.clear()
    bad = dict(_METHODS, free_ai_questions="lots",
               plan_ai_questions={"cloud": -1, "ai": True, "team": "200"})
    data = await _methods(_relay(AsyncMock(return_value=_answer(bad))))
    assert data["free_ai_questions"] is None
    assert data["plan_ai_questions"] is None

    _relay_methods.clear()
    data = await _methods(_relay(AsyncMock(return_value=_answer(
        dict(_METHODS, free_ai_questions=True, plan_ai_questions=[100])))))
    assert data["free_ai_questions"] is None
    assert data["plan_ai_questions"] is None


async def test_ai_offer_uses_cached_methods():
    calls = 0

    async def _get(url, **kw):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return _answer(_METHODS)

    factory = _relay(_get)
    first, second = await _offer(factory, calls=2)
    assert calls == 1, "concurrent misses share one relay fetch"
    assert first == second == {"paid_ai": False, "free_ai_questions": 20,
                               "plan_ai_questions": {"cloud": 100, "ai": 200, "team": 200}}
    await _offer(factory)
    assert calls == 1, "a fresh cached answer makes no relay request"
    assert (await _offer(factory, tier="cloud"))["paid_ai"] is True


async def test_ai_offer_fetch_failure_returns_null():
    data = await _offer(_relay(AsyncMock(side_effect=ConnectionError("down"))))
    assert data == {"paid_ai": False, "free_ai_questions": None, "plan_ai_questions": None}
    data = await _offer(_relay(AsyncMock(return_value=_answer({}, status=503))))
    assert data["free_ai_questions"] is None and data["plan_ai_questions"] is None


async def test_failed_refetch_serves_last_good_answer():
    from celerp.routers import health
    await _offer(_relay(AsyncMock(return_value=_answer(_METHODS))))
    health._relay_methods["at"] -= health.RELAY_METHODS_TTL + 1
    data = await _offer(_relay(AsyncMock(side_effect=ConnectionError("down"))))
    assert data["free_ai_questions"] == 20
