# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Render tests for the GitHub-star UI surfaces (footer CTA, supporter badge,
supporter card). Pure render — no server."""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.components.shell import _topbar, base_shell, star_supporter_card
from ui.routes.auth import _onboarding_view


def test_footer_cta_present_and_gold():
    xml = to_xml(base_shell(title="t"))
    assert 'id="star-cta"' in xml
    assert "/stars/cta?medium=footer" in xml
    assert "/stars/badge" in xml
    # Gold + bold so it stands out from the grey "Powered by" link.
    seg = xml.split('id="star-cta"')[1][:140]
    assert "#d4af37" in seg


def test_supporter_badge_slot_in_topbar_for_user():
    # The badge slot lives in the user-menu, which renders only when logged in.
    xml = to_xml(_topbar([], user_email="admin@test.local"))
    assert 'id="supporter-badge"' in xml


def test_supporter_card_component():
    xml = to_xml(star_supporter_card("dashboard"))
    assert 'id="star-supporter-card"' in xml
    assert "/stars/cta?medium=dashboard" in xml
    assert "/stars/claim" in xml             # claim handshake link
    assert "/stars/dismiss" in xml
    # Dismiss is now an X button (top-right), not a "Maybe later" button.
    assert 'id="star-card-dismiss"' in xml
    assert "Maybe later" not in xml
    assert "×" in xml
    # The ask is the header; the explanation + founding-badge promise is the body.
    assert "<h3" in xml and "Star on GitHub" in xml
    assert "first 100 people that star us" in xml
    # Hidden until JS hydrates it (non-neutral + not dismissed).
    assert "display:none" in xml.split('id="star-supporter-card"')[1][:90]


def test_card_medium_is_parameterized():
    assert "/stars/cta?medium=onboarding" in to_xml(star_supporter_card("onboarding"))
    assert "/stars/cta?medium=dashboard" in to_xml(star_supporter_card("dashboard"))


def test_onboarding_view_includes_card():
    assert 'id="star-supporter-card"' in to_xml(_onboarding_view())
