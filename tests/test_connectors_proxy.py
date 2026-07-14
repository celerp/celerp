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
