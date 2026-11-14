# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A file change on a document or a list redraws that record's own files section.

The same UI handlers serve /docs/{id}/files and /lists/{id}/files. Each door must send the
change to its own API resource, read the record back from it, and return the files section
whose controls post to the same door.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token

_FILE = {"id": "f1", "filename": "survey-notes.pdf", "content_type": "application/pdf", "size": 10}

_ACTIONS = {
    "upload": ("post", "", "upload_doc_file", {"files": {"file": ("survey-notes.pdf", b"%PDF-1.4", "application/pdf")}}),
    "delete": ("delete", "/f1", "delete_doc_file", {}),
    "tag": ("post", "/f1/tag", "tag_doc_file", {"data": {"document_tag": "contract"}}),
    "description": ("post", "/f1/description", "patch_doc_file_description", {"data": {"description": "Signed"}}),
}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        yield c


@pytest.mark.parametrize("door,entity_id,kind", [("docs", "doc:INV-1", "doc"), ("lists", "list:QUO-1", "list")])
@pytest.mark.parametrize("action", sorted(_ACTIONS))
@pytest.mark.asyncio
async def test_file_change_redraws_the_section_of_its_own_door(ui_client, monkeypatch, door, entity_id, kind, action):
    import ui.api_client as api

    method, suffix, api_fn, body = _ACTIONS[action]
    record = {"entity_id": entity_id, "entity_type": kind, "ref_id": entity_id.split(":")[1], "files": [_FILE]}
    change = AsyncMock(return_value={})
    get_list, get_doc = AsyncMock(return_value=record), AsyncMock(return_value=record)
    monkeypatch.setattr(api, api_fn, change)
    monkeypatch.setattr(api, "get_list", get_list)
    monkeypatch.setattr(api, "get_doc", get_doc)

    r = await getattr(ui_client, method)(f"/{door}/{entity_id}/files{suffix}", headers={"HX-Request": "true"}, **body)

    assert r.status_code == 200
    assert change.await_args.kwargs == ({"resource": "lists"} if door == "lists" else {})
    assert (get_list if door == "lists" else get_doc).await_count == 1
    assert (get_doc if door == "lists" else get_list).await_count == 0
    assert "survey-notes.pdf" in r.text
    assert f"/{door}/{entity_id}/files" in r.text
    assert f"/{door}/{entity_id}" in r.text.replace(f"/{door}/{entity_id}/files", "")
