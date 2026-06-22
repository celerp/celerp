# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Render tests for the GitHub-star UI surfaces (footer CTA, supporter badge,
onboarding card). Pure render — no server."""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.components.shell import _topbar, base_shell
from ui.routes.auth import _onboarding_view


def test_footer_cta_present_in_shell():
    xml = to_xml(base_shell(title="t"))
    # Footer CTA hydration target + the proxy endpoints the head script fetches.
    assert 'id="star-cta"' in xml
    assert "/stars/cta?medium=footer" in xml
    assert "/stars/badge" in xml


def test_supporter_badge_slot_in_topbar_for_user():
    # The badge slot lives in the user-menu, which renders only when logged in.
    xml = to_xml(_topbar([], user_email="admin@test.local"))
    assert 'id="supporter-badge"' in xml


def test_onboarding_card_present():
    xml = to_xml(_onboarding_view())
    assert 'id="star-onboarding-card"' in xml
    assert "/stars/cta?medium=onboarding" in xml
    assert "/stars/claim" in xml          # claim handshake link
    assert 'id="star-card-dismiss"' in xml  # "Maybe later"
    assert "/stars/dismiss" in xml


def test_onboarding_card_hidden_by_default():
    # Card is display:none until JS hydrates it (and only if not dismissed).
    xml = to_xml(_onboarding_view())
    assert "display:none" in xml.split('id="star-onboarding-card"')[1][:80]
