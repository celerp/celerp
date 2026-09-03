# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the gateway commercial-context consumer.

Exercises the version-gated acceptance, the read API, the two inbound message
branches (hello_ack and commercial_updated), and the last-known-good cache
without a live WebSocket by calling _dispatch() and the state functions
directly.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

from celerp.gateway.client import GatewayClient
import celerp.gateway.state as gw_state


@pytest.fixture
def client():
    return GatewayClient(
        gateway_token="test-gateway-token",
        instance_id="test-instance-id",
        gateway_url="wss://relay.celerp.com/ws/connect",
    )


@pytest.fixture(autouse=True)
def reset_commercial_context():
    gw_state._commercial_context = {}
    yield
    gw_state._commercial_context = {}


def _ctx(version=1, schema_version=1, mode="partner_managed",
         implementation="default", offer="default"):
    """Build a well-formed commercial context. String sentinels keep the
    default sub-objects out of the mutable-default trap."""
    if implementation == "default":
        implementation = {
            "status": "active",
            "partner_id": "partner-1",
            "display_name": "Partner Co",
            "support_email": "support@partner.example.com",
            "support_url": "https://partner.example.com/support",
        }
    if offer == "default":
        offer = {
            "offer_id": "offer-1",
            "display_name": "Managed Plan",
            "retail_amount": 4900,
            "currency": "USD",
            "billing_interval": "month",
            "service_description": "Fully managed onboarding and support.",
            "service_bullets": ["Setup", "Support", "Training"],
        }
    return {
        "schema_version": schema_version,
        "version": version,
        "commercial_mode": mode,
        "implementation": implementation,
        "offer": offer,
    }


# -- inbound branches --------------------------------------------------------

@pytest.mark.asyncio
async def test_hello_ack_stores_commercial_context(client):
    """A hello_ack carrying commercial_context populates the model and read API."""
    await client._dispatch({
        "type": "hello_ack",
        "payload": {"commercial_context": _ctx(mode="partner_managed")},
    })
    assert gw_state.get_commercial_mode() == "partner_managed"
    assert gw_state.get_commercial_context()["version"] == 1
    assert gw_state.get_partner_identity()["display_name"] == "Partner Co"


@pytest.mark.asyncio
async def test_commercial_updated_applies(client):
    """A commercial_updated message applies its context to the model."""
    await client._dispatch({
        "type": "commercial_updated",
        "payload": _ctx(version=3, mode="partner_managed"),
    })
    assert gw_state.get_commercial_mode() == "partner_managed"
    assert gw_state.get_commercial_context()["version"] == 3
    assert gw_state.get_offer()["offer_id"] == "offer-1"


# -- version gate ------------------------------------------------------------

def test_version_rejects_stale():
    """A strictly-older version is rejected; last-known-good is preserved."""
    assert gw_state.set_commercial_context(_ctx(version=5)) is True
    assert gw_state.set_commercial_context(_ctx(version=3, mode="celerp_direct")) is False
    assert gw_state.get_commercial_context()["version"] == 5
    assert gw_state.get_commercial_mode() == "partner_managed"


def test_version_rejects_equal():
    """An equal version is rejected (strictly-newer only)."""
    assert gw_state.set_commercial_context(_ctx(version=5)) is True
    assert gw_state.set_commercial_context(_ctx(version=5, mode="celerp_direct")) is False
    assert gw_state.get_commercial_context()["version"] == 5
    assert gw_state.get_commercial_mode() == "partner_managed"


def test_version_accepts_newer():
    """A strictly-newer version is accepted and replaces the held context."""
    assert gw_state.set_commercial_context(_ctx(version=5)) is True
    assert gw_state.set_commercial_context(_ctx(version=6, mode="celerp_direct")) is True
    assert gw_state.get_commercial_context()["version"] == 6
    assert gw_state.get_commercial_mode() == "celerp_direct"


# -- preservation and release ------------------------------------------------

@pytest.mark.asyncio
async def test_absence_preserves_partner_managed(client):
    """A hello_ack without commercial_context leaves a cached partner_managed intact."""
    assert gw_state.set_commercial_context(_ctx(version=1, mode="partner_managed")) is True
    await client._dispatch({
        "type": "hello_ack",
        "payload": {"session_token": "tok-1"},
    })
    assert gw_state.get_commercial_mode() == "partner_managed"


