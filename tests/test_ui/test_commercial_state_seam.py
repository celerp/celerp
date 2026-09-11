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


def _neutral_infra() -> dict:
    """A fully-neutral local infra projection: no configured external target, no
    entitlement, no open grace. This is the shape get_local_infra_state returns on
    an ordinary install with no Team infrastructure, and the seam tests patch it in
    so they exercise the commercial-state seam alone, independent of the ambient
    self-hosted settings.external_db / feature-flag grace state a co-resident test
    may have mutated."""
    return {
        "has_external_url": False,
        "has_external_storage": False,
        "external_db_entitled": False,
        "external_storage_entitled": False,
        "grace_period_ends": None,
        "in_grace": False,
        "storage_in_grace": False,
    }


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
    never fabricated entitlement.

    The subject is the commercial-state seam: that a failed or absent fetch
    degrades to {} and that a neutral commercial state grants no Team features.
    Local infra is pinned neutral so the assertion proves the seam fails closed
    and does not read entitlement from ambient self-hosted infra state (a
    separate clause, covered by test_ui_has_team_reflects_local_infra_after_grace
    and the packaged/self-hosted infra tests)."""
    from ui.routes.settings_cloud import _commercial_state, _has_team_features

    with patch("celerp.gateway.state.get_local_infra_state",
               return_value=_neutral_infra()):
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


@pytest.mark.asyncio
async def test_ui_has_team_reflects_local_infra_after_grace():
    """After grace lapses, a neutral commercial state ({}) must still show Team
    infrastructure when the local install has an external database configured but
    no longer entitled, so the user can read the restore notice and swap back a
    backup. _has_team_features reads that from get_local_infra_state, not the
    fetched commercial state, so it holds even when the fetch returns {}."""
    from ui.routes.settings_cloud import _has_team_features

    lapsed = _neutral_infra()
    lapsed["has_external_url"] = True  # external DB still configured, not entitled
    with patch("celerp.gateway.state.get_local_infra_state", return_value=lapsed):
        assert _has_team_features({}) is True
