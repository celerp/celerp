# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Foundations for the commercial-UX correction pass (app side).

Covers the new low-level seams the presentation layer consumes:

* ``build_public_acquisition_url`` - the pre-auth/external acquisition URL that
  never emits a named ``instance_id`` on the direct path and never routes a
  top-up anonymously.
* ``get_local_infra_state`` - the cross-build (packaged + self-hosted)
  non-secret Team infrastructure state, exposing only booleans and dates.
* ``apply_commercial_context`` - the one apply/persist helper both the WS and
  the HTTP claim-accept path share, distinguishing applied-new from
  already-converged from rejected-invalid.
* self-hosted commercial-context durability via ``[cloud] commercial_context_json``.
* ``commercial_cta`` - the semantic CTA resolver mapping (intent, mode) to an
  (href, label) pair.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

import celerp.gateway.state as gw_state
from celerp.gateway.state import (
    apply_commercial_context,
    build_public_acquisition_url,
    build_subscribe_url,
    enterprise_url,
    get_local_infra_state,
)


_IID = "inst-123"


@pytest.fixture(autouse=True)
def reset_commercial_context():
    saved = gw_state._commercial_context
    saved_flags = gw_state._feature_flags
    gw_state._commercial_context = {}
    gw_state._feature_flags = {}
    yield
    gw_state._commercial_context = saved
    gw_state._feature_flags = saved_flags


def _direct_ctx(version=1):
    return {"version": version, "schema_version": 1, "commercial_mode": "celerp_direct"}


def _partner_ctx(version=1, support_url="https://partner.example.com/support",
                 support_email="help@partner.example.com"):
    impl = {"partner_id": "partner-1", "display_name": "Partner Co"}
    if support_url:
        impl["support_url"] = support_url
    if support_email:
        impl["support_email"] = support_email
    return {
        "version": version,
        "schema_version": 1,
        "commercial_mode": "partner_managed",
        "implementation": impl,
    }


# ── build_public_acquisition_url ──────────────────────────────────────────────

def test_public_acquisition_direct_has_no_instance_id():
    """A direct install's anonymous acquisition URL points at /subscribe with no
    instance_id (an anonymous self-serve entry, never a named checkout)."""
    gw_state._commercial_context = {"commercial_mode": "celerp_direct"}
    url = build_public_acquisition_url("cloud")
    assert "instance_id" not in url
    assert "/subscribe" in url
    assert url == build_subscribe_url("", extra="plan=cloud")


def test_public_acquisition_direct_ai_has_no_instance_id():
    gw_state._commercial_context = {"commercial_mode": "celerp_direct"}
    url = build_public_acquisition_url("ai")
    assert "instance_id" not in url
    assert url == build_subscribe_url("", extra="plan=ai")


def test_public_acquisition_never_topup():
    """A top-up sku must never resolve to the anonymous route; it degrades to the
    Enterprise route rather than an anonymous named-credit purchase."""
    gw_state._commercial_context = {"commercial_mode": "celerp_direct"}
    url = build_public_acquisition_url("topup")
    assert "/subscribe/topup" not in url
    assert "instance_id" not in url
    assert url == enterprise_url()


def test_public_acquisition_team_is_enterprise():
    gw_state._commercial_context = {"commercial_mode": "celerp_direct"}
    assert build_public_acquisition_url("team") == enterprise_url()


def test_public_acquisition_unknown_mode_is_enterprise():
    """An unknown commercial mode fails closed to the Enterprise route, never a
    direct checkout."""
    gw_state._commercial_context = {"commercial_mode": "reseller"}
    assert build_public_acquisition_url("cloud") == enterprise_url()


def test_public_acquisition_partner_uses_support_url():
    gw_state._commercial_context = _partner_ctx()
    assert build_public_acquisition_url("cloud") == "https://partner.example.com/support"


def test_public_acquisition_partner_falls_back_to_email():
    gw_state._commercial_context = _partner_ctx(support_url="")
    assert build_public_acquisition_url("cloud") == "mailto:help@partner.example.com"


def test_public_acquisition_partner_neither_is_enterprise():
    gw_state._commercial_context = _partner_ctx(support_url="", support_email="")
    assert build_public_acquisition_url("cloud") == enterprise_url()


# ── apply_commercial_context ──────────────────────────────────────────────────

def test_apply_new_context_reports_applied(monkeypatch):
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    status = apply_commercial_context(_partner_ctx(version=2))
    assert status == "applied"
    assert gw_state.get_commercial_mode() == "partner_managed"


