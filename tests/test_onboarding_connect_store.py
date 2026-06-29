# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The onboarding landing surfaces a featured 'Connect your store' card that leads to
the cloud claim. For an App-Store-acquired Shopify merchant, claiming by email there
binds their store (then it auto-syncs); for direct users it links their subscription."""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.routes.auth import _onboarding_view


def test_onboarding_shows_featured_connect_store_card():
    out = to_xml(_onboarding_view())
    # Featured card links to the existing cloud-claim flow.
    assert 'href="/settings/cloud"' in out
    assert "quick-link-card--featured" in out
    # i18n resolved (not raw keys).
    assert "Connect your store" in out
    assert "page.connect_your_store" not in out
    assert "msg.connect_store_desc" not in out
    # Still appears alongside the existing import shortcuts.
    assert "/onboarding/upload/items" in out
