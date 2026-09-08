# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""All-or-nothing commercial-context acceptance and money-bound tests (WS1 A3/A5).

The context consumer accepts an envelope only when the WHOLE thing is valid:
a partner_managed envelope needs a valid implementation, any supplied
offer/subscription must pass whole, and celerp_direct must carry no
implementation or offer. Any invalidity preserves the last-known-good context
AND its version, so a corrected retransmission at the same version is accepted.
An instance_id change resets the version namespace.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

import celerp.gateway.state as gw_state


@pytest.fixture(autouse=True)
def reset_state():
    gw_state._commercial_context = {}
    gw_state._instance_id = ""
    yield
    gw_state._commercial_context = {}
    gw_state._instance_id = ""


def _impl():
    return {
        "status": "active",
        "partner_id": "partner-1",
        "display_name": "Partner Co",
        "support_email": "support@partner.example.com",
        "support_url": "https://partner.example.com/support",
    }


def _offer():
    return {
        "offer_id": "offer-1",
        "display_name": "Managed Plan",
        "retail_amount": 4900,
        "currency": "USD",
        "currency_exponent": 2,
        "billing_interval": "month",
        "service_bullets": ["Setup", "Support"],
    }


def _ctx(version=1, schema_version=1, mode="partner_managed",
         implementation="default", offer="default"):
    if implementation == "default":
        implementation = _impl()
    if offer == "default":
        offer = _offer()
    ctx = {
        "schema_version": schema_version,
        "version": version,
        "commercial_mode": mode,
    }
    if implementation is not None:
        ctx["implementation"] = implementation
    if offer is not None:
        ctx["offer"] = offer
    return ctx


# -- whole-envelope rejection ------------------------------------------------

def test_context_rejects_invalid_offer_whole():
    """A supplied non-null offer that fails validation rejects the WHOLE
    envelope: last-known-good and its version are preserved, so a corrected
    retransmission at the same version is accepted."""
    assert gw_state.set_commercial_context(_ctx(version=5, mode="partner_managed")) is True
    bad_offer = {**_offer(), "retail_amount": -100}
    assert gw_state.set_commercial_context(
        _ctx(version=6, mode="partner_managed", offer=bad_offer)) is False
    # Last-known-good AND version preserved (bad envelope never advanced it).
    assert gw_state.get_commercial_context()["version"] == 5
    assert gw_state.get_offer()["retail_amount"] == 4900
    # A corrected retransmission at the SAME version 6 is now accepted.
    assert gw_state.set_commercial_context(
        _ctx(version=6, mode="partner_managed")) is True
    assert gw_state.get_commercial_context()["version"] == 6


def test_context_partner_managed_requires_impl():
    """partner_managed with an invalid or absent implementation rejects the
    whole envelope; last-known-good is preserved."""
    assert gw_state.set_commercial_context(_ctx(version=1, mode="partner_managed")) is True
    # Invalid implementation (no partner_id) -> whole reject.
    bad_impl = {"display_name": "Partner Co",
                "support_url": "https://partner.example.com/support"}
    assert gw_state.set_commercial_context(
        _ctx(version=2, mode="partner_managed", implementation=bad_impl)) is False
    assert gw_state.get_commercial_context()["version"] == 1
    # Absent implementation under partner_managed -> whole reject.
    assert gw_state.set_commercial_context(
        _ctx(version=2, mode="partner_managed", implementation=None, offer=None)) is False
    assert gw_state.get_commercial_context()["version"] == 1
    assert gw_state.get_partner_identity()["partner_id"] == "partner-1"


def test_context_celerp_direct_shape():
    """celerp_direct is legal only when implementation is None AND offer is
    None; a celerp_direct envelope carrying either rejects whole."""
    # Legal celerp_direct: both null.
    assert gw_state.set_commercial_context(
        _ctx(version=1, mode="celerp_direct", implementation=None, offer=None)) is True
    assert gw_state.get_commercial_mode() == "celerp_direct"
    # Non-null implementation under celerp_direct -> whole reject.
    assert gw_state.set_commercial_context(
        _ctx(version=2, mode="celerp_direct", offer=None)) is False
    assert gw_state.get_commercial_context()["version"] == 1
    # Non-null offer under celerp_direct -> whole reject.
    assert gw_state.set_commercial_context(
        _ctx(version=2, mode="celerp_direct", implementation=None)) is False
    assert gw_state.get_commercial_context()["version"] == 1


def test_context_preserves_lkg_version():
    """A rejected envelope never advances the held version: the all-or-nothing
    gate runs before the strictly-newer version check."""
    assert gw_state.set_commercial_context(_ctx(version=10, mode="partner_managed")) is True
    bad = _ctx(version=11, mode="partner_managed", offer={**_offer(), "currency": 840})
    assert gw_state.set_commercial_context(bad) is False
    assert gw_state.get_commercial_context()["version"] == 10
    # Version 11 is still open because the bad one did not advance it.
    assert gw_state.set_commercial_context(_ctx(version=11, mode="partner_managed")) is True
    assert gw_state.get_commercial_context()["version"] == 11


# -- instance_id / version namespace -----------------------------------------

def test_context_iid_change_resets_version():
    """An observed instance_id CHANGE clears the held context, resetting the
    version namespace so the new instance's context is not rejected as stale.
    The initial set from "" preserves a disk-loaded context."""
    # Initial set from "" preserves a disk-loaded context.
    assert gw_state.set_commercial_context(_ctx(version=9, mode="partner_managed")) is True
    gw_state.set_instance_id("inst-A")  # initial set from "" - context preserved
    assert gw_state.get_commercial_context()["version"] == 9
    # A same-value set is not a change - context preserved.
    gw_state.set_instance_id("inst-A")
    assert gw_state.get_commercial_context()["version"] == 9
    # An observed CHANGE clears the context and its version.
    gw_state.set_instance_id("inst-B")
    assert gw_state.get_commercial_context() == {}
    # A lower version now accepts (namespace reset), no longer rejected as stale.
    assert gw_state.set_commercial_context(_ctx(version=1, mode="partner_managed")) is True
    assert gw_state.get_commercial_context()["version"] == 1


# -- A5 app money bound ------------------------------------------------------

def test_validated_offer_rejects_zero_and_over_max():
    """The app offer validator rejects an amount of 0 (lower bound) and one at
    or over _MAX_RETAIL_AMOUNT (ceiling); a valid amount is kept."""
    for bad in (0, gw_state._MAX_RETAIL_AMOUNT, gw_state._MAX_RETAIL_AMOUNT + 1):
        offer = {**_offer(), "retail_amount": bad}
        assert gw_state._validated_offer(offer) is None, f"amount={bad!r} not rejected"
    # A valid amount inside the bound is kept.
    assert gw_state._validated_offer(_offer()) is not None
    assert gw_state._validated_offer({**_offer(), "retail_amount": 1}) is not None
