# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document's and a list's text fields (terms, customer note) save through one handler: a
document's to the document, a list's to the list."""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "saver", "answer", "version"), [
    ("/docs/doc:inv-1/field/terms_text", "patch_doc", {"event_id": 7}, 7),
    ("/lists/list:l-1/field/terms_text", "patch_list", {"version": 4}, 4),
])
async def test_a_text_field_is_saved_to_its_own_record(url, saver, answer, version):
    from ui.app import app as ui_app
    client = AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui")
    client.cookies.update({"celerp_token": make_test_token(role="owner")})
    save = AsyncMock(return_value=answer)
    with patch(f"ui.api_client.{saver}", new=save):
        r = await client.post(url, data={"terms_text": "Net 30"})
    assert r.status_code == 204, r.text
    save.assert_awaited_once()
    assert save.await_args.args[1:] == (url.split("/")[2], {"terms_text": "Net 30"})
    assert f'"version": {version}' in r.headers["HX-Trigger"]


@pytest.mark.asyncio
async def test_a_list_text_field_save_is_stored_on_the_list(owner_ui):
    """Through the real API: the list keeps the saved terms, and the answer names the list's
    new version for the page's next save."""
    from line_actions_support import quotation, state

    list_id = await quotation(owner_ui.api, {})
    before = (await state(owner_ui.api, {}, list_id))["version"]
    r = await owner_ui.post(f"/lists/{list_id}/field/terms_text", data={"terms_text": "Net 30"})
    assert r.status_code == 204, r.text
    stored = await state(owner_ui.api, {}, list_id)
    assert stored["terms_text"] == "Net 30" and stored["version"] > before
    assert f'"version": {stored["version"]}' in r.headers["HX-Trigger"]
