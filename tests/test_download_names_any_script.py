# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Downloads named after a document keep working, and keep the name, in any script.

A document number is user text (a numbering prefix, or a number carried over from
another system), so it can be in any script: every name in ACCOUNT_NAMES is tried. HTTP headers are latin-1: a name outside
it must travel as RFC 5987 ``filename*``, or building the response fails outright.
"""
from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import unquote

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import ACCOUNT_NAMES, make_test_token

REFS = [f"{name}-0001" for name in ACCOUNT_NAMES.values()]


def _sent_name(cd: str) -> str:
    """The file name a browser takes from a Content-Disposition header (RFC 6266:
    filename* wins over filename)."""
    params = dict(p.strip().split("=", 1) for p in cd.split(";")[1:] if "=" in p)
    if "filename*" in params:
        charset, _, value = params["filename*"].split("'", 2)
        assert charset.lower() == "utf-8"
        return unquote(value)
    return params.get("filename", "").strip('"')


async def _doc(client: AsyncClient, ref: str) -> tuple[dict, str]:
    r = await client.post("/auth/register", json={"company_name": "NameCo", "email": "names@test.example",
                                                  "name": "Admin", "password": "pwvalid1"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "ref_id": ref, "contact_name": "ACME",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 100.0}],
        "subtotal": 100.0, "total": 100.0, "currency": "THB"})
    assert r.status_code == 200, r.text
    return h, r.json()["id"]


@pytest.mark.parametrize("ref", REFS)
@pytest.mark.asyncio
async def test_document_pdf_named_by_its_number(client, ref):
    """RED before the change for names outside latin-1: the raw filename= header cannot
    be encoded, so the PDF download fails with a server error."""
    h, entity_id = await _doc(client, ref)
    r = await client.get(f"/docs/{entity_id}/pdf", headers=h)
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    assert r.headers["content-disposition"].startswith("inline;")
    assert _sent_name(r.headers["content-disposition"]) == f"{ref}.pdf".replace(" ", "_")


@pytest.mark.parametrize("ref", REFS)
@pytest.mark.asyncio
async def test_shared_document_bundle_named_by_its_number(client, ref):
    """RED before the change: same header failure on the shared-document bundle."""
    h, entity_id = await _doc(client, ref)
    token = (await client.post(f"/docs/{entity_id}/share", headers=h)).json()["token"]
    r = await client.get(f"/share/{token}/bundle")
    assert r.status_code == 200
    assert _sent_name(r.headers["content-disposition"]) == f"{ref}.celerp"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


@pytest.mark.parametrize("ref", REFS)
@pytest.mark.parametrize("kind", ["docs", "lists"])
@pytest.mark.asyncio
async def test_line_items_csv_named_by_its_number(ui_client, monkeypatch, kind, ref):
    """RED before the change: same header failure on the line-items CSV export."""
    import ui.api_client as api

    monkeypatch.setattr(api, "get_doc", AsyncMock(return_value={
        "ref_id": ref, "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 100}]}))
    r = await ui_client.get(f"/{kind}/doc:1/items/csv", cookies={"celerp_token": make_test_token(role="owner")})
    assert r.status_code == 200
    assert _sent_name(r.headers["content-disposition"]) == f"{ref}_items.csv".replace(" ", "_")


@pytest.mark.parametrize("name", list(ACCOUNT_NAMES.values()))
@pytest.mark.asyncio
async def test_company_backup_named_for_a_company_in_any_script(client, monkeypatch, name):
    """The company backup is named by the company's web address, which is plain ASCII
    whatever script the company name is in, so the header always encodes."""
    import re

    import celerp.services.company_backup as cb

    async def export(_company_id, dest, provenance=None):
        dest.write_bytes(b"backup")

    monkeypatch.setattr(cb, "export_company_snapshot", export)
    r = await client.post("/auth/register", json={"company_name": name, "email": "names@test.example",
                                                  "name": name, "password": "pwvalid1"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.get("/company-backups/download", headers=h)
    assert r.status_code == 200, r.text
    assert r.content == b"backup"
    sent = _sent_name(r.headers["content-disposition"])
    assert re.fullmatch(r"[a-z0-9-]+\.celerp-company", sent), sent