def test_apply_older_valid_context_reports_converged(monkeypatch):
    """An incoming context at a version <= the held one but with a valid shape is
    already-converged success, not a rejection."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    assert apply_commercial_context(_partner_ctx(version=5)) == "applied"
    status = apply_commercial_context(_partner_ctx(version=5))
    assert status == "converged"
    # held state untouched
    assert gw_state.get_commercial_context()["version"] == 5


def test_apply_invalid_context_reports_rejected(monkeypatch):
    """A malformed shape is a rejection, distinct from converged, and never
    overwrites last-known-good."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    assert apply_commercial_context(_partner_ctx(version=3)) == "applied"
    # partner_managed with no implementation is invalid
    bad = {"version": 9, "schema_version": 1, "commercial_mode": "partner_managed"}
    status = apply_commercial_context(bad)
    assert status == "rejected"
    assert gw_state.get_commercial_context()["version"] == 3


def test_apply_persists_self_hosted(monkeypatch, tmp_path):
    """On a self-hosted install (no CELERP_DATA_DIR) an accepted context is
    written to config.toml [cloud] commercial_context_json."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg_path))
    assert apply_commercial_context(_partner_ctx(version=2)) == "applied"
    from celerp.config import read_config
    stored = read_config().get("cloud", {}).get("commercial_context_json", "")
    assert stored
    decoded = json.loads(stored)
    assert decoded["commercial_mode"] == "partner_managed"


# ── self-hosted commercial-context durability ─────────────────────────────────

def test_self_hosted_partner_context_survives_restart(monkeypatch, tmp_path):
    """A partner-managed context persisted to config.toml loads back on restart
    with the relay unavailable (no CELERP_DATA_DIR)."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg_path))
    assert apply_commercial_context(_partner_ctx(version=4)) == "applied"
    # simulate restart: clear in-memory state, reload from disk
    gw_state._commercial_context = {}
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_mode() == "partner_managed"
    assert gw_state.get_commercial_context()["version"] == 4
    assert gw_state.get_partner_identity()["display_name"] == "Partner Co"


def test_self_hosted_direct_context_survives_restart(monkeypatch, tmp_path):
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg_path))
    assert apply_commercial_context(_direct_ctx(version=3)) == "applied"
    gw_state._commercial_context = {}
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_mode() == "celerp_direct"
    assert gw_state.get_commercial_context()["version"] == 3


def test_self_hosted_corrupt_json_fails_closed(monkeypatch, tmp_path):
    """A corrupt commercial_context_json leaves the neutral default; no partner."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg_path))
    from celerp.config import read_config, write_config
    cfg = read_config()
    cfg.setdefault("cloud", {})["commercial_context_json"] = "{ not json"
    write_config(cfg)
    gw_state._commercial_context = {}
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_context() == {}
    assert gw_state.get_commercial_mode() == "celerp_direct"


def test_self_hosted_invalid_schema_rejected(monkeypatch, tmp_path):
    """An invalid schema in the persisted JSON stays rejected by existing
    validation, preserving the neutral default."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg_path))
    from celerp.config import read_config, write_config
    bad = _partner_ctx(version=1)
    bad["schema_version"] = 99
    cfg = read_config()
    cfg.setdefault("cloud", {})["commercial_context_json"] = json.dumps(bad)
    write_config(cfg)
    gw_state._commercial_context = {}
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_context() == {}
    assert gw_state.get_commercial_mode() == "celerp_direct"


def test_self_hosted_partner_offer_round_trips(monkeypatch, tmp_path):
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg_path))
    ctx = _partner_ctx(version=2)
    ctx["offer"] = {
        "display_name": "Managed Plan",
        "retail_amount": 4900,
        "currency": "USD",
        "currency_exponent": 2,
        "billing_interval": "month",
        "service_bullets": ["Priority support", "Onboarding"],
    }
    assert apply_commercial_context(ctx) == "applied"
    gw_state._commercial_context = {}
    gw_state.load_commercial_context()
    offer = gw_state.get_offer()
    assert offer["display_name"] == "Managed Plan"
    assert offer["service_bullets"] == ["Priority support", "Onboarding"]
    assert gw_state.get_partner_identity()["partner_id"] == "partner-1"


# ── get_local_infra_state (self-hosted, no CELERP_DATA_DIR) ───────────────────

_KEYS = {
    "has_external_url", "has_external_storage", "external_db_entitled",
    "external_storage_entitled", "grace_period_ends", "in_grace",
    "storage_in_grace",
}


def _future() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()


