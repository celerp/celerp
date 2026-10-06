# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Messages the API returns read in the user's language: the UI sends the language of
the page that asked, and the API answers in it, falling back to English."""

from __future__ import annotations

import pytest

import ui.api_client as api
from test_install_owner_invariant import _two_owners
from ui.i18n import set_lang, t

pytestmark = pytest.mark.asyncio


async def test_api_answers_in_the_requested_language(client, session):
    admin_h, admin_id, second_token, _ = await _two_owners(client, session)
    url, body = f"/companies/me/users/{admin_id}", {"is_active": False}
    h = {"Authorization": f"Bearer {second_token}"}
    r = await client.patch(url, json=body, headers={**h, "Accept-Language": "th"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == t("error.install_owner_deactivate", "th")
    assert r.json()["detail"] != t("error.install_owner_deactivate", "en")
    r = await client.patch(url, json=body, headers={**h, "Accept-Language": "xx"})
    assert r.json()["detail"] == t("error.install_owner_deactivate", "en")


async def test_ui_client_sends_the_page_language():
    set_lang("de")
    try:
        c = api._local_client("tok")
        assert c.headers["Accept-Language"] == "de"
        await c.aclose()
    finally:
        set_lang("en")
