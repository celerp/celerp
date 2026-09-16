# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the pre-boot entitlement preflight (s29).

The preflight is a bounded one-shot Electron spawns before it decides whether to
persist an external-DB choice. It refreshes the entitlement from the relay and
returns a tri-state exit code: RENEWED (external allowed), EXPIRED (fall back to
local), or UNREACHABLE (the relay could not authoritatively answer, so no silent
switch). Only a 200 body is authoritative; every transport, HTTP-status, or
body-shape failure maps to UNREACHABLE, and a failed flag write is UNREACHABLE
too so a half-refreshed state never reaches the DB-mode decision.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from unittest.mock import AsyncMock

from celerp import config_store
from celerp import entitlement_preflight as pf


class _Resp:
    """A minimal stand-in for the relay's httpx.Response."""

    def __init__(self, status_code, body=None, bad_json=False):
        self.status_code = status_code
        self._body = body
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("body is not JSON")
        return self._body


@pytest.fixture(autouse=True)
def _packaged(tmp_path, monkeypatch):
    """A packaged data dir and an associated gateway token by default; the relay
    call is always stubbed so no test touches the network or the real config."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pf.settings, "gateway_token", "gw-token", raising=False)
    monkeypatch.setattr(pf, "load_cloud_config", lambda: None, raising=False)
    yield


def _stub(monkeypatch, resp=None, exc=None):
    if exc is not None:
        monkeypatch.setattr(pf, "_request_subscription", AsyncMock(side_effect=exc))
    else:
        monkeypatch.setattr(pf, "_request_subscription", AsyncMock(return_value=resp))


def _flags(external_db=False, external_storage=False, payments_enabled=False,
           grace=None):
    """Build a canonical feature_flags object: all three required bool flags
    present plus a null-or-tz-aware grace, the shape the relay always emits."""
    return {
        "payments_enabled": payments_enabled,
        "external_db": external_db,
        "external_storage": external_storage,
        "grace_period_ends": grace,
    }


def _body(external_db=False, external_storage=False, payments_enabled=False,
          grace=None):
    """A full canonical relay body: canonical feature_flags with the top-level
    grace mirroring the nested value."""
    flags = _flags(external_db=external_db, external_storage=external_storage,
                   payments_enabled=payments_enabled, grace=grace)
    return {"feature_flags": flags, "grace_period_ends": grace}


def test_preflight_renewed_merges_flags(monkeypatch):
    """200 with external_db entitled -> RENEWED, and the flags are persisted
    through the packaged-config writer."""
    _stub(monkeypatch, _Resp(200, _body(external_db=True)))
    assert pf.run_preflight() == pf.RENEWED
    persisted = config_store.read_packaged_config()
    assert persisted["feature_flags"]["external_db"] is True


def test_preflight_expired_when_flags_deny(monkeypatch):
    """200 with external_db denied and no grace -> EXPIRED."""
    _stub(monkeypatch, _Resp(200, _body(external_db=False)))
    assert pf.run_preflight() == pf.EXPIRED


def test_preflight_missing_token_is_expired(monkeypatch):
    """No gateway token (never associated) -> EXPIRED without any relay call."""
    monkeypatch.setattr(pf.settings, "gateway_token", "", raising=False)
    _stub(monkeypatch, exc=AssertionError("relay must not be called without a token"))
    assert pf.run_preflight() == pf.EXPIRED


@pytest.mark.parametrize("resp,exc", [
    (None, ConnectionError("relay down")),
    (_Resp(503, {}), None),
    (_Resp(500, {}), None),
    (_Resp(401, {}), None),
    (_Resp(403, {}), None),
    (_Resp(200, bad_json=True), None),
    (_Resp(200, {"feature_flags": "notadict", "grace_period_ends": None}), None),
    (_Resp(200, {"feature_flags": {"external_db": True}, "grace_period_ends": None}), None),
])
def test_preflight_unreachable_paths(monkeypatch, resp, exc):
    """Transport error, 5xx, 401/403, and malformed bodies all map to
    UNREACHABLE: only a well-formed 200 body is authoritative."""
    _stub(monkeypatch, resp=resp, exc=exc)
    assert pf.run_preflight() == pf.UNREACHABLE


def test_preflight_failed_merge_is_unreachable(monkeypatch):
    """A refreshed 200 whose flag write fails -> UNREACHABLE, never a
    half-refreshed RENEWED."""
    _stub(monkeypatch, _Resp(200, _body(external_db=True)))
    monkeypatch.setattr(pf, "merge_packaged_config", lambda updates: False)
    assert pf.run_preflight() == pf.UNREACHABLE