def test_direct_releases_partner_managed():
    """A strictly-newer celerp_direct context replaces a cached partner_managed."""
    assert gw_state.set_commercial_context(_ctx(version=1, mode="partner_managed")) is True
    assert gw_state.set_commercial_context(
        _ctx(version=2, mode="celerp_direct", implementation=None, offer=None)) is True
    assert gw_state.get_commercial_mode() == "celerp_direct"
    assert gw_state.get_partner_identity() is None


def test_unknown_schema_version_preserves():
    """A schema_version above the supported max is rejected; last-known-good stays."""
    assert gw_state.set_commercial_context(_ctx(version=1, mode="partner_managed")) is True
    assert gw_state.set_commercial_context(
        _ctx(version=2, schema_version=2, mode="celerp_direct")) is False
    assert gw_state.get_commercial_mode() == "partner_managed"
    assert gw_state.get_commercial_context()["version"] == 1


# -- invalid input -----------------------------------------------------------

def test_invalid_mode_rejected():
    """A commercial_mode outside the enum is rejected; last-known-good preserved."""
    assert gw_state.set_commercial_context(_ctx(version=1, mode="partner_managed")) is True
    assert gw_state.set_commercial_context(_ctx(version=2, mode="reseller")) is False
    assert gw_state.get_commercial_mode() == "partner_managed"


def test_invalid_subobject_rejected():
    """An implementation that is neither null nor an object rejects the whole
    context; nothing is partial-applied."""
    assert gw_state.set_commercial_context(_ctx(version=1, mode="partner_managed")) is True
    assert gw_state.set_commercial_context(
        _ctx(version=2, mode="celerp_direct", implementation=42)) is False
    assert gw_state.get_commercial_mode() == "partner_managed"
    assert gw_state.get_commercial_context()["version"] == 1


# -- read API defaults -------------------------------------------------------

def test_default_mode_direct():
    """An empty model reports the neutral celerp_direct default."""
    assert gw_state.get_commercial_context() == {}
    assert gw_state.get_commercial_mode() == "celerp_direct"
    assert gw_state.get_partner_identity() is None
    assert gw_state.get_offer() is None


# -- persistence -------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_reload_roundtrip(client, monkeypatch, tmp_path):
    """An accepted context persists to the cache and reloads after a restart."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    await client._dispatch({
        "type": "commercial_updated",
        "payload": _ctx(version=7, mode="partner_managed"),
    })
    # Simulate a restart: drop the in-memory model, then load from the cache.
    gw_state._commercial_context = {}
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_mode() == "partner_managed"
    assert gw_state.get_commercial_context()["version"] == 7


def test_missing_cache_defaults_direct(monkeypatch, tmp_path):
    """A missing or corrupt cache file loads as the neutral celerp_direct default."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    # Corrupt file present: still degrades to the neutral default, never fabricates.
    (tmp_path / "celerp-config.json").write_text("{ not json")
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_context() == {}
    assert gw_state.get_commercial_mode() == "celerp_direct"


@pytest.mark.asyncio
async def test_commercial_write_preserves_feature_flags(client, monkeypatch, tmp_path):
    """Writing the commercial_context key must not drop the feature_flags key in
    the shared config file."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    config_path.write_text(json.dumps({"feature_flags": {"external_db": True}}))
    await client._dispatch({
        "type": "commercial_updated",
        "payload": _ctx(version=1, mode="partner_managed"),
    })
    written = json.loads(config_path.read_text())
    assert written["feature_flags"] == {"external_db": True}
    assert written["commercial_context"]["commercial_mode"] == "partner_managed"


# -- cache validation (routes the persisted cache through the acceptance gate) --

def _write_cache(tmp_path, cached):
    """Write a celerp-config.json whose commercial_context is `cached`, so the
    cache path can be exercised with shapes the persist-on-accept guard would
    never write itself."""
    (tmp_path / "celerp-config.json").write_text(
        json.dumps({"commercial_context": cached}))


def test_cache_rejects_unknown_schema_version(monkeypatch, tmp_path):
    """A cached context with an unsupported schema_version is rejected at load;
    the neutral default is preserved and no partner is shown."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _write_cache(tmp_path, _ctx(version=1, schema_version=2, mode="partner_managed"))
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_context() == {}
    assert gw_state.get_commercial_mode() == "celerp_direct"
    assert gw_state.get_partner_identity() is None


