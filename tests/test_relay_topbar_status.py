# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression tests for relay-aware shell chrome."""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.components.shell import (
    _STAR_CTA_JS,
    _relay_info_from_request,
    _topbar,
    star_supporter_card,
)


class _Request:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def test_relay_request_uses_forwarded_customer_host_as_known_connection():
    info = _relay_info_from_request(_Request({
        "x-forwarded-host": "demo.celerp.com",
        "x-forwarded-proto": "https",
    }))
    assert info == {
        "connected": True,
        "public_url": "https://demo.celerp.com",
    }


def test_direct_request_keeps_relay_state_unknown():
    assert _relay_info_from_request(_Request({})) is None


def test_known_relay_topbar_does_not_reprobe_status():
    xml = to_xml(_topbar(
        [],
        user_email="admin@test.local",
        relay_info={
            "connected": True,
            "public_url": "https://demo.celerp.com",
        },
    ))
    assert 'hx-get="/topbar-relay-status"' not in xml
    assert "relay-dot--on" in xml
    assert 'href="https://demo.celerp.com"' in xml


def test_unknown_relay_topbar_probe_is_quiet():
    xml = to_xml(_topbar([], user_email="admin@test.local"))
    assert 'hx-get="/topbar-relay-status"' in xml
    assert 'data-quiet-error="1"' in xml


def test_decorative_star_hydration_is_shared_and_serialized():
    assert "window.celerpStarFetch" in _STAR_CTA_JS
    assert "window.addEventListener('load'" in _STAR_CTA_JS
    card = to_xml(star_supporter_card("dashboard"))
    assert "celerpStarFetch('/stars/cta?medium=dashboard')" in card
    assert "celerpStarFetch('/stars/badge')" in card
    assert "fetch('/stars/badge')" not in card
