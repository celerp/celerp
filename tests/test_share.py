# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for document share link generation and public read-only view."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _token(client: AsyncClient) -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": "ShareCo", "email": "share@test.com", "name": "Admin", "password": "pw"},
    )
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _doc_payload(doc_type: str = "invoice") -> dict:
    return {
        "doc_type": doc_type,
        "contact_name": "ACME Corp",
        "line_items": [
            {"description": "Widget A", "sku": "WGT-001", "quantity": 2, "unit_price": 500.0}
        ],
        "subtotal": 1000.0,
        "tax": 70.0,
        "total": 1070.0,
        "currency": "THB",
    }


async def _create_doc(client: AsyncClient, tok: str, doc_type: str = "invoice") -> str:
    r = await client.post("/docs", json=_doc_payload(doc_type), headers=_h(tok))
    assert r.status_code == 200
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Share link generation (authenticated)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_create_share_link(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r = await client.post(f"/docs/{entity_id}/share", headers=_h(tok))
    assert r.status_code == 200
    data = r.json()
    assert "token" in data
    assert len(data["token"]) >= 20


@pytest.mark.anyio
async def test_create_share_link_idempotent(client: AsyncClient):
    """Calling share twice returns the same token."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r1 = await client.post(f"/docs/{entity_id}/share", headers=_h(tok))
    r2 = await client.post(f"/docs/{entity_id}/share", headers=_h(tok))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["token"] == r2.json()["token"]


@pytest.mark.anyio
async def test_create_share_link_requires_auth(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r = await client.post(f"/docs/{entity_id}/share")
    assert r.status_code in (401, 403)


@pytest.mark.anyio
async def test_create_share_link_not_found(client: AsyncClient):
    tok = await _token(client)
    r = await client.post("/docs/nonexistent-doc-id/share", headers=_h(tok))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Public share view
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_public_share_view_returns_html(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "Invoice" in body or "invoice" in body
    assert "ACME Corp" in body
    assert "WGT-001" in body


@pytest.mark.anyio
async def test_public_share_view_contains_branding(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert "Powered by Celerp" in r.text
    assert "celerp.com" in r.text


@pytest.mark.anyio
async def test_public_share_view_contains_accept_cta(client: AsyncClient):
    """Invoice share pages must have the Accept & import CTA."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok, "invoice")
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert "Accept this invoice" in r.text
    assert f"/accept?token={token}" in r.text


@pytest.mark.anyio
async def test_public_share_view_no_accept_cta_for_memo(client: AsyncClient):
    """Memos have no Accept CTA."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok, "memo")
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert r.status_code == 200
    assert "Accept this" not in r.text


@pytest.mark.anyio
async def test_public_share_invalid_token_returns_404(client: AsyncClient):
    r = await client.get("/share/totally-invalid-token-xyz")
    assert r.status_code == 404
    assert "celerp.com" in r.text  # always shows helpful content, never a raw error


@pytest.mark.anyio
async def test_public_share_no_auth_required(client: AsyncClient):
    """Public share view works without any auth headers."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    # No auth headers
    r = await client.get(f"/share/{token}")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_revoke_share_link(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    # Confirm it works before revoke
    r = await client.get(f"/share/{token}")
    assert r.status_code == 200

    # Revoke
    rev = await client.delete(f"/docs/{entity_id}/share", headers=_h(tok))
    assert rev.status_code == 200
    assert rev.json()["revoked"] is True

    # Now it should 404
    r2 = await client.get(f"/share/{token}")
    assert r2.status_code == 404


@pytest.mark.anyio
async def test_revoke_nonexistent_share_returns_404(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r = await client.delete(f"/docs/{entity_id}/share", headers=_h(tok))
    assert r.status_code == 404


@pytest.mark.anyio
async def test_after_revoke_new_share_generates_new_token(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    t1 = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]
    await client.delete(f"/docs/{entity_id}/share", headers=_h(tok))
    t2 = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]
    assert t1 != t2


# ---------------------------------------------------------------------------
# Share URL includes full accept link
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_create_share_link_returns_url(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r = await client.post(f"/docs/{entity_id}/share", headers=_h(tok))
    data = r.json()
    assert "url" in data
    assert "celerp.com/accept" in data["url"]
    assert data["token"] in data["url"]


# ---------------------------------------------------------------------------
# Bundle download
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_bundle_download_returns_json(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}/bundle")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    bundle = r.json()
    assert bundle["version"] == 1
    assert "doc" in bundle
    assert "exported_at" in bundle
    assert bundle["doc"].get("contact_name") == "ACME Corp"


@pytest.mark.anyio
async def test_bundle_download_has_cors_header(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}/bundle")
    assert r.headers.get("access-control-allow-origin") == "*"


@pytest.mark.anyio
async def test_bundle_download_invalid_token_404(client: AsyncClient):
    r = await client.get("/share/invalid-token-xyz/bundle")
    assert r.status_code == 404


@pytest.mark.anyio
async def test_share_view_has_cors_header(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert r.headers.get("access-control-allow-origin") == "*"


@pytest.mark.anyio
async def test_bundle_download_sets_filename(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}/bundle")
    cd = r.headers.get("content-disposition", "")
    assert ".celerp" in cd



# ---------------------------------------------------------------------------
# Lists share
# ---------------------------------------------------------------------------

async def _create_list(client: AsyncClient, tok: str) -> str:
    r = await client.post(
        "/lists",
        json={
            "list_type": "quote",
            "contact_name": "Bob's Shop",
            "line_items": [
                {"name": "Diamond Ring", "sku": "DIA-001", "quantity": 1, "unit_price": 50000.0, "line_total": 50000.0}
            ],
            "subtotal": 50000.0,
            "total": 50000.0,
            "currency": "THB",
        },
        headers=_h(tok),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.anyio
async def test_list_share_link_created(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_list(client, tok)
    r = await client.post(f"/docs/{entity_id}/share", headers=_h(tok))
    assert r.status_code == 200
    data = r.json()
    assert "token" in data
    assert "url" in data


@pytest.mark.anyio
async def test_list_public_view_renders_html(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_list(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "Bob's Shop" in body
    assert "DIA-001" in body


@pytest.mark.anyio
async def test_list_public_view_has_accept_cta(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_list(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert "Accept this list" in r.text
    assert "Powered by Celerp" in r.text


@pytest.mark.anyio
async def test_list_bundle_download(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_list(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}/bundle")
    assert r.status_code == 200
    bundle = r.json()
    assert bundle["version"] == 1
    assert bundle["doc"].get("contact_name") == "Bob's Shop"


# ---------------------------------------------------------------------------
# Bundle import endpoint (POST /docs/import-bundle)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_import_bundle_json_body(client: AsyncClient):
    """POST /docs/import-bundle with JSON body imports a received doc."""
    tok = await _token(client)
    bundle = {
        "version": 1,
        "doc": {
            "doc_type": "invoice",
            "ref_id": "EXT-001",
            "contact_name": "Sender Corp",
            "total": 2500.0,
            "status": "open",
            "line_items": [{"description": "Consulting", "quantity": 1, "unit_price": 2500.0}],
        },
        "exported_at": "2026-03-01T00:00:00+00:00",
    }
    r = await client.post(
        "/docs/import-bundle",
        json=bundle,
        headers={**_h(tok), "Content-Type": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("/docs/doc:rcv:")

    # Verify the doc was actually stored
    r2 = await client.get(location, headers=_h(tok))
    assert r2.status_code == 200
    doc = r2.json()
    assert doc["status"] == "received"
    assert doc["contact_name"] == "Sender Corp"


@pytest.mark.anyio
async def test_import_bundle_missing_doc_returns_422(client: AsyncClient):
    tok = await _token(client)
    r = await client.post(
        "/docs/import-bundle",
        json={"version": 1},
        headers={**_h(tok), "Content-Type": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 422


@pytest.mark.anyio
async def test_import_bundle_invalid_json_returns_422(client: AsyncClient):
    tok = await _token(client)
    r = await client.post(
        "/docs/import-bundle",
        content=b"not json",
        headers={**_h(tok), "Content-Type": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 422


@pytest.mark.anyio
async def test_import_bundle_requires_auth(client: AsyncClient):
    bundle = {"version": 1, "doc": {"doc_type": "invoice", "total": 100.0}}
    r = await client.post("/docs/import-bundle", json=bundle, follow_redirects=False)
    assert r.status_code == 401


@pytest.mark.anyio
async def test_import_bundle_sets_status_received(client: AsyncClient):
    """Even if the sender doc has status=paid, imported doc is always status=received."""
    tok = await _token(client)
    bundle = {
        "version": 1,
        "doc": {"doc_type": "invoice", "ref_id": "EXT-002", "total": 500.0, "status": "paid"},
    }
    r = await client.post(
        "/docs/import-bundle",
        json=bundle,
        headers={**_h(tok), "Content-Type": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    r2 = await client.get(r.headers["location"], headers=_h(tok))
    assert r2.json()["status"] == "received"
