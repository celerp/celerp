# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A file uploaded to a page is closed once the request is answered, whether the page
takes it or the visitor is sent to sign in first, so no temporary file stays open."""
from __future__ import annotations

import io
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.datastructures import UploadFile

from test_helpers import authed_cookies
from test_import_invariants import _company, stage_dir  # noqa: F401


@pytest.fixture
def uploads(monkeypatch) -> list:
    """Every UploadFile the request makes."""
    made: list = []
    original_init = UploadFile.__init__

    def _init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        made.append(self)

    monkeypatch.setattr(UploadFile, "__init__", _init)
    return made


@pytest.mark.parametrize("signed_in, status", [(True, 200), (False, 302)])
async def test_uploaded_file_is_closed_when_the_request_ends(stage_dir, uploads, signed_in, status):  # noqa: F811
    from ui.app import app as ui_app
    company = _company({})
    with patch("ui.routes.auth.api_get_company", new=AsyncMock(return_value=company)), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value=company)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app), base_url="http://ui") as c:
            if signed_in:
                c.cookies.update(authed_cookies(role="owner"))
            r = await c.post("/docs/import/preview",
                             files={"csv_file": ("orders.csv", io.BytesIO(b"Ref\nA-1\n"), "text/csv")})
    assert r.status_code == status, r.text
    assert len(uploads) == 1
    assert uploads[0].file.closed
