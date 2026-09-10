# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for ui/components/cloud_gate.py — 100% line coverage."""

from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from ui.components.cloud_gate import cloud_gate, upgrade_banner

# The subscribe CTA now points at the in-app commercial mint route (which mints a
# handoff token and bounces to the real checkout at click), not at celerp.com
# directly; the plan travels as the ``sku`` query param.
_MINT_ROUTE = "/commercial/checkout?intent=subscribe"


def _render(ft) -> str:
    """Render a FastHTML FT node to HTML string."""
    from fasthtml.common import to_xml
    return to_xml(ft)


def test_upgrade_banner_contains_feature_name():
    html = _render(upgrade_banner("Encrypted Backup", "Get a public URL."))
    assert "Encrypted Backup requires Celerp Connect" in html


def test_upgrade_banner_contains_description():
    html = _render(upgrade_banner("Encrypted Backup", "Get a public URL."))
    assert "Get a public URL." in html


def test_upgrade_banner_default_price():
    html = _render(upgrade_banner("Encrypted Backup", "desc"))
    assert "$29/mo" in html


def test_upgrade_banner_custom_price():
    html = _render(upgrade_banner("Encrypted Backup", "desc", price="$49/mo"))
    assert "$49/mo" in html


def test_upgrade_banner_no_plan():
    html = _render(upgrade_banner("Feature", "desc"))
    assert _MINT_ROUTE in html
    href_tail = html.split(_MINT_ROUTE)[1].split('"')[0]
    assert "#" not in href_tail and "sku=" not in href_tail


def test_upgrade_banner_with_plan():
    html = _render(upgrade_banner("Feature", "desc", plan="ai"))
    assert _MINT_ROUTE in html
    # plan travels as the sku query param (server-visible), never a fragment
    href_tail = html.split(_MINT_ROUTE)[1].split('"')[0]
    assert "sku=ai" in href_tail and "#" not in href_tail


def test_cloud_gate_not_connected_returns_banner():
    html = _render(cloud_gate(
        is_connected=False,
        feature="Encrypted Backup",
        description="desc",
    ))
    assert "Encrypted Backup requires Celerp Connect" in html


def test_cloud_gate_connected_returns_content():
    from fasthtml.common import P
    content = P("Real UI content")
    html = _render(cloud_gate(
        is_connected=True,
        feature="Encrypted Backup",
        description="desc",
        content=content,
    ))
    assert "Real UI content" in html
    assert "requires Celerp Connect" not in html


def test_cloud_gate_connected_no_content_returns_empty_div():
    html = _render(cloud_gate(
        is_connected=True,
        feature="Encrypted Backup",
        description="desc",
        content=None,
    ))
    # Returns empty Div — no banner, no error
    assert "requires Celerp Connect" not in html
