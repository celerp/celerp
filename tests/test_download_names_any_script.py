# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Downloads named after a document keep working, and keep the name, in any script.

A document number is user text (a numbering prefix, or a number carried over from
another system), so it can be Thai or Arabic. HTTP headers are latin-1: a name outside
it must travel as RFC 5987 ``filename*``, or building the response fails outright.
"""
from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import unquote

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token

THAI_REF = "ใบแจ้งหนี้-0001"


def _sent_name(cd: str) -> str:
    """The file name a browser takes from a Content-Disposition header (RFC 6266:
    filename* wins over filename)."""
    params = dict(p.strip().split("=", 1) for p in cd.split(";")[1:] if "=" in p)
    if "filename*" in params:
        charset, _, value = params["filename*"].split("'", 2)
        assert charset.lower() == "utf-8"
        return unquote(value)
    return params.get("filename", "").strip('"')


async def _thai_doc(client: AsyncClient) -> tuple[dict, str]:
    r = await client.post("/auth/register", json={"company_name": "ThaiCo", "email": "thai@test.example",
                                                  "name": "Admin", "password": "pwvalid1"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "ref_id": THAI_REF, "contact_name": "ACME",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 100.0}],
        "subtotal": 100.0, "total": 100.0, "currency": "THB"})
    assert r.status_code == 200, r.text
    return h, r.json()["id"]


@pytest.mark.asyncio
async def test_document_pdf_named_by_a_thai_number(client):
    """RED before the change: the raw filename= header cannot be encoded, so the PDF
    download fails with a server error."""
    h, entity_id = await _thai_doc(client)
    r = await client.get(f"/docs/{entity_id}/pdf", headers=h)
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    assert r.headers["content-disposition"].startswith("inline;")
    assert _sent_name(r.headers["content-disposition"]) == f"{THAI_REF}.pdf"


@pytest.mark.asyncio
async def test_shared_document_bundle_named_by_a_thai_number(client):
    """RED before the change: same header failure on the shared-document bundle."""
    h, entity_id = await _thai_doc(client)
    token = (await client.post(f"/docs/{entity_id}/share", headers=h)).json()["token"]
    r = await client.get(f"/share/{token}/bundle")
    assert r.status_code == 200
    assert _sent_name(r.headers["content-disposition"]) == f"{THAI_REF}.celerp"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


@pytest.mark.parametrize("kind", ["docs", "lists"])
@pytest.mark.asyncio
async def test_line_items_csv_named_by_a_thai_number(ui_client, monkeypatch, kind):
    """RED before the change: same header failure on the line-items CSV export."""
    import ui.api_client as api

    monkeypatch.setattr(api, "get_doc", AsyncMock(return_value={
        "ref_id": THAI_REF, "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 100}]}))
    r = await ui_client.get(f"/{kind}/doc:1/items/csv", cookies={"celerp_token": make_test_token(role="owner")})
    assert r.status_code == 200
    assert _sent_name(r.headers["content-disposition"]) == f"{THAI_REF}_items.csv"
