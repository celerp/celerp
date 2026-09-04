# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The backup-banner poll must tell the truth about an upstream failure.

`/backup/active` collapsed any upstream error into {"active": false}, which is
indistinguishable from a healthy idle backend. The banner then silently hides an
outage. The endpoint must surface an upstream failure as a distinct state so the
client can tell "no backup running" apart from "could not reach the backup
service".
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token


@pytest.fixture
def ui_app():
    from ui.app import app as _app
    return _app


def _cookies() -> dict:
    return {"celerp_token": make_test_token(role="owner")}


@pytest.mark.asyncio
async def test_backup_active_reports_error_state(ui_app):
    """When the upstream backup-status call raises, /backup/active must report a
    distinct error/unknown state, never a bare {"active": false} that reads as a
    healthy idle backend."""
    failing = AsyncMock(side_effect=RuntimeError("backup service unreachable"))
    with patch("ui.api_client.get_backup_status", new=failing):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as c:
            r = await c.get("/backup/active", cookies=_cookies())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body != {"active": False}, (
        "an upstream failure must not masquerade as a healthy idle backend")
    assert body.get("error") or body.get("state") in ("error", "unknown"), (
        f"the failure must surface as a distinct error/unknown state, got {body}")
