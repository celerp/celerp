# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""AI module test fixtures."""
from __future__ import annotations

import sys
import os

import pytest_asyncio

# Guarantee celerp-ai src is on sys.path before any test imports.
_ai_src = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "default_modules", "celerp-ai")
)
if _ai_src not in sys.path:
    sys.path.insert(0, _ai_src)


@pytest_asyncio.fixture
async def auth_client(client, drained_jobs):
    """The root ``client`` (AI job tasks join the test's transaction) signed in as a
    new company's admin on an installation with a seated Connect session, which the
    request sends too. The client fixture restores the session token afterwards. Jobs
    a test started are drained before the client closes."""
    from celerp.gateway.state import set_session_token
    from test_helpers import register_admin
    session_token = "s" * 64
    set_session_token(session_token)
    token = await register_admin(client)
    yield client, {"Authorization": f"Bearer {token}", "X-Session-Token": session_token}
