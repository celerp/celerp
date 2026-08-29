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