def _self_hosted_settings(monkeypatch, *, db_url="", storage_backend="local",
                          s3_endpoint=""):
    from celerp.config import settings
    monkeypatch.setattr(settings, "database_url", db_url or settings.database_url)
    monkeypatch.setattr(settings, "storage_backend", storage_backend)
    monkeypatch.setattr(settings, "storage_s3_endpoint", s3_endpoint)


def test_local_infra_state_key_set(monkeypatch):
    """The state exposes exactly the seven documented keys and nothing else."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    state = get_local_infra_state()
    assert set(state.keys()) == _KEYS


def test_local_infra_state_excludes_secrets(monkeypatch):
    """Self-hosted state never carries a DB URL, password, or S3 secret."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    from celerp.config import settings
    monkeypatch.setattr(
        settings, "database_url",
        "postgresql+asyncpg://celerp:s3cr3t@db.example.com:5432/celerp")
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "storage_s3_endpoint", "https://s3.example.com")
    monkeypatch.setattr(settings, "storage_s3_access_key", "AKIAEXAMPLE")
    monkeypatch.setattr(settings, "storage_s3_secret_key", "topsecretkey")
    state = get_local_infra_state()
    blob = json.dumps(state)
    assert "s3cr3t" not in blob
    assert "topsecretkey" not in blob
    assert "AKIAEXAMPLE" not in blob
    assert "s3.example.com" not in blob


def test_local_infra_active_team_visible(monkeypatch):
    """Active Team: entitlement flags true -> external infra entitled/visible."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    gw_state.set_feature_flags({"external_db": True, "external_storage": True})
    from celerp.config import settings
    monkeypatch.setattr(
        settings, "database_url",
        "postgresql+asyncpg://celerp:x@db.example.com:5432/celerp")
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "storage_s3_endpoint", "https://s3.example.com")
    state = get_local_infra_state()
    assert state["has_external_url"] is True
    assert state["has_external_storage"] is True
    assert state["external_db_entitled"] is True
    assert state["external_storage_entitled"] is True


def test_local_infra_grace_and_db(monkeypatch):
    """Grace + external DB configured: entitlement flag false but grace in future
    -> in_grace true, external url still configured."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    gw_state.set_feature_flags(
        {"external_db": False, "external_storage": False, "grace_period_ends": _future()})
    from celerp.config import settings
    monkeypatch.setattr(
        settings, "database_url",
        "postgresql+asyncpg://celerp:x@db.example.com:5432/celerp")
    state = get_local_infra_state()
    assert state["has_external_url"] is True
    assert state["in_grace"] is True
    assert state["external_db_entitled"] is False


def test_local_infra_after_grace_db_still_visible(monkeypatch):
    """After grace + external DB configured: not entitled, not in grace, but the
    external url stays configured so restore controls remain reachable."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    gw_state.set_feature_flags(
        {"external_db": False, "external_storage": False, "grace_period_ends": None})
    from celerp.config import settings
    monkeypatch.setattr(
        settings, "database_url",
        "postgresql+asyncpg://celerp:x@db.example.com:5432/celerp")
    state = get_local_infra_state()
    assert state["has_external_url"] is True
    assert state["external_db_entitled"] is False
    assert state["in_grace"] is False


def test_local_infra_after_grace_s3_only(monkeypatch):
    """After grace + S3 only: storage configured/visible, no external DB."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    gw_state.set_feature_flags({"external_storage": False, "grace_period_ends": None})
    from celerp.config import settings
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp")
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "storage_s3_endpoint", "https://s3.example.com")
    state = get_local_infra_state()
    assert state["has_external_storage"] is True
    assert state["has_external_url"] is False


def test_local_infra_none_configured(monkeypatch):
    """No entitlement, no external config -> nothing configured/entitled."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    gw_state.set_feature_flags({})
    from celerp.config import settings
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp")
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "storage_s3_endpoint", "")
    state = get_local_infra_state()
    assert state["has_external_url"] is False
    assert state["has_external_storage"] is False
    assert state["external_db_entitled"] is False
    assert state["external_storage_entitled"] is False


def test_local_infra_packaged_delegates(monkeypatch, tmp_path):
    """With CELERP_DATA_DIR set, the packaged branch drives the state (the same
    seven keys, sourced from celerp-config.json)."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    secret_url = "postgresql+asyncpg://celerp:s3cr3t@db.example.com:5432/celerp"
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "db_mode": "external",
        "external_db_url": secret_url,
        "feature_flags": {"external_db": True, "external_storage": False},
    }))
    state = get_local_infra_state()
    assert set(state.keys()) == _KEYS
    assert state["has_external_url"] is True
    assert "s3cr3t" not in json.dumps(state)
