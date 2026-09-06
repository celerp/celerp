# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the UI-process commercial-state seam.

The UI runs in a separate process from the API and cannot read the API's
in-process gateway-state globals. It must fetch live commercial state from the
API over an authenticated HTTP seam and fail closed to a neutral (no-team)
state when that fetch is unavailable.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest


def _request(token: str | None = "bearer-abc"):
    """A minimal request stand-in: cookies for get_token, mutable .state for the
    request-scoped memo."""
    cookies = {"celerp_token": token} if token is not None else {}
    return SimpleNamespace(cookies=cookies, state=SimpleNamespace())


@pytest.mark.asyncio
async def test_ui_has_team_reflects_api_state():
    """The UI tab logic reports Team features from API-held state fetched over
    the seam, not from the empty UI-process global."""
    from ui.routes.settings_cloud import _commercial_state, _has_team_features

    api_state = {
        "feature_flags": {"external_db": True, "external_storage": False},
        "commercial_context": {},
        "partner_identity": None,
        "commercial_mode": "celerp_direct",
    }
    with patch("ui.api_client.get_commercial_state", new=AsyncMock(return_value=api_state)):
        state = await _commercial_state(_request())

    assert _has_team_features(state) is True


@pytest.mark.asyncio
async def test_ui_commercial_state_fails_closed_to_neutral():
    """A failing, non-dict, or token-less state fetch yields no-team and neutral,
    never fabricated entitlement."""
    from ui.routes.settings_cloud import _commercial_state, _has_team_features

    # The fetch raising must degrade to a neutral empty state.
    with patch("ui.api_client.get_commercial_state",
               new=AsyncMock(side_effect=RuntimeError("api down"))):
        state = await _commercial_state(_request())
    assert state == {}
    assert _has_team_features(state) is False

    # A missing token short-circuits to neutral without any fetch.
    fetch = AsyncMock(return_value={"feature_flags": {"external_db": True}})
    with patch("ui.api_client.get_commercial_state", new=fetch):
        state = await _commercial_state(_request(token=None))
    assert state == {}
    assert _has_team_features(state) is False
    fetch.assert_not_awaited()