def test_cache_rejects_invalid_mode(monkeypatch, tmp_path):
    """A cached context with a commercial_mode outside the enum is rejected at
    load; the neutral default is preserved."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _write_cache(tmp_path, _ctx(version=1, mode="reseller"))
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_context() == {}
    assert gw_state.get_commercial_mode() == "celerp_direct"


def test_cache_rejects_nondict_subobject(monkeypatch, tmp_path):
    """A cached context whose implementation is neither null nor an object is
    rejected at load; the neutral default is preserved."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _write_cache(tmp_path, _ctx(version=1, mode="partner_managed", implementation=42))
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_context() == {}
    assert gw_state.get_commercial_mode() == "celerp_direct"


def test_cache_rejects_bad_version(monkeypatch, tmp_path):
    """A cached context missing an integer version is rejected at load; the
    neutral default is preserved."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    cached = _ctx(version=1, mode="partner_managed")
    del cached["version"]
    _write_cache(tmp_path, cached)
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_context() == {}
    assert gw_state.get_commercial_mode() == "celerp_direct"


def test_cache_accepts_valid_context(monkeypatch, tmp_path):
    """A well-formed cached context still loads through the gate (positive
    control: the tightened cache path must not over-reject a genuine cache)."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _write_cache(tmp_path, _ctx(version=4, mode="partner_managed"))
    gw_state.load_commercial_context()
    assert gw_state.get_commercial_mode() == "partner_managed"
    assert gw_state.get_commercial_context()["version"] == 4
    assert gw_state.get_partner_identity()["display_name"] == "Partner Co"


# -- immutability of the held model (deep copy at both boundaries) -----------

def test_get_offer_nested_mutation_isolated():
    """Mutating a nested list in get_offer()'s result does not change the held
    model."""
    assert gw_state.set_commercial_context(_ctx(version=1, mode="partner_managed")) is True
    gw_state.get_offer()["service_bullets"].append("Injected")
    assert "Injected" not in gw_state.get_offer()["service_bullets"]


def test_get_context_nested_mutation_isolated():
    """Mutating a nested subobject reached through get_commercial_context() does
    not leak into the held model."""
    assert gw_state.set_commercial_context(_ctx(version=1, mode="partner_managed")) is True
    gw_state.get_commercial_context()["offer"]["service_bullets"].append("Injected")
    assert "Injected" not in gw_state.get_commercial_context()["offer"]["service_bullets"]


def test_get_partner_identity_mutation_isolated():
    """Mutating a nested value in get_partner_identity()'s result does not leak
    into the held model."""
    implementation = {
        "status": "active",
        "partner_id": "partner-1",
        "display_name": "Partner Co",
        "support_channels": ["email", "phone"],
    }
    assert gw_state.set_commercial_context(
        _ctx(version=1, mode="partner_managed", implementation=implementation)) is True
    gw_state.get_partner_identity()["support_channels"].append("chat")
    assert "chat" not in gw_state.get_partner_identity()["support_channels"]


def test_store_side_deepcopy_isolated():
    """Mutating the inbound payload after set_commercial_context accepts it does
    not change the held model (the store keeps its own deep copy)."""
    new = _ctx(version=1, mode="partner_managed")
    assert gw_state.set_commercial_context(new) is True
    new["offer"]["service_bullets"].append("Injected")
    assert "Injected" not in gw_state.get_offer()["service_bullets"]


# -- schema boundary (only the supported schema is accepted) -----------------

def test_schema_version_zero_rejected():
    """schema_version 0 is below the supported schema and is rejected."""
    assert gw_state.set_commercial_context(
        _ctx(version=1, schema_version=0, mode="partner_managed")) is False
    assert gw_state.get_commercial_context() == {}


def test_schema_version_negative_rejected():
    """A negative schema_version is rejected."""
    assert gw_state.set_commercial_context(
        _ctx(version=1, schema_version=-1, mode="partner_managed")) is False
    assert gw_state.get_commercial_context() == {}


def test_schema_version_one_accepted():
    """The supported schema_version is accepted (positive control: tightening the
    gate must not reject the one understood schema)."""
    assert gw_state.set_commercial_context(
        _ctx(version=1, schema_version=1, mode="partner_managed")) is True
    assert gw_state.get_commercial_context()["version"] == 1


