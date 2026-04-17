# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for ui/components/cloud_gate.py — 100% line coverage."""

from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from ui.components.cloud_gate import _SUBSCRIBE_BASE, cloud_gate, upgrade_banner


def _render(ft) -> str:
    """Render a FastHTML FT node to HTML string."""
    from fasthtml.common import to_xml
    return to_xml(ft)


def test_upgrade_banner_contains_feature_name():
    html = _render(upgrade_banner("Cloud Relay", "Get a public URL."))
    assert "Cloud Relay requires Celerp Cloud" in html


def test_upgrade_banner_contains_description():
    html = _render(upgrade_banner("Cloud Relay", "Get a public URL."))
    assert "Get a public URL." in html


def test_upgrade_banner_default_price():
    html = _render(upgrade_banner("Cloud Relay", "desc"))
    assert "$29/mo" in html


def test_upgrade_banner_custom_price():
    html = _render(upgrade_banner("Cloud AI", "desc", price="$49/mo"))
    assert "$49/mo" in html


def test_upgrade_banner_no_anchor():
    html = _render(upgrade_banner("Feature", "desc"))
    assert _SUBSCRIBE_BASE in html
    assert "#" not in html.split(_SUBSCRIBE_BASE)[1].split('"')[0]  # no anchor fragment


def test_upgrade_banner_with_anchor():
    html = _render(upgrade_banner("Feature", "desc", anchor="ai"))
    assert _SUBSCRIBE_BASE in html
    # The href should end with #ai (possibly with ?instance_id=... before)
    href_tail = html.split(_SUBSCRIBE_BASE)[1].split('"')[0]
    assert href_tail.endswith("#ai")


def test_cloud_gate_not_connected_returns_banner():
    html = _render(cloud_gate(
        is_connected=False,
        feature="Cloud Relay",
        description="desc",
    ))
    assert "Cloud Relay requires Celerp Cloud" in html


def test_cloud_gate_connected_returns_content():
    from fasthtml.common import P
    content = P("Real UI content")
    html = _render(cloud_gate(
        is_connected=True,
        feature="Cloud Relay",
        description="desc",
        content=content,
    ))
    assert "Real UI content" in html
    assert "requires Celerp Cloud" not in html


def test_cloud_gate_connected_no_content_returns_empty_div():
    html = _render(cloud_gate(
        is_connected=True,
        feature="Cloud Relay",
        description="desc",
        content=None,
    ))
    # Returns empty Div — no banner, no error
    assert "requires Celerp Cloud" not in html
