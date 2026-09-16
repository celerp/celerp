# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Fixture-driven entitlement preflight tests (WS1 A1 consumer).

The app never hand-writes its own entitlement example: it consumes the exact
bytes the cloud produces (principle 2.9). This module vendors that fixture,
guards its SHA-256 against drift, and feeds every representative flag shape
through the rewritten preflight so the boot classification is checked
field-by-field against the canonical contract instead of the old
all-bool heuristic.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from unittest.mock import AsyncMock

from celerp import entitlement_preflight as pf

# The recorded SHA-256 of the canonical cloud fixture. The vendored bytes must
# equal this value; the cloud side records the same digest, so a drift on
# either side fails the guard rather than silently forking the contract.
RECORDED_FIXTURE_SHA256 = (
    "7128e53d0d41422ecbd3bf116f0060747864308848c249771fce8939ced5407b"
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "celerp" / "gateway" / "fixtures" / "entitlement_flags_fixture.json"
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_bytes())


class _Resp:
    """A minimal stand-in for the relay's httpx.Response."""

    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _packaged(tmp_path, monkeypatch):
    """A packaged data dir and an associated gateway token; the relay call is
    always stubbed so no test touches the network or the real config."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pf.settings, "gateway_token", "gw-token", raising=False)
    monkeypatch.setattr(pf, "load_cloud_config", lambda: None, raising=False)
    yield


def _body_from_row(flags: dict) -> dict:
    """Build the relay body for a fixture row: the flags object as
    feature_flags, with the top-level grace mirroring the nested value the way
    the cloud producer emits it."""
    return {
        "feature_flags": dict(flags),
        "grace_period_ends": flags.get("grace_period_ends"),
    }


def _run_with(monkeypatch, body):
    monkeypatch.setattr(pf, "_request_subscription", AsyncMock(return_value=_Resp(200, body)))
    return pf.run_preflight()


def check_app_fixture_sha_matches():
    """The vendored fixture's SHA-256 equals the recorded cloud value."""
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == RECORDED_FIXTURE_SHA256


def test_check_app_fixture_sha_matches():
    """Pytest entry point for the fixture-SHA guard."""
    check_app_fixture_sha_matches()


def test_fixture_rows_present():
    """Every representative row the preflight tests rely on is present in the
    vendored fixture (guards against a partial vendor)."""
    fixture = _load_fixture()
    for row in ("team_grace", "team_active", "team_expired", "free",
                "malformed_grace", "missing_flag", "full_real_shape"):
        assert row in fixture, f"missing fixture row {row}"


def test_preflight_team_grace(monkeypatch):
    """Team in-grace body (external_db true) -> RENEWED, no longer wrongly
    UNREACHABLE under the old all-bool check that rejected the nested grace
    string."""
    row = _load_fixture()["team_grace"]
    assert _run_with(monkeypatch, _body_from_row(row)) == pf.RENEWED


def test_preflight_team_active(monkeypatch):
    """Team external-active body (external_db true, grace null) -> RENEWED."""
    row = _load_fixture()["team_active"]
    assert _run_with(monkeypatch, _body_from_row(row)) == pf.RENEWED


def test_preflight_team_expired(monkeypatch):
    """Expired body (external_db false, grace null) -> EXPIRED, reached through
    the field-by-field path, not UNREACHABLE."""
    row = _load_fixture()["team_expired"]
    assert _run_with(monkeypatch, _body_from_row(row)) == pf.EXPIRED


def test_preflight_free(monkeypatch):
    """Free body (external flags false, grace null) -> EXPIRED, distinct from a
    missing required flag: the full four-field shape validates by field."""
    row = _load_fixture()["free"]
    assert _run_with(monkeypatch, _body_from_row(row)) == pf.EXPIRED


def test_preflight_rejects_naive_grace(monkeypatch):
    """A naive (non-tz-aware) grace timestamp -> UNREACHABLE: the canonical
    contract requires tz-aware ISO-8601.

    The naive-grace value is asserted directly against the grace validator so
    the red evidence isolates the tz-aware requirement, not the coincidental
    rejection of a nested grace string by the old all-bool heuristic.
    """
    naive = _load_fixture()["malformed_grace"]["grace_period_ends"]
    assert naive == "2026-06-01T00:00:00"
    assert pf._valid_grace(naive) is False
    row = _load_fixture()["malformed_grace"]
    assert _run_with(monkeypatch, _body_from_row(row)) == pf.UNREACHABLE


def _body_flags_bool_only(row: dict) -> dict:
    """Build a relay body from a fixture row with grace kept only at top level,
    so the nested feature_flags dict holds bool values alone. This isolates the
    field-level flag validation: the old all-bool heuristic passes such a dict,
    so a missing required flag is caught only by the field-by-field contract.
    """
    flags = {k: v for k, v in row.items() if k != "grace_period_ends"}
    return {"feature_flags": flags, "grace_period_ends": row.get("grace_period_ends")}


def test_preflight_missing_required_flag(monkeypatch):
    """A body missing a required flag (external_db) -> UNREACHABLE: the contract
    requires all three bool flags present. With grace at top level only, the old
    all-bool heuristic would pass the short bool dict; only the field-by-field
    contract catches the absent required flag."""
    row = _load_fixture()["missing_flag"]
    assert "external_db" not in row
    assert _run_with(monkeypatch, _body_flags_bool_only(row)) == pf.UNREACHABLE


def test_preflight_toplevel_not_authoritative(monkeypatch):
    """The top-level grace is no longer authoritative on its own: a bool-only
    flags body (which the old all-bool heuristic accepts) with external_db false
    and a future top-level grace but a null nested grace resolves to EXPIRED,
    where the old code folded the top-level value blindly to RENEWED. This is
    the red-first evidence that the top-level read is no longer trusted apart
    from the nested contract value."""
    body = {
        "feature_flags": {"external_db": False, "external_storage": False,
                          "payments_enabled": False, "grace_period_ends": None},
        "grace_period_ends": "2099-12-31T00:00:00+00:00",
    }
    assert _run_with(monkeypatch, body) == pf.EXPIRED


def test_preflight_asserts_toplevel_equals_nested(monkeypatch):
    """When the top-level grace and the nested grace disagree (both non-null),
    the body is malformed -> UNREACHABLE. The equality guard is new; at
    merge-base the same body already reaches UNREACHABLE through the broad
    all-bool rejection, so the discriminating red evidence for the equality
    logic is carried by test_preflight_toplevel_not_authoritative (top-level no
    longer folded blindly). This test locks the tightened outcome."""
    row = _load_fixture()["full_real_shape"]
    body = _body_from_row(row)  # nested grace present and valid tz-aware
    body["grace_period_ends"] = "2027-12-31T00:00:00+00:00"  # differs from nested
    assert _run_with(monkeypatch, body) == pf.UNREACHABLE
