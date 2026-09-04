# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The backup-banner poll must surface an upstream failure as HTTP 503.

`/backup/active` catches any upstream backup-status error and returns a body of
{"state": "error"} with a DEFAULT 200 status. To the poller, a failed backend
read is then indistinguishable from a healthy 200 response, so an outage looks
healthy at the HTTP layer. The route must return 503 on upstream failure so the
observable status the client sees reflects the outage.
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
async def test_backup_active_returns_503_on_upstream_failure(ui_app):
    """With a valid owner token (past the token gate) but a failing upstream
    backup-status read, /backup/active must respond 503 - the status the client
    observes - not a bare 200 that reads as a healthy backend."""
    failing = AsyncMock(side_effect=RuntimeError("backup service unreachable"))
    with patch("ui.api_client.get_backup_status", new=failing):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as c:
            r = await c.get("/backup/active", cookies=_cookies())

    assert r.status_code == 503, (
        f"an upstream backup-status failure must surface as HTTP 503, not "
        f"{r.status_code}; body={r.text}")
