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

import asyncio
import ipaddress
import json
import secrets
import socket
import uuid as _uuid
from datetime import date as _date, datetime, time as _time, timezone
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.models.share import DocShareToken
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.money import round_money, to_decimal, to_stored_float
from celerp.output.doc_print import (
    IMPORTABLE_DOC_TYPES, INVOICE_LAYOUT_DOC_TYPES,
    compose_address, render_doc_print_html, unwrap_address,
)
from celerp.output.share_render import _not_found_page
from celerp_docs.taxes import TaxApplication, compute_tax_amounts

# Authenticated router — share token generation requires login
router = APIRouter(dependencies=[Depends(get_current_user)])

# Public router — share token lookup and recipient import require no auth
public_router = APIRouter()

_TOKEN_BYTES = 9  # 72-bit URL-safe token (12 chars) — short enough to share by hand, unguessable, revocable
_ACCEPT_BASE = "https://www.celerp.com/accept"

# A bundle arrives from another party — treat it as untrusted input. Cap what we
# read, accept only known fields, and recompute all money locally rather than
# trusting the sender's numbers.
_MAX_BUNDLE_BYTES = 1_000_000
_MAX_LINE_ITEMS = 1000
_MAX_STR = 2000
_MAX_NOTES = 20_000
_FETCH_TIMEOUT = 10.0

_IMPORTABLE_DOC_TYPES = frozenset({
    "invoice", "quotation", "proforma", "purchase_order",
    "credit_note", "bill", "memo", "consignment_in",
})
_DOC_STR_FIELDS = frozenset({
    "doc_type", "ref_id", "doc_number", "issue_date", "due_date", "valid_until",
    "expected_delivery", "currency", "contact_name", "contact_company_name",
    "contact_email", "contact_billing_address", "contact_shipping_address",
    "contact_tax_id", "shipping_attn", "terms", "payment_terms",
    "discount_type", "carrier", "tracking",
})
_LINE_STR_FIELDS = frozenset({
    "sku", "name", "description", "unit", "sell_by", "weight_unit",
    "hs_code", "account_code", "tax_code",
})
_LINE_NUM_FIELDS = frozenset({
    "quantity", "unit_price", "pieces", "weight", "tax_rate", "discount_pct",
})


def _share_url(token: str) -> str:
    """Build the full celerp.com/accept URL for a share token."""
    params: dict[str, str] = {"token": token}
    src = (settings.celerp_public_url or "").rstrip("/")
    if src:
        params["src"] = src
    return f"{_ACCEPT_BASE}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Untrusted-input guards (SSRF, size caps, field whitelist + money recompute)
# ---------------------------------------------------------------------------

def _blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def _validate_public_src(src: str) -> str:
    """Return a cleaned https base URL, or raise if it is not a safe public host.

    The recipient's instance fetches this URL server-side, so an unvalidated `src`
    is an SSRF vector — require https and reject any host that resolves to a
    private, loopback, link-local, or reserved address.
    """
    cleaned = (src or "").rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Sender URL must be https")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Sender URL could not be resolved")
    if any(_blocked_ip(info[4][0]) for info in infos):
        raise HTTPException(status_code=400, detail="Sender URL is not a public address")
    return cleaned


async def _read_body_capped(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="Bundle too large")
    return bytes(body)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _str(v, limit: int = _MAX_STR) -> str | None:
    if v is None:
        return None
    return str(v)[:limit]


def _sanitize_taxes(raw) -> list[TaxApplication]:
    if not isinstance(raw, list):
        return []
    out: list[TaxApplication] = []
    for t in raw[:20]:
        if not isinstance(t, dict):
            continue
        out.append(TaxApplication(
            code=str(t.get("code") or "")[:64],
            rate=_num(t.get("rate")),
            amount=0.0,  # recomputed below — never trust the sender's amount
            order=int(_num(t.get("order"))),
            is_compound=bool(t.get("is_compound")),
            label=str(t.get("label") or "")[:64],
        ))
    return out


