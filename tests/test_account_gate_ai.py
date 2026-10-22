# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The account gate for Celerp AI (kind ``ai``).

The AI page's Create button opens the shared sign-up gate staging ``next=ai``.
The gate names the free account's included questions, keeps that count when a
sign-up error re-renders it, and once the account verifies it reloads /ai.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from test_helpers import authed_cookies
from ui.routes.account import _next_from, _resume_parts

_METHODS = {"google": False, "free_email_quota": 10, "free_ai_questions": 20}
_AI_BENEFIT = "Ask Celerp AI 20 questions free"


@pytest.fixture()
async def ui():
    from ui.app import app as ui_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                                 base_url="http://ui", follow_redirects=False) as c:
        c.cookies.update(authed_cookies(role="owner"))
        with patch("ui.routes.account._account_allowed", new=AsyncMock(return_value=True)):
            yield c


def test_next_ai_accepted():
    assert _next_from("ai") == "ai"
    assert _next_from("ai:x") is None


async def test_ai_gate_benefit_shows_free_count(ui):
    with patch("ui.api_client.account_methods", new=AsyncMock(return_value=_METHODS)):
        r = await ui.get("/account/panel?intent=signup&panel=account-gate-panel&next=ai&modal=1")
    html = r.text
    assert 'value="ai"' in html
    assert _AI_BENEFIT in html
    assert "account-panel__benefit" in html

    # Unknown count: the generic hint, never "0 questions" or a guessed number.
    no_count = dict(_METHODS, free_ai_questions=None)
    with patch("ui.api_client.account_methods", new=AsyncMock(return_value=no_count)):
        r = await ui.get("/account/panel?intent=signup&panel=account-gate-panel&next=ai&modal=1")
    assert "questions" not in r.text
    assert "account-panel__lead" in r.text


async def test_ai_gate_error_path_keeps_kind_count(ui):
    with (
        patch("ui.api_client.installation_owner", new=AsyncMock(return_value=True)),
        patch("ui.api_client.account_signup",
              new=AsyncMock(return_value={"error": "That email address cannot be used."})),
        patch("ui.api_client.account_methods", new=AsyncMock(return_value=_METHODS)),
    ):
        r = await ui.post("/account/email?panel=account-gate-panel",
                          data={"email": "o@shop.example", "next": "ai"})
    html = r.text
    assert "That email address cannot be used." in html
    assert _AI_BENEFIT in html
    assert "10 documents" not in html


def test_ai_resume_reloads_ai():
    from fasthtml.common import to_xml
    script = "".join(to_xml(p) for p in _resume_parts("ai"))
    assert "celerp_resume::" in script and '"ai"' in script
    assert "location.assign('/ai')" in script
