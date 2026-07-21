# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests pinning the connector relay-call boundary: the UI process holds no relay
session (the gateway WebSocket client lives in the API process), so every relay
credential/token operation must go through the API process proxy endpoints."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def test_ui_never_uses_relay_session_headers():
    """relay_session_headers() reads gateway state that is only populated in the
    API process; calling it from UI code sends an empty session token and the
    relay rejects the request. Pin: UI code must proxy via ui.api_client instead."""
    offenders = [
        str(p.relative_to(UI_DIR.parent))
        for p in UI_DIR.rglob("*.py")
        if "relay_session_headers" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.asyncio
async def test_fetch_access_token_returns_relay_payload():
    from ui.routes.settings_connectors import _fetch_access_token
    payload = {"access_token": "tok", "store_handle": "s"}
    with patch("ui.api_client.get_connector_access_token", new=AsyncMock(return_value=payload)):
        assert await _fetch_access_token("woocommerce", "jwt") == payload


@pytest.mark.asyncio
async def test_fetch_access_token_raises_on_error_code():
    from ui.routes.settings_connectors import _fetch_access_token
    err = {"error": "not_connected", "detail": "No woocommerce connection found."}
    with patch("ui.api_client.get_connector_access_token", new=AsyncMock(return_value=err)):
        with pytest.raises(RuntimeError, match="No woocommerce connection found"):
            await _fetch_access_token("woocommerce", "jwt")


@pytest.mark.asyncio
async def test_connectors_tab_shows_trial_cta_when_relay_gates_on_plan():
    """A relay 402 (free account) must render the trial CTA on the connectors
    tab, not the generic could-not-load network message."""
    from ui.routes.settings_connectors import connectors_tab_content

    with patch("ui.routes.settings_connectors._fetch_catalog",
               AsyncMock(return_value=([], "Connectors need an active plan.", True))), \
         patch("celerp.config.ensure_instance_id", return_value="iid-x"):
        panel = await connectors_tab_content("en", token="tok")

    from fasthtml.common import to_xml
    html = to_xml(panel)
    assert "/settings/cloud" in html                       # the upgrade CTA link
    assert "connector-entitlement-cta" in html
    assert "Could not load connectors" not in html


@pytest.mark.asyncio
async def test_connectors_tab_shows_fetch_error_detail():
    """A non-entitlement failure shows the actual error detail (e.g. relay
    unreachable), so it reads as what it is instead of a generic string."""
    from ui.routes.settings_connectors import connectors_tab_content

    with patch("ui.routes.settings_connectors._fetch_catalog",
               AsyncMock(return_value=([], "Relay timed out.", False))), \
         patch("celerp.config.ensure_instance_id", return_value="iid-x"):
        panel = await connectors_tab_content("en", token="tok")

    from fasthtml.common import to_xml
    html = to_xml(panel)
    assert "Relay timed out." in html
    assert "connector-entitlement-cta" not in html