def _sanitize_bundle_doc(doc: dict) -> dict:
    """Rebuild a doc from an allowlist and recompute every monetary value locally.

    Nothing from the sender's bundle is trusted for money or status: line totals,
    subtotal, tax, and total are all derived here from quantity × price so a
    tampered bundle can never misstate the figures the recipient sees.
    """
    if not isinstance(doc, dict):
        raise HTTPException(status_code=422, detail="Bundle document is malformed")
    doc_type = str(doc.get("doc_type") or "").strip()
    if doc_type not in _IMPORTABLE_DOC_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported document type in bundle")

    currency = _str(doc.get("currency"), 8) or "USD"
    out: dict = {}
    for k in _DOC_STR_FIELDS:
        val = _str(doc.get(k), _MAX_STR)
        if val is not None:
            out[k] = val
    out["notes"] = _str(doc.get("notes"), _MAX_NOTES) or ""
    out["currency"] = currency
    out["discount"] = _num(doc.get("discount"))
    out["shipping"] = _num(doc.get("shipping"))

    raw_lines = doc.get("line_items")
    if not isinstance(raw_lines, list):
        raw_lines = []
    if len(raw_lines) > _MAX_LINE_ITEMS:
        raise HTTPException(status_code=422, detail="Too many line items in bundle")

    lines: list[dict] = []
    subtotal_d = to_decimal(0)
    line_tax_d = to_decimal(0)
    for raw in raw_lines:
        if not isinstance(raw, dict):
            continue
        line: dict = {}
        for k in _LINE_STR_FIELDS:
            val = _str(raw.get(k), _MAX_STR)
            if val is not None:
                line[k] = val
        for k in _LINE_NUM_FIELDS:
            if k in raw:
                line[k] = _num(raw.get(k))
        base = to_decimal(line.get("quantity", 0)) * to_decimal(line.get("unit_price", 0))
        disc_pct = to_decimal(line.get("discount_pct", 0))
        if disc_pct:
            base = base * (to_decimal(1) - disc_pct / 100)
        lt = round_money(base, currency)
        line["line_total"] = to_stored_float(lt)
        subtotal_d += lt
        taxes = _sanitize_taxes(raw.get("taxes"))
        if taxes:
            resolved = compute_tax_amounts(taxes, to_stored_float(lt), currency)
            line["taxes"] = [t.model_dump() for t in resolved]
            line_tax_d += sum(to_decimal(t.amount) for t in resolved)
        lines.append(line)
    out["line_items"] = lines

    subtotal_d = subtotal_d - round_money(out["discount"], currency)
    doc_taxes = _sanitize_taxes(doc.get("doc_taxes"))
    if doc_taxes:
        resolved = compute_tax_amounts(doc_taxes, to_stored_float(subtotal_d), currency)
        out["doc_taxes"] = [t.model_dump() for t in resolved]
        tax_d = sum(to_decimal(t.amount) for t in resolved) + line_tax_d
    else:
        tax_d = line_tax_d
    shipping_d = round_money(out["shipping"], currency)
    out["subtotal"] = to_stored_float(round_money(subtotal_d, currency))
    out["tax"] = to_stored_float(round_money(tax_d, currency))
    out["total"] = to_stored_float(round_money(subtotal_d + tax_d + shipping_d, currency))
    return out


# ---------------------------------------------------------------------------
# Authenticated endpoints
# ---------------------------------------------------------------------------

def _share_active(row: DocShareToken) -> bool:
    """A link resolves while it is not revoked and not past its expiry instant."""
    if row.revoked_at is not None:
        return False
    return row.expires_at is None or row.expires_at > datetime.now(timezone.utc)


async def _find_share_row(session: AsyncSession, company_id, entity_id: str) -> DocShareToken | None:
    return (await session.execute(
        select(DocShareToken).where(
            DocShareToken.company_id == company_id,
            DocShareToken.entity_id == entity_id,
        )
    )).scalar_one_or_none()


async def _active_share_row(session: AsyncSession, token: str) -> DocShareToken | None:
    """Resolve a public token to its row; expired links are treated as revoked."""
    row = (await session.execute(
        select(DocShareToken).where(DocShareToken.token == token)
    )).scalar_one_or_none()
    return row if row is not None and _share_active(row) else None


async def get_or_create_share_token(session: AsyncSession, company_id, entity_id: str) -> DocShareToken:
    """Return the entity's share token row, minting a deactivated one if none
    exists. A document's link is stable for its lifetime: activation (Share),
    deactivation (Revoke) and expiry toggle the same token. Caller commits."""
    row = await _find_share_row(session, company_id, entity_id)
    if row is None:
        row = DocShareToken(
            company_id=company_id, entity_id=entity_id,
            token=secrets.token_urlsafe(_TOKEN_BYTES),
            revoked_at=datetime.now(timezone.utc),  # born deactivated; Share turns it on
        )
        session.add(row)
    return row


def public_view_url(token: str) -> str | None:
    """Direct link to the branded read-only view, or None if not cloud-connected."""
    base = (settings.celerp_public_url or "").rstrip("/")
    return f"{base}/share/{token}" if base else None


