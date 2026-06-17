# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""Document sharing — generate public share links and serve read-only doc views.

P2P share flow:
  1. Sender clicks Share → POST /docs/{id}/share → get token
  2. Share URL: https://www.celerp.com/accept?src={CELERP_PUBLIC_URL}&token={token}
  3. Recipient lands on celerp.com/accept (static page) → probes localhost + src
  4a. Recipient has local Celerp + src reachable → GET /docs/import?src=&token= on their instance
  4b. Recipient has no Celerp → signup CTA
  4c. Sender on private net → bundle download fallback

The official branded public renderers (the "Powered by Celerp" share pages) live in the proprietary
celerp.output.share_render module; this module owns the share lifecycle/auth and passes the accept URL in.
See celerp-cloud/SHARE_ACCEPT_FLOW.md for full spec and all failure states.
"""

from __future__ import annotations

import json
import secrets
import uuid as _uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.models.share import DocShareToken
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.output.share_render import _public_doc_page, _public_list_page, _not_found_page

# Authenticated router — share token generation requires login
router = APIRouter(dependencies=[Depends(get_current_user)])

# Public router — share token lookup and recipient import require no auth
public_router = APIRouter()

_TOKEN_BYTES = 32  # 256-bit URL-safe token
_ACCEPT_BASE = "https://www.celerp.com/accept"


def _share_url(token: str) -> str:
    """Build the full celerp.com/accept URL for a share token."""
    params: dict[str, str] = {"token": token}
    src = (settings.celerp_public_url or "").rstrip("/")
    if src:
        params["src"] = src
    return f"{_ACCEPT_BASE}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Authenticated endpoints
# ---------------------------------------------------------------------------

@router.post("/docs/{entity_id}/share")
async def create_share_link(
    entity_id: str,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Generate (or return existing) public share token for a document or list."""
    row = await session.get(Projection, (company_id, entity_id))
    if row is None or row.entity_type not in ("doc", "list"):
        raise HTTPException(status_code=404, detail="Document not found")

    existing = await session.execute(
        select(DocShareToken).where(
            DocShareToken.company_id == company_id,
            DocShareToken.entity_id == entity_id,
        )
    )
    token_row = existing.scalar_one_or_none()
    if token_row:
        return {"token": token_row.token, "url": _share_url(token_row.token)}

    token = secrets.token_urlsafe(_TOKEN_BYTES)
    session.add(DocShareToken(company_id=company_id, entity_id=entity_id, token=token))
    await session.commit()
    return {"token": token, "url": _share_url(token)}


@router.delete("/docs/{entity_id}/share")
async def revoke_share_link(
    entity_id: str,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Revoke the share token for a document."""
    existing = await session.execute(
        select(DocShareToken).where(
            DocShareToken.company_id == company_id,
            DocShareToken.entity_id == entity_id,
        )
    )
    token_row = existing.scalar_one_or_none()
    if not token_row:
        raise HTTPException(status_code=404, detail="No share link found")
    await session.delete(token_row)
    await session.commit()
    return {"revoked": True}


# ---------------------------------------------------------------------------
# Public endpoints (no auth)
# ---------------------------------------------------------------------------

@public_router.get("/share/{token}", response_class=HTMLResponse)
async def view_shared_doc(
    token: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Public read-only document view. No authentication required.
    CORS: Access-Control-Allow-Origin: * so celerp.com/accept JS can probe reachability.
    """
    share_row = (await session.execute(
        select(DocShareToken).where(DocShareToken.token == token)
    )).scalar_one_or_none()
    if share_row is None:
        return HTMLResponse(_not_found_page("link-expired"), status_code=404)

    row = await session.get(Projection, (share_row.company_id, share_row.entity_id))
    if row is None:
        return HTMLResponse(_not_found_page("doc-missing"), status_code=404)

    headers = {"Access-Control-Allow-Origin": "*"}
    state = row.state
    if row.entity_type == "list":
        return HTMLResponse(_public_list_page(state, token, _share_url(token)), headers=headers)
    return HTMLResponse(_public_doc_page(state, token, _share_url(token)), headers=headers)


@public_router.options("/share/{token}")
async def share_cors_preflight(token: str) -> Response:
    """Handle CORS preflight for the share endpoint."""
    return Response(
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }
    )


