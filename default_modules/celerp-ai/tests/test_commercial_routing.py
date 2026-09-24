# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Commercial-routing gates for the celerp-ai module.

Every commercial CTA the module renders (quota-exceeded topup/upgrade card, the
403 batch-upgrade body, the AI showcase price cards, the quota-status topup
link) must route its destination through the central resolver and suppress
direct Celerp pricing under partner_managed, never a direct checkout.
"""

from __future__ import annotations

import os
import secrets

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

import celerp.gateway.state as gw_state
from test_helpers import authed_cookies


@pytest.fixture(autouse=True)
def _reset_commercial_context():
    gw_state._commercial_context = {}
    yield
    gw_state._commercial_context = {}


def _set_partner(support_url: str = "https://partner.example.com/support"):
    gw_state._commercial_context = {
        "commercial_mode": "partner_managed",
        "implementation": {"display_name": "Partner Co", "support_url": support_url},
        "offer": {"display_name": "Managed Plan", "retail_amount": 4900,
                  "currency": "USD"},
    }


# ── quota-exceeded card (topup + upgrade) ───────────────────────────────────

def test_ai_topup_card_routes_through_policy():
    """partner mode: the low-credit topup card href is not a direct
    /subscribe/topup URL."""
    from celerp_ai.ui_routes import _quota_exceeded_card
    _set_partner()
    detail = {"instance_id": "inst-1", "limit": 100, "tier": "ai"}
    html = to_xml(_quota_exceeded_card(detail, user_bubble="", lang="en"))
    assert "/subscribe/topup" not in html
    assert "/subscribe" not in html


def test_ai_topup_card_direct_unchanged():
    """celerp_direct: the topup card points at the in-app commercial mint route,
    which resolves the direct topup destination and mints a handoff token at click
    (positive control)."""
    from celerp_ai.ui_routes import _quota_exceeded_card
    detail = {"instance_id": "inst-1", "limit": 100, "tier": "ai"}
    html = to_xml(_quota_exceeded_card(detail, user_bubble="", lang="en"))
    assert "/commercial/checkout?intent=topup" in html


def test_ai_upgrade_url_not_preferred():
    """A relay-supplied detail.upgrade_url is never used; the upgrade CTA routes
    through the resolver instead."""
    from celerp_ai.ui_routes import _quota_exceeded_card
    _set_partner()
    detail = {"instance_id": "inst-1", "limit": 100, "tier": "cloud",
              "upgrade_url": "https://celerp.com/subscribe?plan=ai"}
    html = to_xml(_quota_exceeded_card(detail, user_bubble="", lang="en"))
    assert "celerp.com/subscribe" not in html


def test_ai_quota_upgrade_label_not_price_in_partner():
    """partner mode: the upgrade CTA label carries no "$49"."""
    from celerp_ai.ui_routes import _quota_exceeded_card
    _set_partner()
    detail = {"instance_id": "inst-1", "limit": 100, "tier": "cloud"}
    html = to_xml(_quota_exceeded_card(detail, user_bubble="", lang="en"))
    assert "$49" not in html


# ── AI showcase ─────────────────────────────────────────────────────────────

def test_ai_showcase_partner_no_direct_price():
    """partner mode: the AI showcase shows neither "$29" nor "$49"."""
    from celerp_ai.ui_routes import _showcase_view
    _set_partner()
    html = to_xml(_showcase_view(lang="en"))
    assert "$29" not in html
    assert "$49" not in html


def test_ai_showcase_direct_does_not_embed_stale_price():
    """Even direct mode leaves price display to the authoritative plan grid."""
    from celerp_ai.ui_routes import _showcase_view
    html = to_xml(_showcase_view(lang="en"))
    assert "$29" not in html
    assert "$49" not in html


def test_ai_showcase_direct_surfaces_existing_subscriber_recovery():
    """The exact #332 recovery door is visible where a paid user sees the
    locked AI showcase: direct subscribers can jump to the shared account/link
    surface instead of being offered only another purchase.
    """
    from celerp_ai.ui_routes import _showcase_view
    html = to_xml(_showcase_view(lang="en"))
    assert "Already subscribed" in html
    assert "Link by email" in html
    assert 'href="/settings/cloud"' in html


def test_ai_showcase_partner_does_not_offer_direct_email_claim():
    """Partner-managed billing must not expose the direct-Celerp claim CTA."""
    from celerp_ai.ui_routes import _showcase_view
    _set_partner()
    html = to_xml(_showcase_view(lang="en"))
    assert 'href="/settings/cloud"' not in html


# ── quota-status proxy topup_url injection ──────────────────────────────────

@pytest_asyncio.fixture
async def auth_client():
    """UI-app client with the celerp-ai module routes registered; the
    quota-status route lives on the FastHTML UI app and reads its token from the
    auth cookie."""
    from ui.app import app as ui_app
    from celerp_ai.ui_routes import setup_ui_routes
    setup_ui_routes(ui_app)
    async with AsyncClient(transport=ASGITransport(app=ui_app),
                           base_url="http://ui", follow_redirects=False) as c:
        yield c


@pytest.mark.asyncio
async def test_ai_quota_status_topup_url_policy(auth_client):
    """The UI quota-status proxy injects a resolver-computed topup_url; under
    partner_managed it is not a direct /subscribe/topup URL."""
    _set_partner()
    mock_status = {"used": 15, "limit": 200, "topup_credits": 0,
                   "resets_at": "", "tier": "ai"}
    with patch("celerp_ai.ui_routes.api.ai_quota_status",
               AsyncMock(return_value=mock_status | {"instance_id": "inst-1"})):
        r = await auth_client.get("/ai/quota-status", cookies=authed_cookies())
    data = r.json()
    assert "topup_url" in data
    assert "/subscribe/topup" not in data["topup_url"]


@pytest.mark.asyncio
async def test_ai_quota_status_topup_url_direct(auth_client):
    """celerp_direct: the injected topup_url is the in-app commercial mint route,
    which resolves the direct topup destination at click (positive control)."""
    mock_status = {"used": 15, "limit": 200, "topup_credits": 0,
                   "resets_at": "", "tier": "ai"}
    with patch("celerp_ai.ui_routes.api.ai_quota_status",
               AsyncMock(return_value=mock_status | {"instance_id": "inst-1"})):
        r = await auth_client.get("/ai/quota-status", cookies=authed_cookies())
    data = r.json()
    assert "/commercial/checkout?intent=topup" in data["topup_url"]


# ── AI-api 401 body (celerp/modules/api.py) ─────────────────────────────────

@pytest.mark.asyncio
async def test_ai_api_401_is_transport_neutral_in_partner_mode(monkeypatch):
    """Missing service connection never becomes purchase advice."""
    from fastapi import HTTPException
    from celerp.modules.api import ai_query
    _set_partner()
    monkeypatch.setattr("celerp.gateway.state.get_session_token", lambda: "")
    with pytest.raises(HTTPException) as exc:
        await ai_query(query="hi", company_id="c1", session_token="", db_session=None)
    assert exc.value.status_code == 401
    detail = str(exc.value.detail)
    assert "celerp.com/subscribe" not in detail
    assert "partner.example.com" not in detail
    assert "Settings > Web Access" in detail


@pytest.mark.asyncio
async def test_ai_api_401_is_transport_neutral_in_direct_mode(monkeypatch):
    from fastapi import HTTPException
    from celerp.modules.api import ai_query
    monkeypatch.setattr("celerp.gateway.state.get_session_token", lambda: "")
    with pytest.raises(HTTPException) as exc:
        await ai_query(query="hi", company_id="c1", session_token="", db_session=None)
    assert exc.value.status_code == 401
    detail = str(exc.value.detail)
    assert "celerp.com/subscribe" not in detail
    assert "Settings > Web Access" in detail