async def send_view_url(session: AsyncSession, company_id, entity_id: str) -> str | None:
    """View link to embed in a send email — only when cloud-connected (the
    branded view is only reachable then). Sending IS an explicit share, so a
    revoked link is reactivated; an expired one yields None rather than a URL
    that 404s. Caller commits."""
    if not (settings.celerp_public_url or "").strip():
        return None
    row = await get_or_create_share_token(session, company_id, entity_id)
    row.revoked_at = None
    if not _share_active(row):
        return None
    return public_view_url(row.token)


def _share_status(row: DocShareToken | None) -> dict:
    """Uniform share-state payload for the UI: status/create/revoke all return it."""
    if row is None:
        return {"shared": False, "active": False, "revoked": False, "expired": False,
                "token": None, "url": None, "view_url": None, "expires_at": None}
    active = _share_active(row)
    return {
        "shared": active,
        "active": active,
        "revoked": row.revoked_at is not None,
        "expired": row.revoked_at is None and not active,
        "token": row.token,
        "url": _share_url(row.token),
        "view_url": public_view_url(row.token),
        "expires_at": row.expires_at.date().isoformat() if row.expires_at else None,
    }


class ShareCreateBody(BaseModel):
    # ISO date (YYYY-MM-DD); the link stops resolving at the end of that day UTC.
    # None/empty = no expiry.
    expires_at: str | None = None