@public_router.get("/share/{token}/bundle")
async def download_share_bundle(
    token: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Download the document as a .celerp JSON bundle (fallback for p2p import failures)."""
    share_row = (await session.execute(
        select(DocShareToken).where(DocShareToken.token == token)
    )).scalar_one_or_none()
    if share_row is None:
        raise HTTPException(status_code=404, detail="Share link not found or revoked")

    row = await session.get(Projection, (share_row.company_id, share_row.entity_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Document no longer exists")

    doc = row.state
    ref = doc.get("ref_id") or doc.get("doc_number") or share_row.entity_id
    bundle = {
        "version": 1,
        "doc": doc,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    filename = f"{ref}.celerp"
    return Response(
        content=json.dumps(bundle, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Allow-Origin": "*",
        },
    )


@public_router.get("/docs/import")
async def import_shared_doc(
    src: str = Query(..., description="Sender's Celerp public URL"),
    token: str = Query(..., description="Share token from sender"),
    company_id: _uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Recipient's Celerp fetches a shared doc from the sender's instance and imports it.

    Called by celerp.com/accept after probing that both instances are reachable.
    The doc is stored with status='received' — not auto-booked. Recipient reviews first.
    """
    src_clean = src.rstrip("/")
    fetch_url = f"{src_clean}/share/{token}/bundle"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(fetch_url)
            r.raise_for_status()
            bundle = r.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Share link not found on sender's instance")
        raise HTTPException(status_code=502, detail=f"Sender's instance returned {exc.response.status_code}")
    except Exception:
        raise HTTPException(status_code=502, detail="Could not reach sender's Celerp instance")

    return await _import_bundle(bundle, token, company_id, user.id, session, src_clean)


@public_router.post("/docs/import-bundle")
async def import_bundle_upload(
    request: Request,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Accept a .celerp bundle (JSON body or multipart file) and import as received doc.

    Used when p2p fetch is unavailable (sender on private network).
    Accepts: application/json body OR multipart/form-data with field 'bundle'.
    """
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        file = form.get("bundle")
        if file is None:
            raise HTTPException(status_code=422, detail="Missing 'bundle' field in multipart form")
        raw = await file.read()
        try:
            bundle = json.loads(raw)
        except Exception:
            raise HTTPException(status_code=422, detail="Bundle file is not valid JSON")
    else:
        try:
            bundle = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="Request body is not valid JSON")

    return await _import_bundle(bundle, None, company_id, user.id, session, None)


# ---------------------------------------------------------------------------
# Shared import helper
# ---------------------------------------------------------------------------

async def _import_bundle(
    bundle: dict,
    token: str | None,
    company_id: _uuid.UUID,
    actor_id: _uuid.UUID,
    session: AsyncSession,
    src: str | None,
) -> Response:
    """Import a .celerp bundle dict as a received doc. Returns a redirect to the doc."""
    doc = bundle.get("doc") or {}
    if not doc:
        raise HTTPException(status_code=422, detail="Bundle contains no document data")

    # Strip sender-specific keys that would conflict locally
    inbound = {k: v for k, v in doc.items() if k not in ("entity_id", "company_id")}
    if token:
        inbound["source_share_token"] = token
    if src:
        inbound["source_origin"] = src

    entity_id = f"doc:rcv:{_uuid.uuid4().hex[:12]}"
    idem_key = f"share:{token}:{company_id}" if token else f"bundle:{_uuid.uuid4().hex}"

    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.shared_import",
        data=inbound,
        actor_id=actor_id,
        location_id=None,
        source="share_import",
        idempotency_key=idem_key,
        metadata_={"share_token": token or "", "src": src or ""},
    )
    await session.commit()

    return Response(
        status_code=302,
        headers={"Location": f"/docs/{entity_id}"},
    )
