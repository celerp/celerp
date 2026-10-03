# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The getting-started hub offers connecting another system as one option among
the imports, never as a featured first step. A merchant who installed from Shopify
still finds the cloud link that binds their store."""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.routes.auth import _ONBOARDING_ACTIONS, _onboarding_view


def _hub() -> str:
    return to_xml(_onboarding_view({path for path, *_ in _ONBOARDING_ACTIONS}))


def test_connect_option_links_to_the_cloud_claim_flow():
    out = _hub()
    assert 'href="/settings/cloud?tab=website"' in out
    assert "Connect an online store or accounting system" in out
    assert "Shopify" in out


def test_connect_option_is_not_featured_over_imports():
    out = _hub()
    assert "quick-link-card--featured" not in out
    assert out.index("/inventory/import") < out.index("/settings/cloud")


def _connect_card() -> str:
    from ui.routes.auth import _onboarding_view
    return to_xml(_onboarding_view({"/settings/cloud"})).lower()


def test_connect_option_is_not_described_as_moving_the_business():
    out = _connect_card()
    assert "migrat" not in out and "move from another system" not in out
