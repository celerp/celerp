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
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import celerp.gateway.state as gw_state
from celerp.session_gate import require_session_token, _SUBSCRIBE_BASE


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
    """No header + no in-process session -> subscription CTA."""
    resp = _client.get("/gated")
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert _SUBSCRIBE_BASE in detail
    assert "always free" in detail


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
    assert "not connected" in detail
    assert "GATEWAY_TOKEN" in detail


def test_header_mismatch_returns_expired_error():
    """Header doesn't match in-process token -> expiry/mismatch error."""
    gw_state.set_session_token("correct-token-abc")
    resp = _client.get("/gated", headers={"X-Session-Token": "wrong-token-xyz"})
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert "expired" in detail.lower() or "invalid" in detail.lower()
    assert "Reconnect" in detail


def test_valid_header_passes():
    """Matching header token -> 200."""
    gw_state.set_session_token("valid-secret-token-123")
    resp = _client.get("/gated", headers={"X-Session-Token": "valid-secret-token-123"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_whitespace_header_no_session_treated_as_missing():
    """Whitespace-only header + no session -> subscribe CTA."""
    resp = _client.get("/gated", headers={"X-Session-Token": "   "})
    assert resp.status_code == 401
    assert _SUBSCRIBE_BASE in resp.json()["detail"]


def test_whitespace_header_with_session_passes():
    """Whitespace-only header + in-process session -> allow."""
    gw_state.set_session_token("valid-token")
    resp = _client.get("/gated", headers={"X-Session-Token": "   "})
    assert resp.status_code == 200
