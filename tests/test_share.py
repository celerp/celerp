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

@pytest.mark.asyncio
async def test_create_share_link(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r = await client.post(f"/docs/{entity_id}/share", headers=_h(tok))
    assert r.status_code == 200
    data = r.json()
    assert "token" in data
    # 9 random bytes -> 12 URL-safe chars: short enough to share, still unguessable
    assert len(data["token"]) == 12


@pytest.mark.asyncio
async def test_create_share_link_idempotent(client: AsyncClient):
    """Calling share twice returns the same token."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r1 = await client.post(f"/docs/{entity_id}/share", headers=_h(tok))
    r2 = await client.post(f"/docs/{entity_id}/share", headers=_h(tok))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["token"] == r2.json()["token"]


@pytest.mark.asyncio
async def test_create_share_link_requires_auth(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r = await client.post(f"/docs/{entity_id}/share")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_create_share_link_not_found(client: AsyncClient):
    tok = await _token(client)
    r = await client.post("/docs/nonexistent-doc-id/share", headers=_h(tok))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Public share view
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_public_share_view_contains_branding(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert "Powered by Celerp" in r.text
    assert "celerp.com" in r.text


@pytest.mark.asyncio
async def test_public_share_view_contains_accept_cta(client: AsyncClient):
    """Invoice share pages must have the Accept & import CTA."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok, "invoice")
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert "Accept this invoice" in r.text
    assert f"/accept?token={token}" in r.text


@pytest.mark.asyncio
async def test_public_share_view_no_accept_cta_for_memo(client: AsyncClient):
    """Memos have no Accept CTA."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok, "memo")
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert r.status_code == 200
    assert "Accept this" not in r.text


@pytest.mark.asyncio
async def test_public_share_invalid_token_returns_404(client: AsyncClient):
    r = await client.get("/share/totally-invalid-token-xyz")
    assert r.status_code == 404
    assert "celerp.com" in r.text  # always shows helpful content, never a raw error


@pytest.mark.asyncio
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

@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_revoke_nonexistent_share_returns_404(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r = await client.delete(f"/docs/{entity_id}/share", headers=_h(tok))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_revoke_then_reshare_keeps_stable_url(client: AsyncClient):
    """A document's link is stable: revoke turns it off, re-share turns the
    SAME token back on."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    t1 = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    await client.delete(f"/docs/{entity_id}/share", headers=_h(tok))
    assert (await client.get(f"/share/{t1}")).status_code == 404

    reshared = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()
    assert reshared["token"] == t1
    assert reshared["active"] is True
    assert (await client.get(f"/share/{t1}")).status_code == 200


# ---------------------------------------------------------------------------
# Share URL includes full accept link
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
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

@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_bundle_download_has_cors_header(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}/bundle")
    assert r.headers.get("access-control-allow-origin") == "*"


@pytest.mark.asyncio
async def test_bundle_download_invalid_token_404(client: AsyncClient):
    r = await client.get("/share/invalid-token-xyz/bundle")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_share_view_has_cors_header(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert r.headers.get("access-control-allow-origin") == "*"


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_list_share_link_created(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_list(client, tok)
    r = await client.post(f"/docs/{entity_id}/share", headers=_h(tok))
    assert r.status_code == 200
    data = r.json()
    assert "token" in data
    assert "url" in data


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_list_public_view_has_accept_cta(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_list(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/share/{token}")
    assert "Accept this list" in r.text
    assert "Powered by Celerp" in r.text


@pytest.mark.asyncio
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

@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_import_bundle_missing_doc_returns_422(client: AsyncClient):
    tok = await _token(client)
    r = await client.post(
        "/docs/import-bundle",
        json={"version": 1},
        headers={**_h(tok), "Content-Type": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_import_bundle_invalid_json_returns_422(client: AsyncClient):
    tok = await _token(client)
    r = await client.post(
        "/docs/import-bundle",
        content=b"not json",
        headers={**_h(tok), "Content-Type": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_import_bundle_requires_auth(client: AsyncClient):
    bundle = {"version": 1, "doc": {"doc_type": "invoice", "total": 100.0}}
    r = await client.post("/docs/import-bundle", json=bundle, follow_redirects=False)
    assert r.status_code == 401


@pytest.mark.asyncio
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


# ---------------------------------------------------------------------------
# Import hardening: untrusted-bundle handling
# ---------------------------------------------------------------------------

async def _import(client: AsyncClient, tok: str, doc: dict):
    return await client.post(
        "/docs/import-bundle",
        json={"version": 1, "doc": doc},
        headers={**_h(tok), "Content-Type": "application/json"},
        follow_redirects=False,
    )


@pytest.mark.asyncio
async def test_import_recomputes_total_and_drops_unknown_fields(client: AsyncClient):
    tok = await _token(client)
    r = await _import(client, tok, {
        "doc_type": "invoice",
        "total": 0.01,                 # tampered
        "subtotal": 0.01,
        "injected_field": "x",
        "amount_paid": 999.0,
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 500.0, "line_total": 0.01}],
    })
    assert r.status_code == 302
    doc = (await client.get(r.headers["location"], headers=_h(tok))).json()
    assert doc["total"] == 1000.0           # recomputed from qty*price, not trusted
    assert doc["subtotal"] == 1000.0
    assert doc["amount_paid"] == 0.0
    assert "injected_field" not in doc


@pytest.mark.asyncio
async def test_import_rejects_unsupported_doc_type(client: AsyncClient):
    tok = await _token(client)
    r = await _import(client, tok, {"doc_type": "totally_unknown", "total": 100.0})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_import_rejects_too_many_line_items(client: AsyncClient):
    tok = await _token(client)
    r = await _import(client, tok, {
        "doc_type": "invoice",
        "line_items": [{"description": "x", "quantity": 1, "unit_price": 1} for _ in range(1001)],
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_import_rejects_oversized_json_body(client: AsyncClient):
    tok = await _token(client)
    big = {"version": 1, "doc": {"doc_type": "invoice", "notes": "A" * 1_100_000}}
    import json as _json
    r = await client.post(
        "/docs/import-bundle",
        content=_json.dumps(big).encode(),
        headers={**_h(tok), "Content-Type": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 413


def _detail_xml(doc: dict, **kw) -> str:
    from fasthtml.common import to_xml
    from ui.routes.documents import _doc_detail
    return to_xml(_doc_detail(doc, **kw))


def _invoice(status: str = "draft", doc_type: str = "invoice") -> dict:
    return {
        "entity_id": "doc:x1", "id": "doc:x1", "doc_type": doc_type, "status": status,
        "currency": "USD", "total": 100.0,
        "line_items": [{"description": "W", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
    }


def test_doc_detail_shows_share_button_only_when_share_enabled():
    """Share appears only when the cloud gives a reachable public URL (share_enabled),
    NOT merely when the tunnel is 'connected' — the link is served at that URL."""
    on = _detail_xml(_invoice(), share_enabled=True)
    assert "share-modal-doc-x1" in on
    assert "/docs/doc:x1/share" in on

    assert "share-modal-doc-x1" not in _detail_xml(_invoice(), share_enabled=False)


def test_share_hidden_when_connected_but_no_public_url():
    """The bug this guards: connected tunnel but no public URL yet (e.g. tos_required
    / mid-activation) must NOT show a Share button that would copy a dead link."""
    xml = _detail_xml(_invoice(), relay_connected=True, share_enabled=False)
    assert "share-modal-doc-x1" not in xml


def test_supplier_docs_are_not_shareable():
    """Inbound/supplier docs (bill, purchase_order, consignment_in) never get Share."""
    for dt in ("bill", "purchase_order", "consignment_in"):
        xml = _detail_xml(_invoice(status="sent", doc_type=dt), share_enabled=True)
        assert "share-modal-doc-x1" not in xml, dt
    # A customer-facing invoice does.
    assert "share-modal-doc-x1" in _detail_xml(_invoice(status="sent"), share_enabled=True)


def test_paid_invoice_is_still_shareable():
    """Share isn't gated on send-status: a paid invoice can be shared as a receipt."""
    assert "share-modal-doc-x1" in _detail_xml(_invoice(status="paid"), share_enabled=True)


def test_print_view_escapes_injected_html():
    """Document strings (incl. the doc number in the footer) must be HTML-escaped
    when rendered, so an imported doc can't smuggle markup into the print view."""
    from fasthtml.common import to_xml
    from ui.routes.documents import _doc_print_view

    html = to_xml(_doc_print_view({
        "doc_type": "invoice",
        "doc_number": "<script>alert(1)</script>",
        "contact_name": "<img src=x onerror=alert(2)>",
        "currency": "USD",
        "total": 10.0,
        "line_items": [{"description": "<b>bad</b>", "quantity": 1, "unit_price": 10.0}],
    }))
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror=alert(2)>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.asyncio
async def test_import_src_rejects_non_https(client: AsyncClient):
    tok = await _token(client)
    r = await client.get("/docs/import?src=http://evil.example.com&token=abc", headers=_h(tok))
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_import_src_rejects_private_and_metadata_hosts(client: AsyncClient):
    tok = await _token(client)
    for host in ("https://127.0.0.1", "https://10.0.0.1", "https://169.254.169.254", "https://192.168.1.1"):
        r = await client.get(f"/docs/import?src={host}&token=abc", headers=_h(tok))
        assert r.status_code == 400, host


# ---------------------------------------------------------------------------
# Send email carries a view link when cloud-connected
# ---------------------------------------------------------------------------

async def _capture_send(monkeypatch):
    import asyncio
    captured: dict = {}
    done = asyncio.Event()

    async def _fake(to, subject, body_html, body_text="", **kw):
        captured.update(to=to, html=body_html, text=body_text)
        done.set()
        return True

    monkeypatch.setattr("celerp.services.email.send_email", _fake)
    return captured, done


@pytest.mark.asyncio
async def test_send_email_includes_view_link_when_cloud_connected(client: AsyncClient, monkeypatch):
    from celerp.config import settings as cfg
    monkeypatch.setattr(cfg, "celerp_public_url", "https://acme.celerp.com")
    captured, done = await _capture_send(monkeypatch)

    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r = await client.post(f"/docs/{entity_id}/send", json={"sent_to": "cust@x.com"}, headers=_h(tok))
    assert r.status_code == 200

    import asyncio
    await asyncio.wait_for(done.wait(), timeout=2)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]
    assert f"https://acme.celerp.com/share/{token}" in captured["html"]
    assert f"https://acme.celerp.com/share/{token}" in captured["text"]


@pytest.mark.asyncio
async def test_send_email_no_link_when_not_cloud_connected(client: AsyncClient, monkeypatch):
    from celerp.config import settings as cfg
    monkeypatch.setattr(cfg, "celerp_public_url", "")
    captured, done = await _capture_send(monkeypatch)

    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r = await client.post(f"/docs/{entity_id}/send", json={"sent_to": "cust@x.com"}, headers=_h(tok))
    assert r.status_code == 200

    import asyncio
    await asyncio.wait_for(done.wait(), timeout=2)
    assert "/share/" not in captured["html"]


# ---------------------------------------------------------------------------
# Share status + expiry
# ---------------------------------------------------------------------------

async def _expire_token(session, token: str) -> None:
    """Force a token's expiry into the past directly in the DB."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    from celerp.models.share import DocShareToken
    await session.execute(
        update(DocShareToken)
        .where(DocShareToken.token == token)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await session.commit()


@pytest.mark.asyncio
async def test_share_status_lifecycle(client: AsyncClient):
    """First GET mints the stable link deactivated; Share activates it, Revoke
    deactivates it - same token throughout."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)

    r = await client.get(f"/docs/{entity_id}/share", headers=_h(tok))
    assert r.status_code == 200
    first = r.json()
    assert first["active"] is False and first["revoked"] is True
    assert len(first["token"]) == 12
    # A deactivated link does not resolve publicly
    assert (await client.get(f"/share/{first['token']}")).status_code == 404

    created = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()
    assert created["active"] is True and created["revoked"] is False
    assert created["token"] == first["token"]

    status = (await client.get(f"/docs/{entity_id}/share", headers=_h(tok))).json()
    assert status["active"] is True and status["token"] == first["token"]

    revoked = (await client.delete(f"/docs/{entity_id}/share", headers=_h(tok))).json()
    assert revoked["active"] is False and revoked["revoked"] is True
    assert revoked["token"] == first["token"]


@pytest.mark.asyncio
async def test_share_status_unknown_doc_404s(client: AsyncClient):
    tok = await _token(client)
    r = await client.get("/docs/nonexistent-doc-id/share", headers=_h(tok))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_share_expiry_validation(client: AsyncClient):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    r = await client.post(f"/docs/{entity_id}/share",
                          json={"expires_at": "not-a-date"}, headers=_h(tok))
    assert r.status_code == 422
    r = await client.post(f"/docs/{entity_id}/share",
                          json={"expires_at": "2000-01-01"}, headers=_h(tok))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_share_expiry_set_and_cleared(client: AsyncClient):
    """A future expiry is stored; sharing again without one clears it."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    created = (await client.post(f"/docs/{entity_id}/share",
                                 json={"expires_at": "2099-12-31"}, headers=_h(tok))).json()
    assert created["expires_at"] == "2099-12-31"
    assert created["active"] is True

    cleared = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()
    assert cleared["expires_at"] is None
    assert cleared["token"] == created["token"]


@pytest.mark.asyncio
async def test_expired_link_is_dead_everywhere(client: AsyncClient, session):
    """An expired token 404s on the public view and bundle download, and the
    status endpoint reports shared-but-inactive."""
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]
    assert (await client.get(f"/share/{token}")).status_code == 200

    await _expire_token(session, token)

    assert (await client.get(f"/share/{token}")).status_code == 404
    assert (await client.get(f"/share/{token}/bundle")).status_code == 404
    status = (await client.get(f"/docs/{entity_id}/share", headers=_h(tok))).json()
    assert status["active"] is False and status["expired"] is True


@pytest.mark.asyncio
async def test_share_again_reactivates_expired_link(client: AsyncClient, session):
    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]
    await _expire_token(session, token)
    assert (await client.get(f"/share/{token}")).status_code == 404

    reshared = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()
    assert reshared["token"] == token
    assert reshared["active"] is True
    assert (await client.get(f"/share/{token}")).status_code == 200


@pytest.mark.asyncio
async def test_send_email_no_link_when_share_expired(client: AsyncClient, session, monkeypatch):
    """An expired share link never lands in a send email - the email falls back
    to the accept URL instead of embedding a URL that 404s."""
    from celerp.config import settings as cfg
    monkeypatch.setattr(cfg, "celerp_public_url", "https://acme.celerp.com")
    captured, done = await _capture_send(monkeypatch)

    tok = await _token(client)
    entity_id = await _create_doc(client, tok)
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]
    await _expire_token(session, token)

    r = await client.post(f"/docs/{entity_id}/send", json={"sent_to": "cust@x.com"}, headers=_h(tok))
    assert r.status_code == 200

    import asyncio
    await asyncio.wait_for(done.wait(), timeout=2)
    assert "/share/" not in captured["html"]
