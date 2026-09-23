# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/session_gate.py - 100% line coverage.

Error paths + happy paths:
  1. No header, no in-process session -> 401 with subscription CTA
  2. No header, in-process session exists -> 200 (same-origin UI request)
  3. Header present, session state empty (not connected) -> 401
  4. Header present, state set, mismatch (expired/wrong) -> 401
  5. Header matches state token -> 200
  6. Whitespace-only header, no session -> 401 (treated as missing)
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import celerp.gateway.state as gw_state
from celerp.session_gate import require_session_token

_SUBSCRIBE_BASE = "https://celerp.com/subscribe"


# Minimal FastAPI app with one gated route

_app = FastAPI()


@_app.get("/gated", dependencies=[])
async def gated_route(token_check: None = __import__("fastapi").Depends(require_session_token)):
    return {"ok": True}


_client = TestClient(_app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def reset_session_token():
    """Ensure gateway state is clean between tests."""
    original = gw_state.get_session_token()
    gw_state.set_session_token("")
    yield
    gw_state.set_session_token(original)


def test_no_header_no_session_returns_401():
    """Missing transport is reported as transport state, never as a billing verdict."""
    resp = _client.get("/gated")
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert "No active Celerp Connect session" in detail
    assert "Settings > Web Access" in detail
    assert _SUBSCRIBE_BASE not in detail


def test_no_header_with_session_passes():
    """No header + in-process session exists -> allow (same-origin UI)."""
    gw_state.set_session_token("in-process-session-token")
    resp = _client.get("/gated")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_header_present_but_instance_not_connected():
    """Header sent but session state is empty -> not-connected error."""
    gw_state.set_session_token("")
    resp = _client.get("/gated", headers={"X-Session-Token": "some-token"})
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert "not connected" in detail.lower()
    assert "settings > web access" in detail.lower()


def test_header_mismatch_returns_expired_error():
    """Header doesn't match in-process token -> expiry/mismatch error."""
    gw_state.set_session_token("correct-token-abc")
    resp = _client.get("/gated", headers={"X-Session-Token": "wrong-token-xyz"})
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert "no longer valid" in detail.lower()
    assert "settings > web access" in detail.lower()


def test_valid_header_passes():
    """Matching header token -> 200."""
    gw_state.set_session_token("valid-secret-token-123")
    resp = _client.get("/gated", headers={"X-Session-Token": "valid-secret-token-123"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_whitespace_header_no_session_treated_as_missing():
    """Whitespace-only header follows the same neutral transport path."""
    resp = _client.get("/gated", headers={"X-Session-Token": "   "})
    assert resp.status_code == 401
    assert _SUBSCRIBE_BASE not in resp.json()["detail"]


def test_whitespace_header_with_session_passes():
    """Whitespace-only header + in-process session -> allow."""
    gw_state.set_session_token("valid-token")
    resp = _client.get("/gated", headers={"X-Session-Token": "   "})
    assert resp.status_code == 200


@pytest.fixture
def _partner_mode():
    original = dict(gw_state._commercial_context)
    gw_state._commercial_context = {
        "commercial_mode": "partner_managed",
        "implementation": {"display_name": "Partner Co",
                           "support_url": "https://partner.example.com/support"},
    }
    yield
    gw_state._commercial_context = original


def test_session_gate_401_is_commercially_neutral(_partner_mode):
    """Transport loss does not manufacture either direct or partner purchase advice."""
    gw_state.set_session_token("")
    resp = _client.get("/gated")
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert "celerp.com/subscribe" not in detail
    assert "partner.example.com" not in detail
    assert "Settings > Web Access" in detail


def test_same_origin_request_recovers_session_from_durable_entitlement():
    async def recover():
        gw_state.set_session_token("recovered-session")
        return {}
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "celerp.services.cloud_entitlement.sync_existing_entitlement",
        side_effect=recover,
    ):
        resp = _client.get("/gated")
    assert resp.status_code == 200


def test_same_origin_recovery_local_apply_failure_degrades_to_401(monkeypatch):
    """Relay authority plus failed durable apply remains a retryable 401."""
    import httpx
    from unittest.mock import AsyncMock, patch
    from celerp.config import settings

    monkeypatch.setattr(settings, "gateway_token", "durable-api-key")
    monkeypatch.setattr(settings, "gateway_instance_id", "issue-332-iid")
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    response = httpx.Response(200, json={
        "gateway_token": "durable-api-key",
        "tier": "ai",
        "status": "active",
        "public_url": "https://issue332.celerp.com",
    })

    async def run(_timeout, operation):
        class Client:
            async def post(self, *_args, **_kwargs):
                return response
        return await operation(Client())

    with (
        patch(
            "celerp.gateway.state.fetch_relay_auth",
            new=AsyncMock(return_value=("instance-jwt", "issue-332-iid")),
        ) as relay_auth,
        patch("celerp.gateway.state.with_relay_client", new=run),
        patch(
            "celerp.services.cloud_entitlement.apply_activation_state",
            new=AsyncMock(side_effect=OSError("config write failed")),
        ) as apply_state,
    ):
        resp = _client.get("/gated")

    assert resp.status_code == 401
    assert "No active Celerp Connect session" in resp.json()["detail"]
    relay_auth.assert_awaited_once()
    apply_state.assert_awaited_once()

def test_explicit_disconnect_never_auto_recovers(monkeypatch):
    from unittest.mock import AsyncMock, patch
    from celerp.config import settings
    monkeypatch.setattr(settings, "cloud_disconnected", True)
    with patch(
        "celerp.services.cloud_entitlement.sync_existing_entitlement",
        new=AsyncMock(),
    ) as recover:
        resp = _client.get("/gated")
    assert resp.status_code == 401
    recover.assert_not_awaited()
