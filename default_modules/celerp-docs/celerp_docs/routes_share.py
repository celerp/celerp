# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""Document sharing — generate public share links and serve read-only doc views.

P2P share flow:
  1. Sender clicks Share → POST /docs/{id}/share → get token
  2. The share page (``<public url>/share/<token>``, or a share.celerp.com link on a
     free instance) carries an Import link: celerp.com/accept?link=<share page>
  3. Recipient pastes the link into their own Celerp → GET /docs/import?link=
     (the older ``src`` + ``token`` pair is still accepted)
  4. The document lands in Received; Book turns it into a local draft
  5. Sender on private net → bundle download fallback

The official branded public renderers (the "Powered by Celerp" share pages) live in the proprietary
celerp.output.share_render module; this module owns the share lifecycle/auth and passes the accept URL in.
See celerp-cloud/SHARE_ACCEPT_FLOW.md for full spec and all failure states.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import uuid as _uuid
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.db import get_session
from celerp.models.projections import Projection
from celerp.models.share import DocShareToken, is_active as share_is_active
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.money import round_money, to_decimal, to_stored_float
from celerp.services.permissions import require_permission
from celerp.services.outbound_url import validate_public_base_url
from celerp.output.doc_print import (
    IMPORT_ACCEPT_URL, INVOICE_LAYOUT_DOC_TYPES,
    import_accept_url, render_doc_print_html,
)
from celerp.output.share_render import _not_found_page
from celerp.output.document_context import prepare_document_output
from celerp_docs import received
from celerp_docs.doc_constants import SHAREABLE_DOC_TYPES, is_shareable, share_doc_type
from celerp_docs.taxes import TaxApplication, compute_tax_amounts

# Authenticated router — share token generation requires login
router = APIRouter(dependencies=[Depends(get_current_user)])

# Public router — share token lookup and recipient import require no auth
public_router = APIRouter()

_TOKEN_BYTES = 9  # 72-bit URL-safe token (12 chars) — short enough to share by hand, unguessable, revocable
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# A bundle arrives from another party — treat it as untrusted input. Cap what we
# read, accept only known fields, and recompute all money locally rather than
# trusting the sender's numbers.
_MAX_BUNDLE_BYTES = 1_000_000
_MAX_LINE_ITEMS = 1000
_MAX_STR = 2000
_MAX_NOTES = 20_000
_FETCH_TIMEOUT = 10.0

_DOC_STR_FIELDS = frozenset({
    "doc_type", "list_type", "ref_id", "doc_number", "reference", "issue_date", "due_date", "valid_until",
    "expected_delivery", "currency", "company_name", "company_address", "company_phone",
    "company_tax_id", "company_email", "company_website", "contact_name", "contact_company_name",
    "contact_email", "contact_phone", "contact_billing_address", "contact_shipping_address",
    "contact_tax_id", "contact_billing_attn", "shipping_attn", "terms", "terms_template", "terms_text",
    "customer_note", "payment_terms", "discount_type", "carrier", "tracking",
})
_DOC_NUM_FIELDS = frozenset({
    "discount", "shipping", "subtotal", "tax", "total",
})
_LINE_STR_FIELDS = frozenset({
    "sku", "name", "description", "unit", "sell_by", "weight_unit",
    "hs_code", "country_of_origin", "tax_code",
})
_LINE_NUM_FIELDS = frozenset({
    "quantity", "unit_price", "pieces", "weight", "tax_rate", "discount_pct",
})


def _import_link(link: str | None, src: str | None, token: str | None) -> str:
    """One share link from either input form: ``link``, or the older
    ``src`` + ``token`` pair. Mixed or incomplete input is rejected."""
    if link:
        if src or token:
            raise HTTPException(status_code=400, detail="Give either link, or src and token, not both")
        return link
    if not (src and token):
        raise HTTPException(status_code=400, detail="A share link is required: link, or both src and token")
    if not _TOKEN_RE.fullmatch(token):
        raise HTTPException(status_code=400, detail="Not a Celerp share link")
    return f"{src.rstrip('/')}/share/{token}"