def test_schema_version_above_max_rejected_with_upgrade(caplog):
    """A schema_version above the supported max is rejected with the distinct
    upgrade warning, kept separate from the invalid-schema reject."""
    import logging
    with caplog.at_level(logging.WARNING):
        assert gw_state.set_commercial_context(
            _ctx(version=1, schema_version=2, mode="partner_managed")) is False
    assert "needs updating" in caplog.text


# -- config persistence: atomicity and 0600 mode -----------------------------

def test_commercial_context_persist_preserves_0600(client, tmp_path, monkeypatch):
    """Persisting commercial_context must leave celerp-config.json at mode 0600.

    The secrets file (external_db_url, S3 keys) shares this file, so a write that
    broadens its mode to group/world exposes them.
    """
    import asyncio
    import stat

    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    # An existing 0600 secrets file that a persist must not broaden.
    config_path.write_text(json.dumps({"external_db_url": "postgresql://x"}))
    config_path.chmod(0o600)

    asyncio.run(client._persist_commercial_context(_ctx()))

    mode = stat.S_IMODE(config_path.stat().st_mode)
    assert mode == 0o600, f"config mode broadened to {oct(mode)}"
    persisted = json.loads(config_path.read_text())
    assert persisted["commercial_context"]["commercial_mode"] == "partner_managed"
    assert persisted["external_db_url"] == "postgresql://x"


def test_feature_flags_persist_survives_midwrite_failure(client, tmp_path, monkeypatch):
    """A failure mid-write must leave the prior config intact and valid JSON.

    The prior file (with its secrets) must survive an interrupted write rather
    than being truncated to an empty or corrupt state.
    """
    import asyncio

    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    prior = {"external_db_url": "postgresql://x", "feature_flags": {"external_db": False}}
    config_path.write_text(json.dumps(prior))

    import celerp.gateway.client as gw_client

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(gw_client.json, "dump", _boom)
    # Persist must swallow the write error (best-effort) and never raise.
    asyncio.run(client._persist_feature_flags({"external_db": True}))

    # The prior config is untouched and still parseable.
    reread = json.loads(config_path.read_text())
    assert reread == prior


def test_commercial_context_persist_coerces_non_dict_config(client, tmp_path, monkeypatch):
    """A valid-but-non-dict top-level config (array/string) is coerced to an
    object before the merge instead of raising."""
    import asyncio

    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    config_path.write_text(json.dumps(["not", "a", "dict"]))

    asyncio.run(client._persist_commercial_context(_ctx()))

    persisted = json.loads(config_path.read_text())
    assert isinstance(persisted, dict)
    assert persisted["commercial_context"]["commercial_mode"] == "partner_managed"


# -- the API->UI commercial-state endpoint -----------------------------------

@pytest.mark.asyncio
async def test_system_commercial_state_endpoint_returns_state(session):
    """GET /companies/commercial-state returns feature_flags, commercial_context,
    partner_identity and commercial_mode under manage_integrations."""
    import secrets

    from httpx import ASGITransport, AsyncClient

    from celerp.db import get_session
    from celerp.main import app
    from celerp.services.session_tracker import clear as _clear_tracker

    await _clear_tracker(session)
    app.dependency_overrides[get_session] = lambda: session
    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    token = secrets.token_hex(32)
    gw_state.set_session_token(token)
    gw_state.set_feature_flags({"external_db": True, "external_storage": False})
    assert gw_state.set_commercial_context(_ctx()) is True

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/auth/register", json={
                "company_name": "SeamCo", "email": "owner@example.com",
                "name": "Owner", "password": "pw",
            })
            login = await c.post(
                "/auth/login",
                json={"email": "owner@example.com", "password": "pw"},
                headers={"X-Session-Token": token},
            )
            jwt = login.json()["access_token"]
            r = await c.get(
                "/companies/commercial-state",
                headers={"Authorization": f"Bearer {jwt}", "X-Session-Token": token},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["feature_flags"] == {"external_db": True, "external_storage": False}
        assert body["commercial_context"]["commercial_mode"] == "partner_managed"
        assert body["commercial_mode"] == "partner_managed"
        assert body["partner_identity"]["partner_id"] == "partner-1"
    finally:
        app.dependency_overrides.clear()
        gw_state.set_session_token("")
        gw_state.set_feature_flags({})
