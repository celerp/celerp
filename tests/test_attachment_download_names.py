# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An attachment downloaded through the UI keeps its own file name, in any script.

The API serves attachments with Starlette's FileResponse, which sends a name outside
latin-1 only as the RFC 5987 ``filename*=utf-8''...`` parameter. The UI download
routes forward the file to the browser and must carry that name through.
"""
from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import unquote

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.responses import FileResponse

from test_helpers import make_test_token

THAI = "ใบแจ้งหนี้.pdf"
ARABIC = "فاتورة.pdf"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _api_reply(tmp_path, name: str) -> httpx.Response:
    """The API's reply for an attachment called ``name``, headers as FileResponse sends them."""
    path = tmp_path / "blob"
    path.write_bytes(b"%PDF-1.4 test")
    cd = FileResponse(path, filename=name, media_type="application/pdf").headers["content-disposition"]
    return httpx.Response(200, content=path.read_bytes(),
                          headers={"content-type": "application/pdf", "content-disposition": cd})


def _sent_name(cd: str) -> str:
    """The file name a browser takes from a Content-Disposition header (RFC 6266:
    filename* wins over filename)."""
    params = dict(p.strip().split("=", 1) for p in cd.split(";")[1:] if "=" in p)
    if "filename*" in params:
        charset, _, value = params["filename*"].split("'", 2)
        assert charset.lower() == "utf-8"
        return unquote(value)
    return params.get("filename", "").strip('"')


@pytest.mark.parametrize("route,api_fn,disposition", [
    ("/items/item:1/files/f1/download", "download_item_file", "inline"),
    ("/contacts/c1/files/f1/download", "download_contact_file", "attachment"),
    ("/docs/d1/files/f1/download", "download_doc_file", "attachment"),
])
@pytest.mark.parametrize("name", [THAI, ARABIC, "invoice.pdf"])
@pytest.mark.asyncio
async def test_download_keeps_the_attachment_name(ui_client, monkeypatch, tmp_path, route, api_fn,
                                                  disposition, name):
    """RED before the change for non-latin-1 names on items and contacts: the routes
    read only ``filename=`` and sent the file as "download"."""
    import ui.api_client as api

    monkeypatch.setattr(api, api_fn, AsyncMock(return_value=_api_reply(tmp_path, name)))
    r = await ui_client.get(route, cookies={"celerp_token": make_test_token(role="owner")})
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 test"
    cd = r.headers["content-disposition"]
    assert cd.split(";")[0] == disposition
    assert _sent_name(cd) == name