# ---------------------------------------------------------------------------
# Untrusted-input guards (SSRF, size caps, field whitelist + money recompute)
# ---------------------------------------------------------------------------

async def _validate_share_link(link: str) -> str:
    """Return the share page URL for a public share link, or 400.

    Every Celerp share link is a public page whose last path segment is its
    token, with the bundle download at ``<page>/bundle``."""
    try:
        page = await validate_public_base_url(link, reject_query=True, reject_fragment=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = urlsplit(page).path.rsplit("/", 1)[-1]
    if not token:
        raise HTTPException(status_code=400, detail="Not a Celerp share link")
    return page


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


def _public_taxes(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [
        {
            "code": str(t.get("code") or "")[:64],
            "rate": _num(t.get("rate")),
            "amount": _num(t.get("amount")),
            "order": int(_num(t.get("order"))),
            "is_compound": bool(t.get("is_compound")),
            "label": str(t.get("label") or "")[:64],
        }
        for t in raw[:20] if isinstance(t, dict)
    ]


def _public_bundle_doc(doc: dict) -> dict:
    """Allowlisted customer-facing bundle state; internal projection fields never leave the sender."""
    if not isinstance(doc, dict):
        raise HTTPException(status_code=422, detail="Bundle document is malformed")
    out: dict = {}
    for key in _DOC_STR_FIELDS:
        value = _str(
            doc.get(key),
            _MAX_NOTES if key in {"terms", "terms_text", "customer_note"} else _MAX_STR,
        )
        if value is not None:
            out[key] = value
    # Bundles may originate from pre-terms_text installations. Keep accepting
    # the historical alias, but publish/import one canonical customer-facing
    # field. An explicit canonical blank deliberately suppresses legacy text.
    if "terms_text" not in out and "terms" in out:
        out["terms_text"] = out["terms"]
    out.pop("terms", None)
    for key in _DOC_NUM_FIELDS:
        if key in doc:
            out[key] = _num(doc.get(key))

    raw_lines = doc.get("line_items")
    if not isinstance(raw_lines, list):
        raw_lines = []
    if len(raw_lines) > _MAX_LINE_ITEMS:
        raise HTTPException(status_code=422, detail="Too many line items in bundle")
    lines: list[dict] = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            continue
        line: dict = {}
        for key in _LINE_STR_FIELDS:
            value = _str(raw.get(key), _MAX_STR)
            if value is not None:
                line[key] = value
        for key in _LINE_NUM_FIELDS | {"line_total"}:
            if key in raw:
                line[key] = _num(raw.get(key))
        taxes = _public_taxes(raw.get("taxes"))
        if taxes:
            line["taxes"] = taxes
        lines.append(line)
    out["line_items"] = lines

    doc_taxes = _public_taxes(doc.get("doc_taxes"))
    if doc_taxes:
        out["doc_taxes"] = doc_taxes
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
    if doc_type not in SHAREABLE_DOC_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported document type in bundle")

    public = _public_bundle_doc(doc)
    currency = _str(public.get("currency"), 8) or "USD"
    # Payment state and derived totals are local accounting facts. Never
    # accept them from an untrusted sender: totals are recomputed below, and a
    # received document carries no payment state at all.
    derived = {"line_items", "doc_taxes", "subtotal", "tax", "total", "amount_paid", "amount_outstanding"}
    out: dict = {k: v for k, v in public.items() if k not in derived}
    out["currency"] = currency
    out["discount"] = _num(public.get("discount"))
    out["shipping"] = _num(public.get("shipping"))

    raw_lines = public.get("line_items")
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
    doc_taxes = _sanitize_taxes(public.get("doc_taxes"))
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
    """A link resolves while it is not revoked and not past its expiry instant.
    The rule lives once in celerp.models.share; this is its module-side name."""
    return share_is_active(row)


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


async def public_view_url(token: str) -> str | None:
    """Direct link to the branded read-only view, or None when no link can be
    minted. A paid instance builds `<public_url>/share/<token>`; a free
    relay-bound instance mints a `share.celerp.com` envelope through the relay
    seam; a self-hosted instance (no relay) returns None."""
    base = (settings.celerp_public_url or "").rstrip("/")
    if base:
        return f"{base}/share/{token}"
    from celerp.services import relay_share
    return await relay_share.mint_free_share_url(token)


# Emailing a document always shares it for this long, so the recipient's view
# link is guaranteed to work without the sender managing expiry by hand.
SEND_SHARE_DAYS = 30


async def send_view_url(session: AsyncSession, company_id, entity_id: str) -> str | None:
    """Activate the public share link for a send and return its URL, or None
    when no link can be minted (a self-hosted instance with no relay).

    Sending IS the share: the link is reactivated and given a fresh
    SEND_SHARE_DAYS window each time, so every emailed link is live. A free
    relay-bound instance brings its lazy tunnel up so the link resolves once
    sent. Caller commits."""
    row = await get_or_create_share_token(session, company_id, entity_id)
    row.revoked_at = None
    row.expires_at = datetime.now(timezone.utc) + timedelta(days=SEND_SHARE_DAYS)
    from celerp.services import relay_share
    relay_share.ensure_running()
    return await public_view_url(row.token)


async def send_pay_url(session: AsyncSession, company_id, entity_id: str) -> str | None:
    """Online-payment link for a send email, when cloud-connected and Stripe is
    connected. Rides the same stable share token the view link activates (call
    send_view_url first in a send flow), so both links live and die together.
    Caller commits."""
    from celerp.services import payments as _pay
    base = (settings.celerp_public_url or "").rstrip("/")
    if not base or not _pay.payments_enabled():
        return None
    row = await get_or_create_share_token(session, company_id, entity_id)
    return f"{base}/pay/{row.token}"


async def share_import_url(session: AsyncSession, row: DocShareToken | None,
                           view_url: str | None = None) -> str | None:
    """Import link for a live share of an importable document: the accept page
    carrying the public view address. None otherwise, so a printed or saved
    copy never carries a link that goes nowhere."""
    if row is None or not _share_active(row):
        return None
    doc = await session.get(Projection, (row.company_id, row.entity_id))
    if doc is None or not is_shareable(doc.entity_type, doc.state or {}):
        return None
    view_url = view_url or await public_view_url(row.token)
    return import_accept_url(view_url) if view_url else None


async def _share_status(session: AsyncSession, row: DocShareToken | None) -> dict:
    """Uniform share-state payload for the UI: status/create/revoke all return it."""
    if row is None:
        return {"shared": False, "active": False, "revoked": False, "expired": False,
                "token": None, "url": None, "view_url": None, "expires_at": None}
    active = _share_active(row)
    view_url = await public_view_url(row.token)
    return {
        "shared": active,
        "active": active,
        "revoked": row.revoked_at is not None,
        "expired": row.revoked_at is None and not active,
        "token": row.token,
        "url": await share_import_url(session, row, view_url),
        "view_url": view_url,
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
    if not is_shareable(row.entity_type, row.state or {}):
        raise HTTPException(status_code=422, detail="This type of document cannot be shared")
    share_row = await get_or_create_share_token(session, company_id, entity_id)
    await session.commit()
    return await _share_status(session, share_row)


@router.post("/docs/{entity_id}/share")
async def create_share_link(
    entity_id: str,
    body: ShareCreateBody | None = None,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create the public share token for a document or list (or update the
    expiry of the existing one)."""
    row = await session.get(Projection, (company_id, entity_id))
    if row is None or row.entity_type not in ("doc", "list"):
        raise HTTPException(status_code=404, detail="Document not found")
    if not is_shareable(row.entity_type, row.state or {}):
        raise HTTPException(status_code=422, detail="This type of document cannot be shared")

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
    # A free instance's tunnel is lazy: creating a share brings it up on demand
    # so the link resolves. A no-op for a paid (always-on) or self-hosted instance.
    from celerp.services import relay_share
    relay_share.ensure_running()
    await session.commit()
    return await _share_status(session, share_row)


@router.delete("/docs/{entity_id}/share")
async def revoke_share_link(
    entity_id: str,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Deactivate the share link immediately. The token row persists so the
    document keeps its stable URL; Share turns the same link back on."""
    token_row = await _find_share_row(session, company_id, entity_id)
    if not token_row:
        raise HTTPException(status_code=404, detail="No share link found")
    token_row.revoked_at = datetime.now(timezone.utc)
    await session.commit()
    return await _share_status(session, token_row)


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
        if crow is not None and crow.entity_type == "contact":
            contact_state = crow.state or {}
    prepared = prepare_document_output(
        {}, company={"name": company.name, "settings": cfg}, self_contact=contact_state,
    )
    return {
        key: prepared.get(key) or ""
        for key in ("company_name", "company_address", "company_phone", "company_tax_id", "company_email", "company_website")
    }


async def _resolve_share_contact(session: AsyncSession, company_id, state: dict) -> None:
    """Fill the Bill-To block from the contact projection when the doc state
    only carries a contact_id."""
    cid = state.get("contact_id")
    if not cid:
        return
    crow = await session.get(Projection, (company_id, cid))
    if crow is None or crow.entity_type != "contact":
        return
    state.update(prepare_document_output(state, contact=crow.state or {}))


async def _enrich_share_lines(session: AsyncSession, company_id, state: dict) -> str:
    """Source pieces/weight (and the weight's unit) from each line's item for
    the shared view - the DB-side mirror of the UI print enrichment. Shipping
    documents also backfill HS code / country of origin from the item. When the
    company shows barcodes on lines, every doc type backfills barcodes onto
    lines saved before barcode stamping.

    Returns the company's line_item_identifier mode (it loads Company anyway),
    so the caller can pass it to the renderer without a second lookup."""
    from celerp.models.company import Company
    from celerp.services.line_measures import (
        LINE_IDENTIFIER_MODES, identifier_backfill, item_measure_meta, resolve_line_measures)
    from celerp.services.shipping import SHIPPING_LIST_TYPE, customs_backfill, line_gross_weight
    from celerp.services.units import DEFAULT_UNITS, build_unit_map
    company = await session.get(Company, company_id)
    settings = (company.settings or {}) if company else {}
    ident_mode = settings.get("line_item_identifier")
    if ident_mode not in LINE_IDENTIFIER_MODES:
        ident_mode = "sku"
    invoice_layout = state.get("doc_type") in INVOICE_LAYOUT_DOC_TYPES
    if not invoice_layout and ident_mode == "sku":
        return ident_mode
    units = settings.get("units") or DEFAULT_UNITS
    umap = build_unit_map(units)
    is_shipping = state.get("list_type") == SHIPPING_LIST_TYPE
    for li in state.get("line_items") or []:
        eid = li.get("entity_id") or li.get("item_id")
        if not eid:
            continue
        irow = await session.get(Projection, (company_id, eid))
        if irow is None or irow.entity_type != "item":
            continue
        if ident_mode != "sku":
            identifier_backfill(li, irow.state or {})
        if not invoice_layout:
            continue
        meta = item_measure_meta(irow.state or {}, umap)
        li["pieces"], li["weight"], li["weight_unit"], _, _ = resolve_line_measures(li, item_meta=meta)
        if is_shipping:
            customs_backfill(li, irow.state or {})
            li["gross_weight"], li["gross_weight_unit"] = line_gross_weight(
                li, irow.state or {}, bool(meta.get("qty_is_weight")))
    return ident_mode


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
        if "contact_name" not in state:
            fallback_name = state.get("receiver") or state.get("customer_name")
            if fallback_name:
                state["contact_name"] = fallback_name
        if not state.get("issue_date"):
            state["issue_date"] = state.get("created_at") or state.get("date")
    for key, value in (await _letterhead(session, share_row.company_id)).items():
        if key not in state and value:
            state[key] = value
    await _resolve_share_contact(session, share_row.company_id, state)
    ident_mode = await _enrich_share_lines(session, share_row.company_id, state)

    importable = is_shareable(row.entity_type, state)
    # The page points its Import link at the address it is read from, which
    # behind share.celerp.com only the browser knows.
    base = (settings.celerp_public_url or "").rstrip("/")
    import_url = (import_accept_url(f"{base}/share/{token}") if base else IMPORT_ACCEPT_URL) if importable else None
    # Online payment: offered on money-carrying, payable doc types when this
    # instance has Stripe connected. The renderer drops the bar once nothing
    # is outstanding, so a paid invoice's link quietly reverts to view-only.
    pay_url = None
    from celerp.services import payments as _pay
    if _pay.payments_enabled() and state.get("doc_type") in ("invoice", "proforma"):
        pay_url = f"/pay/{token}"
    html = render_doc_print_html(
        state,
        import_url=import_url,
        import_from_page=True,
        pay_url=pay_url,
        line_identifier=ident_mode,
    )
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

    doc = dict(row.state or {})
    if row.entity_type == "list":
        doc.setdefault("doc_type", share_doc_type(row.entity_type, doc))
    for key, value in (await _letterhead(session, share_row.company_id)).items():
        if key not in doc and value:
            doc[key] = value
    await _resolve_share_contact(session, share_row.company_id, doc)
    public_doc = _public_bundle_doc(doc)
    ref = public_doc.get("ref_id") or public_doc.get("doc_number") or share_row.entity_id
    from celerp.config import ensure_instance_id
    bundle = {
        "version": 1,
        "doc": public_doc,
        # Stable identity of this document, whichever link or file carries it:
        # the recipient files every revision of it under one Received entry.
        # The company is named by a digest of the installation and company ids,
        # stable for the recipient without revealing either id.
        "source": {
            "installation": hashlib.sha256(ensure_instance_id().encode()).hexdigest(),
            "company": hashlib.sha256(f"{ensure_instance_id()}\n{share_row.company_id}".encode()).hexdigest(),
            "document": share_row.entity_id,
            "revision": row.version,
        },
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
    link: str | None = Query(None, description="The share link: the page the sender's document is shown on"),
    src: str | None = Query(None, description="Older link form: the sender's public address, given with token"),
    token: str | None = Query(None, description="Older link form: the share token, given with src"),
    company_id: _uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Fetch a document another Celerp shared and file it in Received.

    Nothing is booked: the recipient reviews it there and books it explicitly.
    """
    page = await _validate_share_link(_import_link(link, src, token))
    fetch_url = f"{page}/bundle"

    try:
        from celerp.services.outbound_url import (
            PublicFetchTooLarge,
            fetch_public_bytes,
        )

        r = await fetch_public_bytes(
            fetch_url,
            max_bytes=_MAX_BUNDLE_BYTES,
            timeout=_FETCH_TIMEOUT,
        )
        if r.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="Share link not found on sender's instance",
            )
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Sender's instance returned {r.status_code}",
            )
        bundle = json.loads(r.content)
    except PublicFetchTooLarge as exc:
        raise HTTPException(status_code=413, detail="Bundle too large") from exc
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Could not reach sender's Celerp instance")

    return await _import_bundle(bundle, company_id, user.id, session, page)


@public_router.post("/docs/import-bundle")
async def import_bundle_upload(
    request: Request,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
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

    return await _import_bundle(bundle, company_id, user.id, session, None)


# ---------------------------------------------------------------------------
# Shared import helper
# ---------------------------------------------------------------------------

async def _import_bundle(
    bundle: dict,
    company_id: _uuid.UUID,
    actor_id: _uuid.UUID,
    session: AsyncSession,
    link: str | None,
) -> Response:
    """File a .celerp bundle in Received. Returns a redirect to the Received entry."""
    if not isinstance(bundle, dict):
        raise HTTPException(status_code=422, detail="Bundle is malformed")
    doc = bundle.get("doc") or {}
    if not isinstance(doc, dict) or not doc:
        raise HTTPException(status_code=422, detail="Bundle contains no document data")

    # Accept only known fields and recompute money locally — never trust the bundle.
    document = _sanitize_bundle_doc(doc)
    rid = await received.record_received(
        session, company_id, actor_id, bundle=bundle, document=document, link=link,
    )
    await session.commit()

    return Response(
        status_code=302,
        headers={"Location": f"/docs/received/{rid}"},
    )