@router.get("/docs/{entity_id}/share/state")
async def share_state(
    entity_id: str,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Read-only share state for the page-load status light. Never mints a
    token (so merely viewing a document doesn't create share rows)."""
    row = await _find_share_row(session, company_id, entity_id)
    return {"active": bool(row is not None and _share_active(row))}


@router.get("/docs/{entity_id}/share")
async def share_status(
    entity_id: str,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Share state for a document or list. Mints the (deactivated) token on
    first call so the UI always shows the document's stable URL; never
    activates anything."""
    row = await session.get(Projection, (company_id, entity_id))
    if row is None or row.entity_type not in ("doc", "list"):
        raise HTTPException(status_code=404, detail="Document not found")
    share_row = await get_or_create_share_token(session, company_id, entity_id)
    await session.commit()
    return _share_status(share_row)


@router.post("/docs/{entity_id}/share")
async def create_share_link(
    entity_id: str,
    body: ShareCreateBody | None = None,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create the public share token for a document or list (or update the
    expiry of the existing one)."""
    row = await session.get(Projection, (company_id, entity_id))
    if row is None or row.entity_type not in ("doc", "list"):
        raise HTTPException(status_code=404, detail="Document not found")

    expires = None
    if body and body.expires_at:
        try:
            expiry_date = _date.fromisoformat(body.expires_at)
        except ValueError:
            raise HTTPException(status_code=422, detail="expires_at must be an ISO date (YYYY-MM-DD)")
        expires = datetime.combine(expiry_date, _time(23, 59, 59), tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            raise HTTPException(status_code=422, detail="expires_at must be in the future")

    share_row = await get_or_create_share_token(session, company_id, entity_id)
    share_row.revoked_at = None
    share_row.expires_at = expires
    await session.commit()
    return _share_status(share_row)


@router.delete("/docs/{entity_id}/share")
async def revoke_share_link(
    entity_id: str,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Deactivate the share link immediately. The token row persists so the
    document keeps its stable URL; Share turns the same link back on."""
    token_row = await _find_share_row(session, company_id, entity_id)
    if not token_row:
        raise HTTPException(status_code=404, detail="No share link found")
    token_row.revoked_at = datetime.now(timezone.utc)
    await session.commit()
    return _share_status(token_row)


# ---------------------------------------------------------------------------
# Public endpoints (no auth)
# ---------------------------------------------------------------------------

async def _letterhead(session: AsyncSession, company_id) -> dict:
    """Company letterhead fields for the shared document - the DB-side mirror
    of the UI's resolution: the company's self-contact first, company
    settings as the fallback."""
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    if company is None:
        return {}
    cfg = company.settings or {}
    contact_state: dict = {}
    self_id = cfg.get("self_contact_id")
    if self_id:
        crow = await session.get(Projection, (company_id, self_id))
        if crow is not None:
            contact_state = crow.state or {}
    addrs = contact_state.get("addresses") or []
    primary = next((a for a in addrs if a.get("address_type") == "billing"), None) or (addrs[0] if addrs else None)
    address = (compose_address(primary) if primary else "") or unwrap_address(cfg.get("address")) or ""
    return {
        "company_name": contact_state.get("name") or company.name or "",
        "company_address": address,
        "company_phone": contact_state.get("phone") or cfg.get("phone") or "",
        "company_tax_id": contact_state.get("tax_id") or cfg.get("tax_id") or "",
        "company_email": contact_state.get("email") or cfg.get("email") or "",
    }


async def _resolve_share_contact(session: AsyncSession, company_id, state: dict) -> None:
    """Fill the Bill-To block from the contact projection when the doc state
    only carries a contact_id."""
    cid = state.get("contact_id")
    if not cid or state.get("contact_name"):
        return
    crow = await session.get(Projection, (company_id, cid))
    if crow is None:
        return
    contact = crow.state or {}
    state["contact_name"] = contact.get("name") or ""
    state["contact_company_name"] = contact.get("company_name") or ""
    state["contact_email"] = contact.get("email") or ""
    state["contact_billing_address"] = contact.get("billing_address") or contact.get("address") or ""
    state["contact_tax_id"] = contact.get("tax_id") or ""


async def _enrich_share_lines(session: AsyncSession, company_id, state: dict) -> None:
    """Source pieces/weight (and the weight's unit) from each line's item for
    the shared view - the DB-side mirror of the UI print enrichment."""
    if state.get("doc_type") not in INVOICE_LAYOUT_DOC_TYPES:
        return
    from celerp.models.company import Company
    from celerp.services.line_measures import item_measure_meta, resolve_line_measures
    from celerp.services.units import DEFAULT_UNITS, build_unit_map
    company = await session.get(Company, company_id)
    units = ((company.settings or {}).get("units") if company else None) or DEFAULT_UNITS
    umap = build_unit_map(units)
    for li in state.get("line_items") or []:
        eid = li.get("entity_id") or li.get("item_id")
        if not eid:
            continue
        irow = await session.get(Projection, (company_id, eid))
        if irow is None:
            continue
        meta = item_measure_meta(irow.state or {}, umap)
        li["pieces"], li["weight"], li["weight_unit"], _, _ = resolve_line_measures(li, item_meta=meta)


@public_router.get("/share/{token}", response_class=HTMLResponse)
async def view_shared_doc(
    token: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Public read-only document view - the exact letterhead layout the Print
    button produces, plus the shared quiet footer. No authentication required.
    CORS: Access-Control-Allow-Origin: * so celerp.com/accept JS can probe reachability.
    """
    share_row = await _active_share_row(session, token)
    if share_row is None:
        return HTMLResponse(_not_found_page("link-expired"), status_code=404)

    row = await session.get(Projection, (share_row.company_id, share_row.entity_id))
    if row is None:
        return HTMLResponse(_not_found_page("doc-missing"), status_code=404)

    state = dict(row.state or {})
    if row.entity_type == "list":
        state.setdefault("doc_type", "list")
        if not state.get("contact_name"):
            state["contact_name"] = state.get("receiver") or state.get("customer_name") or ""
        if not state.get("issue_date"):
            state["issue_date"] = state.get("created_at") or state.get("date")
    if not state.get("company_name"):
        state.update(await _letterhead(session, share_row.company_id))
    await _resolve_share_contact(session, share_row.company_id, state)
    await _enrich_share_lines(session, share_row.company_id, state)

    importable = state.get("doc_type") in IMPORTABLE_DOC_TYPES
    html = render_doc_print_html(state, import_url=_share_url(token) if importable else None)
    return HTMLResponse(html, headers={"Access-Control-Allow-Origin": "*"})


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
    share_row = await _active_share_row(session, token)
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
    src_clean = await _validate_public_src(src)
    fetch_url = f"{src_clean}/share/{token}/bundle"

    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=False) as client:
            r = await client.get(fetch_url)
            r.raise_for_status()
            if len(r.content) > _MAX_BUNDLE_BYTES:
                raise HTTPException(status_code=413, detail="Bundle too large")
            bundle = json.loads(r.content)
    except HTTPException:
        raise
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
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > 2 * _MAX_BUNDLE_BYTES:
        raise HTTPException(status_code=413, detail="Bundle too large")

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        file = form.get("bundle")
        if file is None:
            raise HTTPException(status_code=422, detail="Missing 'bundle' field in multipart form")
        raw = await file.read(_MAX_BUNDLE_BYTES + 1)
        if len(raw) > _MAX_BUNDLE_BYTES:
            raise HTTPException(status_code=413, detail="Bundle too large")
        try:
            bundle = json.loads(raw)
        except Exception:
            raise HTTPException(status_code=422, detail="Bundle file is not valid JSON")
    else:
        raw = await _read_body_capped(request, _MAX_BUNDLE_BYTES)
        try:
            bundle = json.loads(raw)
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
    if not isinstance(bundle, dict):
        raise HTTPException(status_code=422, detail="Bundle is malformed")
    doc = bundle.get("doc") or {}
    if not doc:
        raise HTTPException(status_code=422, detail="Bundle contains no document data")

    # Accept only known fields and recompute money locally — never trust the bundle.
    inbound = _sanitize_bundle_doc(doc)
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
