# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression guards for the Connect activation deadline contract."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import ui.api_client as api
from celerp.routers import health


def test_connect_outer_deadline_exceeds_backend_worst_case():
    backend_budget = (
        health._RELAY_ACTIVATE_TIMEOUT
        + health._GATEWAY_START_ATTEMPTS * health._GATEWAY_START_INTERVAL
    )
    assert api._CONNECT_ACTIVATE_TIMEOUT > backend_budget
    assert api._CONNECT_ACTIVATE_TIMEOUT - backend_budget >= 4.0


@pytest.mark.asyncio
async def test_activate_relay_uses_connect_specific_timeout(monkeypatch):
    seen = {}
    response = MagicMock()
    response.is_redirect = False
    response.is_error = False
    response.json.return_value = {"connected": True}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    @asynccontextmanager
    async def fake_api_client(token, timeout=10.0):
        seen["token"] = token
        seen["timeout"] = timeout
        yield client

    monkeypatch.setattr(api, "_api_client", fake_api_client)
    result = await api.activate_relay("access-token")

    assert result == {"connected": True}
    assert seen == {"token": "access-token", "timeout": api._CONNECT_ACTIVATE_TIMEOUT}
    client.post.assert_awaited_once_with("/settings/cloud-activate")
