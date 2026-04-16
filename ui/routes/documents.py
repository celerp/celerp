# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from urllib.parse import urlencode

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import search_bar, EMPTY, pagination, searchable_select, breadcrumbs, status_cards, empty_state_cta, fmt_money, format_value, currency_symbol
from ui.components.activity import activity_table
from ui.config import get_token as _token, get_role as _get_role
from ui.i18n import t, get_lang
from ui.routes.reports import _date_filter_bar, _parse_dates, _resolve_preset

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lists constants
# ---------------------------------------------------------------------------
_LIST_TYPES = ["quotation", "transfer", "audit"]
_LIST_DATE_FIELDS = {"date", "link_expiry"}

_PER_PAGE = 50
_PER_PAGE_OPTIONS = [25, 50, 100, 250]
_DOC_TYPES = ["invoice", "purchase_order", "bill", "receipt", "credit_note", "memo", "consignment_in", "list"]
_DOC_TYPE_PAGE_LABELS: dict[str, str] = {
    "invoice": "Invoices",
    "purchase_order": "Draft Bills & POs",
    "bill": "Vendor Bills",
    "receipt": "Receipts",
    "credit_note": "Credit Notes",
    "memo": "Consignment Out",
    "consignment_in": "Consignment In",
    "list": "Lists",
}
_DOC_TYPE_NEW_LABEL_KEYS: dict[str, str] = {
    "invoice": "btn.new_invoice",
    "purchase_order": "btn.new_purchase_order",
    "bill": "btn.new_bill",
    "receipt": "btn.new_receipt",
    "credit_note": "btn.new_credit_note",
    "memo": "btn.new_memo",
    "consignment_in": "btn.new_consignment_in",
    "list": "btn.new_list",
}


def _doc_type_new_label(doc_type: str, lang: str = "en") -> str:
    """Return localised "New <DocType>" label."""
    key = _DOC_TYPE_NEW_LABEL_KEYS.get(doc_type)
    if key:
        return t(key, lang)
    return t("btn.new_document", lang)
_DOC_STATUSES = ["draft", "sent", "paid", "overdue", "void", "open", "awaiting_payment", "converted", "expired", "bill", "final"]

# Singular labels for doc detail pages / breadcrumbs
_DOC_TYPE_SINGULAR: dict[str, str] = {
    "invoice": "Invoice",
    "purchase_order": "Purchase Order",
    "bill": "Vendor Bill",
    "receipt": "Receipt",
    "credit_note": "Credit Note",
    "memo": "Consignment Out",
    "consignment_in": "Consignment In",
    "list": "List",
}


def _doc_section_label(doc_type: str) -> str:
    """Section label for breadcrumb (plural)."""
    return _DOC_TYPE_PAGE_LABELS.get(doc_type, "Documents")


def _doc_section_url(doc_type: str) -> str:
    """URL for the doc type's listing page."""
    if doc_type == "list":
        return "/lists"
    return f"/docs?type={doc_type}" if doc_type else "/docs"


def _doc_singular_label(doc_type: str) -> str:
    """Singular label for a doc type (e.g. 'Invoice', 'Purchase Order')."""
    return _DOC_TYPE_SINGULAR.get(doc_type, doc_type.replace("_", " ").title() if doc_type else "Document")




from datetime import date as _date, timedelta as _timedelta


def _calculate_due_date(issue_date: str | None, payment_terms_name: str | None, terms_list: list[dict]) -> str | None:
    """Return ISO due_date string given an issue_date + payment_terms name + company terms list.

    Returns None if any input is missing/invalid so callers can skip the patch.
    """
    if not issue_date or not payment_terms_name:
        return None
    term = next((item for item in terms_list if item.get("name") == payment_terms_name), None)
    if term is None:
        return None
    days = term.get("days")
    if days is None:
        return None
    try:
        base = _date.fromisoformat(str(issue_date)[:10])
    except (ValueError, TypeError):
        return None
    return (base + _timedelta(days=int(days))).isoformat()


def resolve_price(item: dict, price_list: str) -> float:
    """Deterministic price lookup. No fallback chain.

    Checks the price list name directly on the item, then the conventional
    {name.lower()}_price key (e.g. "retail_price" for "Retail").
    Returns 0.0 if no price is found for this list.
    """
    val = item.get(price_list)
    if val is not None:
        return float(val)
    conventional_key = f"{price_list.lower()}_price"
    val = item.get(conventional_key)
    if val is not None:
        return float(val)
    return 0.0


async def _line_items_from_inventory(token: str, entity_ids: list[str], price_list: str = "Retail") -> list[dict]:
    """Fetch inventory items and build doc/list line items."""
    line_items = []
    for eid in entity_ids:
        try:
            item = await api.get_item(token, eid)
        except APIError:
            continue
        sku = item.get("sku", "")
        name = item.get("name", "")
        sell_by = item.get("sell_by") or "piece"
        unit_price = resolve_price(item, price_list)
        qty = float(item.get("quantity", 1)) if float(item.get("quantity", 1)) > 0 else 1
        desc = f"{sku} - {name}" if sku else name

        line_items.append({
            "description": desc,
            "quantity": qty,
            "unit_price": unit_price,
            "unit": sell_by,
            "sku": sku,
            "price_list": price_list,
            "hs_code": item.get("hs_code") or None,
            "entity_id": eid,
            "allow_splitting": bool(item.get("allow_splitting")),
        })
    return line_items


async def _company_doc_taxes(token: str) -> list[dict]:
    """Fetch company sales taxes and return them as doc_taxes dicts for new documents."""
    try:
        taxes = await api.get_taxes(token)
    except Exception:
        return []
    return [
        {"code": tax.get("name", "Tax"), "rate": float(tax.get("rate", 0)), "order": i, "is_compound": bool(tax.get("is_compound"))}
        for i, tax in enumerate(taxes)
        if tax.get("rate")
    ]


def _send_to_option_list(items: list[dict], kind: str) -> FT:
    """Render the searchable option list for send-to-modal (docs, lists, or memos)."""
    if not items:
        return Div(P(t("doc.no_results"), cls="send-to-empty"), id="send-to-options")
    rows = []
    for d in items:
        eid = d.get("id") or d.get("entity_id", "")
        if kind == "memo":
            ref = d.get("memo_number") or eid.split(":")[-1][:8]
            contact = d.get("contact_name") or d.get("customer_name") or ""
            label = f"Memo {ref}"
        elif kind == "list":
            ref = d.get("ref_id") or eid.split(":")[-1][:8]
            contact = d.get("customer_name") or d.get("receiver") or ""
            label = f"List {ref}"
        else:
            ref = d.get("ref_id") or d.get("doc_number") or eid.split(":")[-1][:8]
            contact = d.get("contact_name") or ""
            label = ref
        status = d.get("status", "")
        rows.append(
            Div(
                Input(type="radio", name="target_id", value=eid, cls="send-to-radio"),
                Span(label, cls="send-to-ref"),
                Span(contact, cls="send-to-contact") if contact else None,
                Span(status, cls=f"badge badge--{status}") if status else None,
                cls="send-to-option",
            )
        )
    return Div(*rows, id="send-to-options")


def _send_to_modal(
    type_label: str,
    create_url: str,
    add_url: str,
    search_url: str,
    drafts: list[dict],
    hidden_items: list[FT],
    kind: str,
) -> FT:
    """Unified send-to modal: create new draft or add to existing doc/list/memo."""
    return Div(
        Div(
            H3(f"Send to {type_label}", cls="modal-title"),
            # Create new draft
            Form(
                *hidden_items,
                Button(f"Create new draft {type_label.lower()}", type="submit", cls="btn btn--primary btn--full"),
                hx_post=create_url,
                hx_target="#modal-container",
                hx_swap="innerHTML",
                cls="send-to-create",
            ),
            Hr(),
            P(t("doc.or_add_to_an_existing_one"), cls="send-to-subtitle"),
            # Search input
            Input(
                type="search",
                name="q",
                placeholder=f"Search by ref, customer...",
                hx_get=search_url,
                hx_trigger="input changed delay:300ms",
                hx_target="#send-to-options",
                hx_swap="outerHTML",
                cls="form-input send-to-search",
                autocomplete="off",
            ),
            # Options list (pre-loaded with recent drafts)
            _send_to_option_list(drafts, kind),
            # Add to selected
            Form(
                *hidden_items,
                Input(type="hidden", name="target_id", value="", id="send-to-target-hidden"),
                Button(f"Add to selected {type_label.lower()}", type="submit", cls="btn btn--secondary btn--full",
                       id="send-to-add-btn", disabled=True),
                hx_post=add_url,
                hx_target="#modal-container",
                hx_swap="innerHTML",
                cls="send-to-add-form",
            ),
            # JS: sync radio selection to the hidden input + enable button
            Script("""
(function(){
  var opts = document.getElementById('send-to-options');
  var hidden = document.getElementById('send-to-target-hidden');
  var btn = document.getElementById('send-to-add-btn');
  if (!opts || !hidden) return;
  document.addEventListener('change', function(e) {
    if (e.target.name === 'target_id' && e.target.type === 'radio') {
      hidden.value = e.target.value;
      if (btn) { btn.disabled = false; btn.classList.add('btn--active'); }
    }
  });
})();
"""),
            Button(t("btn.cancel"), cls="btn btn--ghost btn--full send-to-cancel",
                   onclick="document.getElementById('modal-container').innerHTML=''"),
            cls="modal-body send-to-modal",
        ),
        id="modal-container",
        cls="modal-overlay",
    )


# Compact SVG icons for CSV export/import (16x16, matching pair)
_ICON_CSV_EXPORT = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><polyline points="9 15 12 18 15 15"/></svg>'
_ICON_CSV_IMPORT = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="12" x2="12" y2="18"/><polyline points="9 15 12 12 15 15"/></svg>'


def setup_routes(app):

    @app.get("/docs")
    async def docs_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        doc_type = request.query_params.get("type", "") or request.query_params.get("doc_type", "")
        status = request.query_params.get("status", "")
        view = request.query_params.get("view", "")  # "drafts" = drafts-only mode
        page = int(request.query_params.get("page", 1))
        sort = request.query_params.get("sort", "date")
        sort_dir = request.query_params.get("dir", "desc")
        try:
            per_page = max(1, int(request.query_params.get("per_page", _PER_PAGE)))
        except (ValueError, TypeError):
            per_page = _PER_PAGE

        # Date filter: use explicit URL param if set, otherwise fall back to
        # the per-company saved preference (default: last_12m).
        _has_explicit_date = (
            request.query_params.get("preset")
            or request.query_params.get("from")
            or request.query_params.get("to")
        )
        try:
            company = await api.get_company(token)
        except Exception:
            company = {}
        currency = company.get("currency") or None
        if _has_explicit_date:
            date_from, date_to, preset = _parse_dates(request)
        else:
            _default_preset = company.get("docs_default_preset") or "last_12m"
            if _default_preset == "all":
                date_from, date_to, preset = "", "", "all"
            else:
                date_from, date_to = _resolve_preset(_default_preset)
                preset = _default_preset

        # Drafts are segregated: only shown when ?view=drafts or explicit ?status=draft.
        # All other views exclude drafts by default (like email treats Drafts).
        is_drafts_view = view == "drafts" or status == "draft"
        effective_status = status
        if is_drafts_view:
            effective_status = "draft"
        elif not status:
            effective_status = "exclude_draft"  # backend must support this param

        try:
            params = {"limit": per_page, "offset": (page - 1) * per_page}
            if q:
                params["q"] = q
            if doc_type:
                params["doc_type"] = doc_type
            if effective_status == "exclude_draft":
                params["exclude_status"] = "draft"
            elif effective_status:
                params["status"] = effective_status
            if date_from and not is_drafts_view:
                params["date_from"] = date_from
            if date_to and not is_drafts_view:
                params["date_to"] = date_to
            docs_resp = await api.list_docs(token, params)
            docs = docs_resp.get("items", []) if isinstance(docs_resp, dict) else docs_resp
            # Fetch draft count for the badge (always unfiltered)
            draft_params = {"status": "draft", "limit": 1}
            if doc_type:
                draft_params["doc_type"] = doc_type
            draft_resp = await api.list_docs(token, {**draft_params, "limit": 250})
            draft_count = draft_resp.get("total", 0) if isinstance(draft_resp, dict) else len(draft_resp)
            summary = await api.get_doc_summary(token, doc_type=doc_type)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            docs, summary, draft_count = [], {}, 0

        extra = f"&q={q}&type={doc_type}&status={status}&view={view}".strip("&")
        total_count = summary.get("total_count", len(docs))
        lang = get_lang(request)
        page_title = _DOC_TYPE_PAGE_LABELS.get(doc_type, "Documents")
        new_label = _doc_type_new_label(doc_type, lang)
        search_url = f"/docs/search?type={doc_type}" if doc_type else "/docs/search"
        create_type = doc_type or "invoice"
        return base_shell(
            page_header(
                page_title,
                _drafts_tab(draft_count, is_drafts_view, doc_type, status=status, lang=lang),
                search_bar(
                    placeholder="Search doc number, contact...",
                    target="#doc-table",
                    url=search_url,
                ),
                Button(
                    new_label,
                    hx_post=f"/docs/create-blank?type={create_type}",
                    hx_swap="none",
                    cls="btn btn--primary",
                ),
                A(t("btn.export_csv"), href="/docs/export/csv", cls="btn btn--secondary"),
                A(t("doc.import_csv"), href="/docs/import", cls="btn btn--secondary"),
            ),
            *([] if is_drafts_view else [
                _date_filter_bar("/docs", date_from, date_to, preset, extra_params=f"&{extra}" if extra else "", lang=lang),
            ]),
            _summary_bar(summary, doc_type, currency, lang),
            _doc_status_cards(docs, status, summary, currency, doc_type=doc_type, lang=lang),
            _doc_table(
                docs,
                sort=sort,
                sort_dir=sort_dir,
                base_params={"q": q, "type": doc_type, "status": status, "view": view, "page": str(page), "per_page": str(per_page)},
                doc_type=doc_type if not is_drafts_view else doc_type,
                lang=lang,
            ),
            pagination(page, total_count, per_page, "/docs", f"q={q}&type={doc_type}&status={status}&view={view}&sort={sort}&dir={sort_dir}".strip("&")),
            title=f"{page_title} - Celerp",
            nav_active={"invoice": "invoices", "memo": "memos", "purchase_order": "purchase-orders", "bill": "vendor-bills", "consignment_in": "consignment-in"}.get(doc_type, "invoices"),
            lang=lang,
            request=request,
        )

    @app.get("/docs/search")
    async def docs_search(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        doc_type = request.query_params.get("type", "") or request.query_params.get("doc_type", "")
        status = request.query_params.get("status", "")
        page = int(request.query_params.get("page", 1))
        sort = request.query_params.get("sort", "date")
        sort_dir = request.query_params.get("dir", "desc")
        try:
            params = {"limit": _PER_PAGE, "offset": (page - 1) * _PER_PAGE}
            if q:
                params["q"] = q
            if doc_type:
                params["doc_type"] = doc_type
            if status:
                params["status"] = status
            docs = (await api.list_docs(token, params)).get("items", [])
        except APIError as e:
            docs = []
        return _doc_table(
            docs,
            sort=sort,
            sort_dir=sort_dir,
            base_params={"q": q, "type": doc_type, "status": status, "page": str(page)},
            doc_type=doc_type,
            lang=get_lang(request),
        )

    @app.get("/docs/export/csv")
    async def docs_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        params: dict = {}
        q = request.query_params.get("q", "")
        doc_type = request.query_params.get("type", "") or request.query_params.get("doc_type", "")
        status = request.query_params.get("status", "")
        if q:
            params["q"] = q
        if doc_type:
            params["doc_type"] = doc_type
        if status:
            params["status"] = status
        try:
            data = await api.export_docs_csv(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            data = b"error\n"
        from starlette.responses import Response
        return Response(
            content=data,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=documents.csv"},
        )

    @app.post("/docs/create-blank")
    async def create_blank_doc(request: Request):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401)
        doc_type = request.query_params.get("type", "invoice")
        try:
            result = await api.create_doc(token, {"doc_type": doc_type, "status": "draft"})
            entity_id = result.get("entity_id") or result.get("id", "")
            # Auto-populate default T&C template for this doc_type
            try:
                tc_templates = await api.get_terms_conditions(token)
                default_tc = next(
                    (tc for tc in tc_templates
                     if doc_type in (tc.get("default_for") or [])),
                    None,
                )
                if default_tc and entity_id:
                    await api.patch_doc(token, entity_id, {
                        "terms_template": default_tc["name"],
                        "terms_text": default_tc.get("text", ""),
                    })
            except Exception:
                pass  # Non-critical: doc still created, user can set T&C manually
        except APIError as e:
            if e.status == 401:
                from starlette.responses import Response as _R
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            from starlette.responses import Response as _R
            return _R("", status_code=500)
        from starlette.responses import Response as _R
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    @app.post("/docs/from-items")
    async def doc_from_items_modal(request: Request):
        """Modal: choose to create new draft invoice or add to existing."""
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        # Fetch recent draft invoices for the picker
        try:
            drafts_resp = await api.list_docs(token, {"status": "draft", "doc_type": "invoice", "limit": 20})
            drafts = drafts_resp.get("items", [])
        except APIError:
            drafts = []
        hidden_items = [Input(type="hidden", name="selected", value=eid) for eid in entity_ids]
        return _send_to_modal("Invoice", "/docs/from-items/new", "/docs/from-items/add",
                              "/docs/from-items/search", drafts, hidden_items, "doc")

    @app.post("/docs/from-items/new")
    async def create_doc_from_items(request: Request):
        """Create a draft invoice pre-populated with line items from selected inventory items."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="modal-container")
        line_items = await _line_items_from_inventory(token, entity_ids)
        doc_taxes = await _company_doc_taxes(token)
        try:
            result = await api.create_doc(token, {
                "doc_type": "invoice",
                "status": "draft",
                "line_items": line_items,
                "doc_taxes": doc_taxes,
            })
            doc_id = result.get("entity_id") or result.get("id", "")
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="modal-container")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{doc_id}"})

    @app.post("/docs/from-items/add")
    async def add_items_to_doc(request: Request):
        """Append line items from selected inventory to an existing document."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        target_id = str(form.get("target_id", "")).strip()
        if not entity_ids or not target_id:
            return Div(P(t("label.no_items_or_target_selected"), cls="flash flash--warning"), id="modal-container")
        new_lines = await _line_items_from_inventory(token, entity_ids)
        try:
            doc = await api.get_doc(token, target_id)
            existing_lines = doc.get("line_items") or []
            combined = existing_lines + new_lines
            subtotal = sum(l.get("quantity", 0) * l.get("unit_price", 0) for l in combined)
            await api.patch_doc(token, target_id, {
                "line_items": combined,
                "subtotal": subtotal,
                "total": subtotal,
            })
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="modal-container")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{target_id}"})

    @app.get("/docs/from-items/search")
    async def doc_from_items_search(request: Request):
        """HTMX search endpoint for the doc picker dropdown."""
        token = _token(request)
        if not token:
            return Div()
        q = request.query_params.get("q", "").strip()
        try:
            resp = await api.list_docs(token, {"doc_type": "invoice", "q": q, "limit": 20} if q else {"status": "draft", "doc_type": "invoice", "limit": 20})
            docs = resp.get("items", [])
        except APIError:
            docs = []
        return _send_to_option_list(docs, "doc")
    @app.get("/docs/catalog-lookup")
    async def doc_catalog_lookup(request: Request):
        """Lookup item by SKU or barcode. Returns {sku, description, unit_price} or {}."""
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        code = request.query_params.get("sku", "").strip()
        if not code:
            return JSONResponse({})
        price_list = request.query_params.get("price_list", "Retail").strip() or "Retail"

        def _extract(item: dict) -> dict:
            return {
                "sku": item.get("sku") or "",
                "description": item.get("name") or item.get("description") or "",
                "unit_price": resolve_price(item, price_list),
                "sell_by": item.get("sell_by") or None,
                "quantity": item.get("quantity") or 0,
                "hs_code": item.get("hs_code") or None,
                "entity_id": item.get("entity_id") or None,
                "allow_splitting": bool(item.get("allow_splitting")),
            }

        try:
            # Try exact SKU match first
            resp = await api.list_items(token, {"sku": code, "limit": 1})
            items = resp.get("items", []) if isinstance(resp, dict) else resp
            if items:
                return JSONResponse(_extract(items[0]))
            # Try exact barcode match
            resp = await api.list_items(token, {"barcode": code, "limit": 1})
            items = resp.get("items", []) if isinstance(resp, dict) else resp
            if items:
                return JSONResponse(_extract(items[0]))
            # Fallback: general search (name, description, attributes)
            resp = await api.list_items(token, {"q": code, "limit": 1})
            items = resp.get("items", []) if isinstance(resp, dict) else resp
            if items:
                return JSONResponse(_extract(items[0]))
        except Exception:
            pass
        return JSONResponse({})

    @app.get("/docs/catalog-search")
    async def doc_catalog_search(request: Request):
        """Search inventory items by SKU or name. Returns [{sku, description, unit_price, sell_by}]."""
        from starlette.responses import JSONResponse as _J
        token = _token(request)
        if not token:
            return _J({"error": "unauthorized"}, status_code=401)
        q = request.query_params.get("q", "").strip()
        price_list = request.query_params.get("price_list", "Retail").strip() or "Retail"
        if not q:
            return _J([])

        def _extract(item: dict) -> dict:
            return {
                "sku": item.get("sku") or "",
                "description": item.get("name") or item.get("description") or "",
                "unit_price": resolve_price(item, price_list),
                "sell_by": item.get("sell_by") or None,
                "hs_code": item.get("hs_code") or None,
                "quantity": item.get("quantity") or 0,
                "entity_id": item.get("entity_id") or None,
                "allow_splitting": bool(item.get("allow_splitting")),
            }

        try:
            resp = await api.list_items(token, {"q": q, "limit": 10})
            items = resp.get("items", []) if isinstance(resp, dict) else resp
            return _J([_extract(i) for i in items])
        except Exception:
            return _J([])

    # ── Line item CSV export/import ─────────────────────────────────

    _CSV_COLUMNS = ["sku", "description", "quantity", "unit", "unit_price", "discount_pct", "tax_code", "tax_rate", "hs_code", "account_code"]
    _CSV_ALIASES: dict[str, str] = {
        "item": "sku", "item_code": "sku", "code": "sku", "product": "sku", "barcode": "sku",
        "name": "description", "desc": "description", "item_name": "description", "product_name": "description",
        "qty": "quantity", "amount": "quantity",
        "price": "unit_price", "rate": "unit_price", "unit price": "unit_price",
        "discount": "discount_pct", "disc": "discount_pct", "disc_pct": "discount_pct", "discount_percent": "discount_pct",
        "tax": "tax_rate", "tax_pct": "tax_rate", "vat": "tax_rate", "vat_rate": "tax_rate",
        "tax_name": "tax_code", "tax code": "tax_code",
        "hs": "hs_code", "hs code": "hs_code", "tariff": "hs_code",
        "account": "account_code", "gl_code": "account_code",
    }

    def _map_csv_header(header: str) -> str | None:
        """Map a CSV header to a canonical column name, or None if unmapped."""
        h = header.strip().lower().replace("-", "_").replace(" ", "_")
        if h in _CSV_COLUMNS:
            return h
        return _CSV_ALIASES.get(h)

    async def _export_line_items_csv(request: Request, entity_id: str):
        """Shared: export a doc/list's line items as CSV."""
        from starlette.responses import Response as _Resp
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return _Resp(content=f"Error: {e.detail}", status_code=e.status)
        line_items = doc.get("line_items") or []
        doc_ref = (doc.get("ref_id") or doc.get("doc_number") or entity_id).replace(" ", "_")

        import io as _io, csv as _csv
        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(_CSV_COLUMNS)
        for li in line_items:
            writer.writerow([
                li.get("sku") or "",
                li.get("description") or li.get("name") or "",
                li.get("quantity", 0),
                li.get("unit") or "",
                li.get("unit_price", 0),
                li.get("discount_pct") or 0,
                li.get("tax_code") or "",
                li.get("tax_rate") or 0,
                li.get("hs_code") or "",
                li.get("account_code") or "",
            ])
        return _Resp(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{doc_ref}_items.csv"'},
        )

    @app.get("/docs/{entity_id}/items/csv")
    async def doc_items_export_csv(request: Request, entity_id: str):
        """Export a document's line items as CSV."""
        return await _export_line_items_csv(request, entity_id)

    @app.post("/docs/{entity_id}/items/csv")
    async def doc_items_import_csv(request: Request, entity_id: str):
        """Import line items from CSV and append to document."""
        from starlette.responses import JSONResponse as _J
        token = _token(request)
        if not token:
            return _J({"error": "unauthorized"}, status_code=401)
        form = await request.form()
        upload = form.get("file")
        if not upload or not hasattr(upload, "read"):
            return _J({"error": t("doc.csv_no_file")}, status_code=400)

        import io as _io, csv as _csv
        try:
            raw = (await upload.read()).decode("utf-8-sig")
        except Exception:
            return _J({"error": t("doc.csv_decode_error")}, status_code=400)
        reader = _csv.DictReader(_io.StringIO(raw))
        if not reader.fieldnames:
            return _J({"error": t("doc.csv_no_headers")}, status_code=400)

        # Map CSV headers to canonical names
        col_map: dict[str, str] = {}
        for h in reader.fieldnames:
            mapped = _map_csv_header(h)
            if mapped:
                col_map[h] = mapped

        if "sku" not in col_map.values() and "description" not in col_map.values():
            return _J({"error": t("doc.csv_missing_sku_or_description")}, status_code=400)

        # Parse rows
        new_lines: list[dict] = []
        price_list = form.get("price_list") or "Retail"
        for row in reader:
            mapped_row: dict[str, str] = {}
            for csv_col, canon in col_map.items():
                mapped_row[canon] = row.get(csv_col, "").strip()
            # Skip empty rows
            if not mapped_row.get("sku") and not mapped_row.get("description"):
                continue

            sku = mapped_row.get("sku", "")
            desc = mapped_row.get("description", "")
            qty_str = mapped_row.get("quantity", "1")
            price_str = mapped_row.get("unit_price", "")
            disc_str = mapped_row.get("discount_pct", "0")
            unit = mapped_row.get("unit", "")
            tax_code = mapped_row.get("tax_code", "")
            tax_rate_str = mapped_row.get("tax_rate", "0")
            hs_code = mapped_row.get("hs_code", "")
            account_code = mapped_row.get("account_code", "")

            try:
                qty = float(qty_str) if qty_str else 1
            except ValueError:
                qty = 1
            try:
                unit_price = float(price_str) if price_str else None
            except ValueError:
                unit_price = None
            try:
                discount_pct = float(disc_str) if disc_str else 0
            except ValueError:
                discount_pct = 0
            try:
                tax_rate = float(tax_rate_str) if tax_rate_str else 0
            except ValueError:
                tax_rate = 0

            # Resolve SKU against inventory catalog
            catalog_item: dict = {}
            if sku:
                try:
                    resp = await api.list_items(token, {"sku": sku, "limit": 1})
                    items = resp.get("items", []) if isinstance(resp, dict) else resp
                    if items:
                        catalog_item = items[0]
                    else:
                        resp = await api.list_items(token, {"barcode": sku, "limit": 1})
                        items = resp.get("items", []) if isinstance(resp, dict) else resp
                        if items:
                            catalog_item = items[0]
                except Exception:
                    pass

            line = {
                "sku": sku or (catalog_item.get("sku") or ""),
                "description": desc or catalog_item.get("name") or "",
                "quantity": qty,
                "unit": unit or catalog_item.get("sell_by") or "",
                "unit_price": unit_price if unit_price is not None else resolve_price(catalog_item, price_list) if catalog_item else 0,
                "discount_pct": discount_pct,
                "tax_code": tax_code,
                "tax_rate": tax_rate,
                "hs_code": hs_code or catalog_item.get("hs_code") or "",
                "account_code": account_code,
                "entity_id": catalog_item.get("entity_id") or "",
                "allow_splitting": bool(catalog_item.get("allow_splitting")),
            }
            new_lines.append(line)

        if not new_lines:
            return _J({"error": t("doc.csv_no_valid_rows")}, status_code=400)

        # Append to existing line items
        try:
            doc = await api.get_doc(token, entity_id)
            existing = doc.get("line_items") or []
            combined = existing + new_lines
            subtotal = sum(
                float(l.get("quantity", 0)) * float(l.get("unit_price", 0)) * (1 - float(l.get("discount_pct", 0)) / 100)
                for l in combined
            )
            await api.patch_doc(token, entity_id, {
                "line_items": combined,
                "subtotal": subtotal,
                "total": subtotal,
            })
        except APIError as e:
            return _J({"error": str(e.detail)}, status_code=e.status)

        return _J({"ok": True, "imported": len(new_lines)})

    # Same export for lists
    @app.get("/lists/{entity_id}/items/csv")
    async def list_items_export_csv(request: Request, entity_id: str):
        """Export a list's line items as CSV - delegates to shared handler."""
        return await _export_line_items_csv(request, entity_id)

    @app.post("/lists/{entity_id}/items/csv")
    async def list_items_import_csv(request: Request, entity_id: str):
        """Import line items from CSV and append to list - delegates to doc handler."""
        return await doc_items_import_csv(request, entity_id)

    @app.get("/docs/{entity_id}/pdf")
    async def doc_pdf_proxy(request: Request, entity_id: str):
        """Proxy PDF generation from the API app so the browser can access it on the UI port."""
        from starlette.responses import Response as _Resp
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            import httpx
            from ui.config import API_BASE
            async with httpx.AsyncClient(base_url=API_BASE, headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as c:
                r = await c.get(f"/docs/{entity_id}/pdf")
                if r.status_code != 200:
                    return _Resp(content=f"PDF generation failed ({r.status_code})", status_code=r.status_code)
                return _Resp(
                    content=r.content,
                    media_type=r.headers.get("content-type", "application/pdf"),
                    headers={"Content-Disposition": r.headers.get("content-disposition", f'inline; filename="{entity_id}.pdf"')},
                )
        except Exception as e:
            return _Resp(content=f"PDF error: {e}", status_code=500)

    @app.get("/docs/{entity_id}")
    async def doc_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            doc = await api.get_doc(token, entity_id)
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if isinstance(e, APIError) and e.status == 404:
                from starlette.responses import HTMLResponse as _HR
                return _HR("<h2>Document not found</h2><p><a href='/docs'>Back to Documents</a></p>", status_code=404)
            doc = {}

        # Inject company fields so "My company info" box is populated
        if not doc.get("company_name"):
            try:
                company = await api.get_company(token)
                doc = {
                    **doc,
                    "company_name": company.get("name") or "",
                    "company_address": company.get("address") or company.get("settings", {}).get("address") or "",
                    "company_phone": company.get("phone") or company.get("settings", {}).get("phone") or "",
                    "company_tax_id": company.get("tax_id") or company.get("settings", {}).get("tax_id") or "",
                    "company_email": company.get("email") or company.get("settings", {}).get("email") or "",
                }
            except Exception:
                pass

        # Resolve contact details if contact_id set but name missing
        cid = doc.get("contact_id")
        _resolved_contact: dict | None = None
        if cid and not doc.get("contact_name"):
            try:
                _resolved_contact = await api.get_contact(token, cid)
                doc["contact_name"] = _resolved_contact.get("name") or ""
                doc["contact_company_name"] = _resolved_contact.get("company_name") or ""
                doc["contact_email"] = _resolved_contact.get("email") or ""
                doc["contact_phone"] = _resolved_contact.get("phone") or ""
                doc["contact_tax_id"] = _resolved_contact.get("tax_id") or ""
            except Exception:
                pass

        # Resolve default billing/shipping address from contact if not yet stored on doc
        if cid and (not doc.get("contact_billing_address") or not doc.get("contact_shipping_address")):
            try:
                contact = _resolved_contact or await api.get_contact(token, cid)
                addresses = contact.get("addresses") or []
                def _resolve_addr(addr_type: str) -> str:
                    default = next((a for a in addresses if a.get("address_type") == addr_type and a.get("is_default")), None)
                    if default:
                        return default.get("full_address") or default.get("address") or default.get("label") or ""
                    first = next((a for a in addresses if a.get("address_type") == addr_type), None)
                    if first:
                        return first.get("full_address") or first.get("address") or first.get("label") or ""
                    return contact.get(f"{addr_type}_address") or ""
                if not doc.get("contact_billing_address"):
                    doc["contact_billing_address"] = doc.get("contact_address") or _resolve_addr("billing")
                if not doc.get("contact_shipping_address"):
                    doc["contact_shipping_address"] = _resolve_addr("shipping")
                # Also resolve shipping attn from contact address
                if not doc.get("shipping_attn"):
                    default_ship = next((a for a in addresses if a.get("address_type") == "shipping" and a.get("is_default")), None)
                    first_ship = default_ship or next((a for a in addresses if a.get("address_type") == "shipping"), None)
                    if first_ship and first_ship.get("attn"):
                        doc["shipping_attn"] = first_ship["attn"]
            except Exception:
                pass
        # Backward compat: migrate contact_address → contact_billing_address
        if not doc.get("contact_billing_address") and doc.get("contact_address"):
            doc["contact_billing_address"] = doc["contact_address"]

        # Fetch locations for receive-goods dropdown (PO + consignment_in) + company address picker
        locations: list[dict] = []
        doc_type = doc.get("doc_type", "")
        company_locations: list[dict] = []
        try:
            loc_resp = await api.get_locations(token)
            _all_locs = loc_resp.get("items") or loc_resp.get("locations") or (loc_resp if isinstance(loc_resp, list) else [])
            if not isinstance(_all_locs, list):
                _all_locs = []
            locations = _all_locs if doc_type in ("purchase_order", "consignment_in", "bill") else []
            company_locations = _all_locs
        except Exception:
            locations = []
            company_locations = []

        # Fetch document history (ledger entries)
        ledger: list[dict] = []
        try:
            ledger_resp = await api.list_ledger(token, {"entity_id": entity_id, "limit": 50})
            ledger = ledger_resp.get("items", []) if isinstance(ledger_resp, dict) else []
        except Exception:
            ledger = []

        doc_ref = doc.get("ref_id") or doc.get("doc_number") or doc.get("ref") or doc.get("external_id") or "Document"
        status = doc.get("status", "draft")

        # Fetch price lists for price list dropdown on doc detail
        price_lists: list[dict] = []
        try:
            price_lists = await api.get_price_lists(token)
        except Exception:
            pass
        # Fetch T&C templates for dropdown on doc detail
        tc_templates: list[dict] = []
        try:
            tc_templates = await api.get_terms_conditions(token)
        except Exception:
            pass
        # Fetch company timezone for notes display
        tz: str = "UTC"
        try:
            _co = await api.get_company(token)
            tz = _co.get("timezone") or "UTC"
        except Exception:
            pass
        company_taxes: list[dict] = []
        try:
            company_taxes = await api.get_taxes(token)
        except Exception:
            pass
        # Fetch bank accounts for payment section
        bank_accounts: list[dict] = []
        if doc_type in ("invoice", "bill", "credit_note"):
            try:
                ba_resp = await api.get_bank_accounts(token)
                bank_accounts = ba_resp.get("accounts", []) if isinstance(ba_resp, dict) else ba_resp
                if not isinstance(bank_accounts, list):
                    bank_accounts = []
            except Exception:
                pass
        # Draft invoices use proforma numbering by design - label accordingly
        status_label = "Pro Forma" if doc_type == "invoice" and status == "draft" else status.replace("_", " ").title()
        type_label = _doc_singular_label(doc_type)
        section_label = _doc_section_label(doc_type)
        section_url = _doc_section_url(doc_type)
        back_url = _doc_section_url(doc_type)
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), (section_label, section_url), (f"{status_label} {doc_ref}", None)]),
            page_header(f"{type_label} - {status_label} {doc_ref}"),
            _doc_detail(doc, locations=locations, ledger=ledger, price_lists=price_lists, tc_templates=tc_templates, tz=tz, company_taxes=company_taxes, bank_accounts=bank_accounts, company_locations=company_locations, role=_get_role(request)),
            title=f"{type_label} {doc_ref} - Celerp",
            nav_active={"invoice": "invoices", "memo": "memos", "purchase_order": "purchase-orders", "bill": "vendor-bills", "consignment_in": "consignment-in"}.get(doc_type, "invoices"),
            request=request,
        )

    @app.get("/docs/{entity_id}/field/{field}/display")
    async def doc_field_display(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        # Resolve contact fields to display names
        if field in ("contact_id", "commission_contact_id"):
            display_value = _resolve_contact_display(doc, field)
        elif field == "contact_company_name":
            display_value = doc.get("contact_company_name")
        else:
            display_value = doc.get(field)
        return _doc_display_cell(entity_id, field, display_value)

    @app.get("/docs/{entity_id}/field/{field}/edit")
    async def doc_field_edit(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        value = str(doc.get(field, "") or "")

        restore_url = f"/docs/{entity_id}/field/{field}/display"
        esc_js = (
            f"if(event.key==='Escape'){{"
            f"htmx.ajax('GET','{restore_url}',{{target:this.closest('.editable-cell'),swap:'outerHTML'}});"
            f"event.preventDefault();}}"
        )
        enter_js = "if(event.key==='Enter'){event.preventDefault();this.blur();}"
        blur_restore = f"htmx.ajax('GET','{restore_url}',{{target:this.closest('.editable-cell'),swap:'outerHTML'}})"
        combobox_esc_js = (
            f"if(event.key==='Escape'){{"
            f"htmx.ajax('GET','{restore_url}',{{target:this.closest('.editable-cell'),swap:'outerHTML'}});"
            f"event.preventDefault();}}"
        )
        if field == "status":
            input_el = Select(
                *[Option(s, value=s, selected=(s == value)) for s in _DOC_STATUSES],
                name="value",
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="change", cls="cell-input cell-input--select", autofocus=True,
                onkeydown=esc_js, onblur=blur_restore,
            )
        elif field == "purchase_kind":
            opts = ["inventory", "expense", "asset"]
            input_el = Select(
                *[Option(o, value=o, selected=(o == value)) for o in opts],
                name="value",
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="change", cls="cell-input cell-input--select", autofocus=True,
                onkeydown=esc_js, onblur=blur_restore,
            )
        elif field in ("issue_date", "due_date", "valid_until"):
            display_value = value[:10] if value else ""
            if not display_value and field == "issue_date":
                from datetime import date
                display_value = date.today().isoformat()
            input_el = Input(
                type="date", name="value", value=display_value,
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js + enter_js,
                onblur=f"if(!this.value.trim() && !this.dataset.dirty){{{blur_restore}}}",
                oninput="this.dataset.dirty='1'",
                data_orig=value,
            )
        elif field == "price_list":
            # Searchable dropdown of company price lists
            try:
                price_lists = await api.get_price_lists(token)
            except APIError:
                price_lists = []
            pl_names = [pl.get("name", "") for pl in price_lists]
            input_el = Select(
                Option(t("doc._default"), value=""),
                *[Option(name, value=name, selected=(name == value)) for name in pl_names],
                name="value",
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="change",
                cls="cell-input cell-input--select", autofocus=True,
                onkeydown=esc_js,
            )
        elif field in ("contact_id", "commission_contact_id", "contact_company_name"):
            # Searchable contact picker.
            # - commission_contact_id: always vendor-only
            # - contact_id: customer docs → customers; vendor docs → vendors
            # - contact_company_name: same filtering as contact_id but selects by company_name → resolves contact_id
            _VENDOR_TYPES = ("purchase_order", "bill", "consignment_in")
            doc_type_for_filter = doc.get("doc_type", "")
            if field == "commission_contact_id":
                contact_filter = "vendor"
            elif doc_type_for_filter in _VENDOR_TYPES:
                contact_filter = "vendor"
            else:
                contact_filter = "customer"
            try:
                contact_resp = await api.list_contacts(token, {"limit": 500, "contact_type": contact_filter})
                contacts = contact_resp.get("items", [])
            except APIError:
                contacts = []
            if field == "contact_company_name":
                # Options are (entity_id, company_name) so selecting a company resolves the contact
                contact_opts = [
                    (c.get("entity_id") or c.get("id") or "", c.get("company_name") or c.get("name") or "")
                    for c in contacts if c.get("company_name")
                ]
                # Current value is the company_name string; find current contact_id for pre-selection
                current_contact_id = doc.get("contact_id") or ""
                pre_val = current_contact_id
                patch_url = f"/docs/{entity_id}/field/contact_id"
            else:
                contact_opts = [(c.get("entity_id") or c.get("id") or "", c.get("name") or c.get("entity_id") or c.get("id") or "") for c in contacts]
                contact_opts.append(("__new__", "+ Add new contact"))
                pre_val = value
                patch_url = f"/docs/{entity_id}/field/{field}"
            # Fix #1: wrap combobox in div so ESC keydown bubbles up and can restore the display cell
            input_el = Div(
                searchable_select(
                    name="value",
                    options=contact_opts,
                    value=pre_val,
                    placeholder="Search contacts...",
                    hx_patch=patch_url,
                    hx_target="closest .editable-cell",
                    hx_swap="outerHTML",
                    hx_trigger="change",
                ),
                onkeydown=combobox_esc_js,
            )
        elif field in ("contact_billing_address", "contact_shipping_address"):
            # Address dropdown from contact's saved addresses
            addr_type = "billing" if field == "contact_billing_address" else "shipping"
            contact_id = doc.get("contact_id") or ""
            addr_opts: list[tuple[str, str]] = []
            if contact_id:
                try:
                    contact = await api.get_contact(token, contact_id)
                    addresses = contact.get("addresses") or []
                    typed = [a for a in addresses if a.get("address_type") == addr_type]
                    for a in typed:
                        label = a.get("label") or a.get("line1") or a.get("street") or str(a)
                        addr_str = a.get("full_address") or a.get("address") or label
                        addr_opts.append((addr_str, addr_str))
                    # Only use top-level field if no typed addresses found
                    if not typed:
                        top_addr = contact.get(f"{addr_type}_address") or ""
                        if top_addr:
                            addr_opts.append((top_addr, top_addr))
                except Exception:
                    pass
            if not addr_opts:
                # Fall back to plain text input if no addresses available
                input_el = Input(
                    type="text", name="value", value=value,
                    hx_patch=f"/docs/{entity_id}/field/{field}",
                    hx_target="closest .editable-cell", hx_swap="outerHTML",
                    hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                    onkeydown=esc_js + enter_js,
                    oninput="this.dataset.dirty='1'",
                )
            else:
                auto_select = len(addr_opts) == 1 and not value
                input_el = Select(
                    Option("-- Select address --", value="", selected=not value and not auto_select),
                    *[Option(lbl, value=val, selected=(val == value) or (auto_select and i == 0)) for i, (val, lbl) in enumerate(addr_opts)],
                    name="value",
                    hx_patch=f"/docs/{entity_id}/field/{field}",
                    hx_target="closest .editable-cell", hx_swap="outerHTML",
                    hx_trigger="change" + (", load" if auto_select else ""), cls="cell-input cell-input--select", autofocus=True,
                    onkeydown=esc_js, onblur=blur_restore,
                )
        else:
            input_el = Input(
                type="text", name="value", value=value,
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js + enter_js,
                onblur=f"if(!this.value.trim() && !this.dataset.dirty){{{blur_restore}}}",
                oninput="this.dataset.dirty='1'",
                data_orig=value,
            )
        return Div(input_el, cls="editable-cell editable-cell--editing")

    @app.patch("/docs/{entity_id}/field/{field}")
    async def doc_field_patch(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))
        if value == "__new__":
            from starlette.responses import Response as _R
            # Route to vendors page for vendor doc types and commission contacts
            _VENDOR_TYPES = ("purchase_order", "bill", "consignment_in")
            try:
                doc = await api.get_doc(token, entity_id)
                is_vendor_context = field == "commission_contact_id" or doc.get("doc_type") in _VENDOR_TYPES
            except APIError:
                is_vendor_context = field == "commission_contact_id"
            target = "/contacts/vendors" if is_vendor_context else "/contacts/customers"
            return _R("", status_code=204, headers={"HX-Redirect": target})
        try:
            patch = {field: value}
            # Auto-populate payment_terms and price_list from contact when contact_id changes
            if field == "contact_id" and value:
                try:
                    contact = await api.get_contact(token, value)
                    # Store contact details for display on the doc
                    contact_name = contact.get("name") or contact.get("display_name")
                    if contact_name:
                        patch["contact_name"] = contact_name
                    patch["contact_company_name"] = contact.get("company_name") or ""
                    patch["contact_email"] = contact.get("email") or ""
                    patch["contact_phone"] = contact.get("phone") or ""
                    # Billing address: prefer default billing address from addresses list, fall back to billing_address field
                    addresses = contact.get("addresses") or []
                    def _default_addr(addr_type: str) -> str:
                        default = next((a for a in addresses if a.get("address_type") == addr_type and a.get("is_default")), None)
                        if default:
                            return default.get("full_address") or default.get("address") or default.get("label") or ""
                        first = next((a for a in addresses if a.get("address_type") == addr_type), None)
                        if first:
                            return first.get("full_address") or first.get("address") or first.get("label") or ""
                        return contact.get(f"{addr_type}_address") or ""
                    def _default_attn(addr_type: str) -> str:
                        default = next((a for a in addresses if a.get("address_type") == addr_type and a.get("is_default")), None)
                        if default and default.get("attn"):
                            return default["attn"]
                        first = next((a for a in addresses if a.get("address_type") == addr_type), None)
                        return (first.get("attn") or "") if first else ""
                    patch["contact_billing_address"] = _default_addr("billing")
                    patch["contact_shipping_address"] = _default_addr("shipping")
                    patch["shipping_attn"] = _default_attn("shipping")
                    patch["contact_tax_id"] = contact.get("tax_id") or ""
                    contact_pt = contact.get("payment_terms")
                    if contact_pt:
                        patch["payment_terms"] = contact_pt
                        # Also recalculate due_date if issue_date is set
                        doc_pre = await api.get_doc(token, entity_id)
                        terms_list = await api.get_payment_terms(token)
                        new_due = _calculate_due_date(doc_pre.get("issue_date"), contact_pt, terms_list)
                        if new_due:
                            patch["due_date"] = new_due
                    # Auto-populate price_list from contact (fallback to company default)
                    contact_pl = contact.get("price_list")
                    if contact_pl:
                        patch["price_list"] = contact_pl
                    else:
                        try:
                            default_pl = await api.get_default_price_list(token)
                            patch["price_list"] = default_pl
                        except Exception:
                            pass
                except APIError:
                    pass  # contact fetch failure → skip auto-populate
            # Auto-calculate due_date when payment_terms changes
            elif field == "payment_terms" and value:
                try:
                    doc_pre = await api.get_doc(token, entity_id)
                    terms_list = await api.get_payment_terms(token)
                    new_due = _calculate_due_date(doc_pre.get("issue_date"), value, terms_list)
                    if new_due:
                        patch["due_date"] = new_due
                except APIError:
                    pass
            # Auto-populate terms_text when terms_template changes
            elif field == "terms_template" and value:
                try:
                    tc_templates = await api.get_terms_conditions(token)
                    tmpl = next((tc for tc in tc_templates if tc.get("name") == value), None)
                    if tmpl:
                        patch["terms_text"] = tmpl.get("text", "")
                except (APIError, Exception):
                    pass
            # Resolve commission contact name for display
            elif field == "commission_contact_id" and value:
                try:
                    contact = await api.get_contact(token, value)
                    name = contact.get("name") or contact.get("display_name")
                    if name:
                        patch["commission_contact_name"] = name
                except APIError:
                    pass
            await api.patch_doc(token, entity_id, patch)
            # Reprice line items when price_list changed
            new_pl = patch.get("price_list")
            if new_pl:
                try:
                    doc_pre = await api.get_doc(token, entity_id)
                    lines = doc_pre.get("line_items") or []
                    if lines:
                        updated = []
                        repriced = 0
                        for line in lines:
                            sku = (line.get("sku") or "").strip()
                            if sku:
                                try:
                                    resp = await api.list_items(token, {"sku": sku, "limit": 1})
                                    items = resp.get("items", []) if isinstance(resp, dict) else resp
                                    if items:
                                        new_price = resolve_price(items[0], new_pl)
                                        line = {**line, "unit_price": new_price, "price_list": new_pl}
                                        repriced += 1
                                except Exception:
                                    pass
                            updated.append(line)
                        if repriced:
                            await api.patch_doc(token, entity_id, {"line_items": updated})
                except Exception:
                    pass  # reprice failure is non-fatal
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        # Contact, price_list, terms_template, or currency changes affect multiple sections - full page refresh
        if field in ("contact_id", "price_list", "terms_template"):
            from starlette.responses import Response as _R
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        # Resolve contact fields to display names
        if field in ("contact_id", "commission_contact_id"):
            display_value = _resolve_contact_display(doc, field)
        else:
            display_value = doc.get(field)
        return _doc_display_cell(entity_id, field, display_value)

    @app.post("/docs/{entity_id}/field/{field}")
    async def doc_field_post(request: Request, entity_id: str, field: str):
        """Handle autosave of text fields (customer_note, internal_note) via hx_post."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401)
        form = await request.form()
        # Field name may come as the field name itself or as 'value'
        value = str(form.get(field, form.get("value", "")))
        try:
            await api.patch_doc(token, entity_id, {field: value})
        except APIError:
            pass  # silent autosave failure
        return _R("", status_code=204)

    @app.post("/docs/{entity_id}/notes")
    async def doc_add_note(request: Request, entity_id: str):
        """Add an internal note to a document."""
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401)
        form = await request.form()
        text = str(form.get("text", "")).strip()
        if text:
            try:
                await api.add_doc_note(token, entity_id, text)
            except APIError:
                pass
        # Re-fetch doc to render updated notes section
        try:
            doc = await api.get_doc(token, entity_id)
            tz: str = "UTC"
            try:
                _co = await api.get_company(token)
                tz = _co.get("timezone") or "UTC"
            except Exception:
                pass
            is_list = doc.get("doc_type") == "list"
            return _internal_notes_section(entity_id, doc, is_list, tz)
        except Exception:
            from starlette.responses import Response as _R
            return _R("", status_code=204)

    @app.post("/lists/{entity_id}/notes")
    async def list_add_note(request: Request, entity_id: str):
        """Add an internal note to a list."""
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401)
        form = await request.form()
        text = str(form.get("text", "")).strip()
        if text:
            try:
                await api.add_list_note(token, entity_id, text)
            except APIError:
                pass
        # Re-fetch list to render updated notes section
        try:
            lst = await api.get_list(token, entity_id)
            tz: str = "UTC"
            try:
                _co = await api.get_company(token)
                tz = _co.get("timezone") or "UTC"
            except Exception:
                pass
            return _internal_notes_section(entity_id, lst, True, tz)
        except Exception:
            from starlette.responses import Response as _R
            return _R("", status_code=204)

    # T2: Save line items
    @app.post("/docs/{entity_id}/lines")
    async def save_doc_lines(request: Request, entity_id: str):
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        lines = body.get("line_items", [])
        subtotal = body.get("subtotal", 0)
        tax = body.get("tax", 0)
        total = body.get("total", subtotal + tax)
        patch_data = {
            "line_items": lines,
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
        }
        try:
            await api.patch_doc(token, entity_id, patch_data)
        except APIError as e:
            return JSONResponse({"error": str(e.detail)}, status_code=400)
        return JSONResponse({"ok": True})

    # T2b: Reprice line items from a given price list
    @app.post("/docs/{entity_id}/reprice")
    async def reprice_doc_lines(request: Request, entity_id: str):
        """Re-resolve unit_price for all line items that came from inventory.

        Body: {"price_list": "Retail"}

        Only lines with a `sku` field (i.e. sourced from inventory) are repriced.
        Lines without a sku (manually entered) are left unchanged.
        """
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        price_list = (body.get("price_list") or "").strip()
        if not price_list:
            return JSONResponse({"error": "price_list is required"}, status_code=400)
        try:
            doc = await api.get_doc(token, entity_id)
        except APIError as e:
            return JSONResponse({"error": str(e.detail)}, status_code=400)
        existing_lines: list[dict] = doc.get("line_items") or []
        if not existing_lines:
            return JSONResponse({"ok": True, "repriced": 0})
        repriced = 0
        updated_lines = []
        for line in existing_lines:
            sku = (line.get("sku") or "").strip()
            if sku:
                # Look up current item price via catalog endpoint (SKU lookup)
                try:
                    resp = await api.list_items(token, {"sku": sku, "limit": 1})
                    items = resp.get("items", []) if isinstance(resp, dict) else resp
                    if items:
                        new_price = resolve_price(items[0], price_list)
                        line = {**line, "unit_price": new_price, "price_list": price_list}
                        repriced += 1
                except APIError:
                    pass  # leave line unchanged on lookup failure
            updated_lines.append(line)
        # Recalculate totals
        subtotal = sum(
            float(l.get("unit_price", 0)) * float(l.get("quantity", 0))
            for l in updated_lines
        )
        tax_rate = float(doc.get("tax_rate", 0) or 0)
        tax = round(subtotal * tax_rate / 100, 2)
        total = round(subtotal + tax, 2)
        try:
            await api.patch_doc(token, entity_id, {
                "line_items": updated_lines,
                "price_list": price_list,
                "subtotal": round(subtotal, 2),
                "tax": tax,
                "total": total,
            })
        except APIError as e:
            return JSONResponse({"error": str(e.detail)}, status_code=400)
        return JSONResponse({"ok": True, "repriced": repriced, "price_list": price_list})

    # T3: Document actions (finalize, void, send, mark_sent, unmark_sent)
    @app.post("/docs/{entity_id}/action/{action}")
    async def doc_action(request: Request, entity_id: str, action: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            if action == "finalize":
                await api.finalize_doc(token, entity_id)
            elif action == "send":
                sent_to = str(form.get("sent_to", "")).strip()
                if not sent_to:
                    # No recipient - redirect to cloud relay settings
                    return _R("", status_code=204, headers={"HX-Redirect": "/settings/general?tab=cloud-relay"})
                data = {"sent_to": sent_to, "sent_via": "email"}
                await api.send_doc(token, entity_id, data=data)
            elif action == "mark_sent":
                await api.send_doc(token, entity_id, data={"sent_via": "manual"})
            elif action == "unmark_sent":
                # Revert to draft status
                await api.patch_doc(token, entity_id, {"status": "draft"})
            elif action == "void":
                reason = str(form.get("reason", "")).strip() or None
                await api.void_doc(token, entity_id, reason)
            elif action == "revert_to_draft":
                reason = str(form.get("reason", "")).strip() or None
                await api.revert_doc_to_draft(token, entity_id, reason)
            elif action == "unvoid":
                await api.unvoid_doc(token, entity_id)
            elif action == "delete":
                await api.delete_doc(token, entity_id)
                doc_type = str(form.get("doc_type", "")).strip() or "invoice"
                return _R("", status_code=204, headers={"HX-Redirect": f"/docs?type={doc_type}"})
            else:
                return _R("", status_code=400)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            # Return error inline
            return Div(
                Span(str(e.detail), cls="flash flash--error"),
                hx_swap_oob="true", id="action-error",
            )
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    # T4: Record payment
    @app.post("/docs/{entity_id}/payment")
    async def record_doc_payment(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            amount_str = str(form.get("amount", "0"))
            try:
                amount = float(amount_str)
            except ValueError:
                amount = 0.0
            payment_date = str(form.get("payment_date", "")).strip() or None
            method = str(form.get("method", "")).strip() or None
            reference = str(form.get("reference", "")).strip() or None
            bank_account = str(form.get("bank_account", "")).strip() or None
            await api.record_payment(token, entity_id, {
                "amount": amount,
                "method": method,
                "reference": reference,
                "payment_date": payment_date,
                "bank_account": bank_account,
            })
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(
                Span(str(e.detail), cls="flash flash--error"),
                id="payment-error",
            )
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    # T1: Convert quotation to invoice
    @app.post("/docs/{entity_id}/convert")
    async def convert_doc_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            result = await api.convert_doc(token, entity_id)
            target_id = result.get("target_doc_id", entity_id)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{target_id}"})

    # T2: Receive PO goods
    @app.post("/docs/{entity_id}/receive")
    async def receive_po_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            location_id = str(form.get("location_id", "") or form.get("location_name", "")).strip()
            notes = str(form.get("notes", "")).strip() or None
            received_items = []
            idx = 0
            while f"item_id_{idx}" in form or f"sku_{idx}" in form:
                item_id = str(form.get(f"item_id_{idx}", "")).strip() or None
                sku = str(form.get(f"sku_{idx}", "")).strip() or None
                name = str(form.get(f"name_{idx}", "")).strip() or None
                try:
                    qty = float(str(form.get(f"qty_{idx}", "0")))
                except ValueError:
                    qty = 0.0
                if qty > 0:
                    item = {"po_line_index": idx, "quantity_received": qty}
                    if item_id:
                        item["item_id"] = item_id
                    if sku:
                        item["sku"] = sku
                    if name:
                        item["name"] = name
                    received_items.append(item)
                idx += 1
            data = {"location_id": location_id, "received_items": received_items}
            if notes:
                data["notes"] = notes
            await api.receive_po(token, entity_id, data)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    # T7: Refund payment
    @app.post("/docs/{entity_id}/refund")
    async def refund_payment_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            try:
                amount = float(str(form.get("amount", "0")))
            except ValueError:
                amount = 0.0
            method = str(form.get("method", "")).strip() or None
            reference = str(form.get("reference", "")).strip() or None
            await api.refund_payment(token, entity_id, {
                "amount": amount,
                "method": method,
                "reference": reference,
            })
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="refund-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    # ---- Payment management routes ----

    @app.post("/docs/{entity_id}/void-payment")
    async def void_payment_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            payment_index = int(form.get("payment_index", -1))
            void_reason = str(form.get("void_reason", "")).strip()
            await api.void_payment(token, entity_id, payment_index, void_reason)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="payment-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    @app.post("/docs/{entity_id}/apply-credit")
    async def apply_credit_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            target_doc_id = str(form.get("target_doc_id", "")).strip()
            amount = float(form.get("amount", 0))
            date = str(form.get("date", "")).strip() or None
            await api.apply_credit_note(token, entity_id, target_doc_id, amount, date)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="payment-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    @app.post("/docs/{entity_id}/refund-credit")
    async def refund_credit_route(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            amount = float(form.get("amount", 0))
            date = str(form.get("date", "")).strip() or None
            method = str(form.get("method", "")).strip() or None
            bank_account = str(form.get("bank_account", "")).strip() or None
            reference = str(form.get("reference", "")).strip() or None
            await api.refund_credit_note(token, entity_id, amount, date, method, bank_account, reference)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="payment-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    @app.post("/docs/bulk-payment")
    async def bulk_payment_route(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            doc_ids = [v.strip() for v in form.getlist("doc_ids") if v.strip()]
            amount = float(form.get("amount", 0))
            payment_date = str(form.get("payment_date", "")).strip() or None
            method = str(form.get("method", "")).strip() or None
            bank_account = str(form.get("bank_account", "")).strip() or None
            reference = str(form.get("reference", "")).strip() or None
            await api.bulk_payment(token, doc_ids, amount, payment_date, method, bank_account, reference)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), id="bulk-payment-error")
        # Refresh the page
        doc_type = str(form.get("doc_type", "invoice")).strip()
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs?type={doc_type}"})

    @app.get("/docs/bulk-payment-panel")
    async def bulk_payment_panel(request: Request):
        """HTMX endpoint: render inline bulk payment panel for selected docs."""
        token = _token(request)
        if not token:
            return Div(P(t("error.unauthorized")), id="bulk-payment-panel")
        doc_ids_raw = request.query_params.get("doc_ids", "")
        doc_ids = [d.strip() for d in doc_ids_raw.split(",") if d.strip()]
        if not doc_ids:
            return Div(P(t("doc.no_documents_selected")), id="bulk-payment-panel")

        docs = []
        for did in doc_ids:
            try:
                docs.append(await api.get_doc(token, did))
            except APIError:
                pass
        if not docs:
            return Div(P(t("doc.could_not_load_selected_documents")), id="bulk-payment-panel")

        # Validate same contact
        contact_ids = set(d.get("contact_id") or "" for d in docs)
        contact_ids.discard("")
        if len(contact_ids) > 1:
            return Div(
                P(t("doc._all_selected_documents_must_be_from_the_same_cont"), cls="flash flash--error"),
                id="bulk-payment-panel",
            )

        # Fetch bank accounts for dropdown
        bank_accounts = []
        try:
            bank_accounts = (await api.get_bank_accounts(token)).get("accounts", [])
        except Exception:
            pass

        contact_name = docs[0].get("contact_name") or ""
        doc_type = docs[0].get("doc_type") or "invoice"
        currency = docs[0].get("currency") or "USD"

        # Filter to payable docs and sort by due date
        payable = [d for d in docs if d.get("status") not in ("draft", "void", "paid") and float(d.get("amount_outstanding") or d.get("outstanding_balance") or 0) > 0]
        payable.sort(key=lambda d: d.get("due_date") or d.get("issue_date") or "")
        skipped = len(docs) - len(payable)
        total_outstanding = sum(float(d.get("amount_outstanding") or d.get("outstanding_balance") or 0) for d in payable)

        alloc_rows = []
        for d in payable:
            eid = d.get("entity_id") or d.get("id", "")
            doc_num = d.get("doc_number") or d.get("ref_id") or eid
            due = d.get("due_date") or "--"
            outstanding = float(d.get("amount_outstanding") or d.get("outstanding_balance") or 0)
            alloc_rows.append(Tr(
                Td(doc_num),
                Td(str(due)[:10]),
                Td(fmt_money(outstanding, currency), cls="cell--number"),
                Td(Span("--", cls="alloc-amount"), cls="cell--number"),
                data_outstanding=str(outstanding),
                data_doc_id=eid,
            ))

        from datetime import date as _d
        today = _d.today().isoformat()
        _methods = [Option(t("doc.cash"), value="cash"), Option(t("doc.bank_transfer"), value="transfer"),
                    Option(t("doc.card"), value="card"), Option(t("doc.check"), value="check"), Option(t("doc.other"), value="other")]
        _bank_opts = [Option(f"{ba.get('account_code', '')} - {ba.get('name', '')}", value=ba.get("account_code", ""))
                      for ba in bank_accounts]
        if not _bank_opts:
            _bank_opts = [Option("1110 - Default", value="1110")]

        hidden_ids = [Input(type="hidden", name="doc_ids", value=d.get("entity_id") or d.get("id", "")) for d in payable]

        panel = Div(
            H3(f"Bulk Payment — {contact_name} ({len(payable)} document{'s' if len(payable) != 1 else ''})", cls="section-title"),
            P(f"{skipped} document(s) skipped (already paid or draft).", cls="text-muted") if skipped else "",
            P(f"Total Outstanding: {fmt_money(total_outstanding, currency)}", cls="total-label--final"),
            Table(
                Thead(Tr(Th(t("th.document")), Th(t("th.due_date")), Th(t("th.outstanding")), Th(t("th.allocation")))),
                Tbody(*alloc_rows),
                cls="data-table data-table--compact", id="bulk-alloc-table",
            ),
            Form(
                *hidden_ids,
                Input(type="hidden", name="doc_type", value=doc_type),
                Div(
                    Div(Label(t("label.amount"), cls="form-label"),
                        Input(type="number", name="amount", value=f"{total_outstanding:.2f}", step="0.01",
                              min="0", cls="form-input", id="bulk-pay-amount",
                              oninput="celerpUpdateBulkAlloc()"), cls="form-group"),
                    Div(Label(t("th.date"), cls="form-label"),
                        Input(type="date", name="payment_date", value=today, cls="form-input"), cls="form-group"),
                    Div(Label(t("label.method"), cls="form-label"),
                        Select(*_methods, name="method", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.bank_account"), cls="form-label"),
                        Select(*_bank_opts, name="bank_account", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.reference"), cls="form-label"),
                        Input(type="text", name="reference", cls="form-input"), cls="form-group"),
                    cls="form-row",
                ),
                Span("", id="bulk-payment-error"),
                Div(
                    Button(t("btn.save_payment"), type="submit", cls="btn btn--primary"),
                    Button(t("btn.cancel"), type="button", cls="btn btn--ghost",
                           onclick="document.getElementById('bulk-payment-panel').innerHTML=''"),
                    cls="form-actions",
                ),
                hx_post="/docs/bulk-payment", hx_swap="none", cls="form-card",
            ),
            Script(f"""
function celerpUpdateBulkAlloc() {{
    const amount = parseFloat(document.getElementById('bulk-pay-amount')?.value || 0);
    let remaining = amount;
    document.querySelectorAll('#bulk-alloc-table tbody tr').forEach(row => {{
        const outstanding = parseFloat(row.dataset.outstanding || 0);
        const alloc = Math.min(remaining, outstanding);
        remaining = Math.max(0, remaining - alloc);
        row.querySelector('.alloc-amount').textContent = alloc > 0 ? '{currency_symbol(currency)}' + alloc.toFixed(2) : '--';
    }});
}}
celerpUpdateBulkAlloc();
"""),
            id="bulk-payment-panel",
            cls="bulk-payment-panel",
        )
        return panel

    @app.get("/payments")
    async def payments_list_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        tab = request.query_params.get("tab", "all")
        q = request.query_params.get("q", "")
        method_filter = request.query_params.get("method", "")
        contact_filter = request.query_params.get("contact", "")
        date_from, date_to, preset = _parse_dates(request)

        try:
            company = await api.get_company(token)
        except Exception:
            company = {}
        currency = company.get("currency") or None

        # Fetch all docs and extract payments
        try:
            docs_resp = await api.list_docs(token, {"limit": 5000, "exclude_status": "draft"})
            all_docs = docs_resp.get("items", []) if isinstance(docs_resp, dict) else docs_resp
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            all_docs = []

        # Build flat payment list from all docs
        payments_list: list[dict] = []
        for d in all_docs:
            eid = d.get("entity_id") or d.get("id", "")
            doc_number = d.get("doc_number") or d.get("ref_id") or eid
            doc_type = d.get("doc_type", "")
            contact_name = d.get("contact_name") or d.get("contact_id") or ""
            contact_id = d.get("contact_id") or ""
            doc_currency = d.get("currency") or currency
            for p in (d.get("payments") or []):
                payments_list.append({
                    "doc_id": eid,
                    "doc_number": doc_number,
                    "doc_type": doc_type,
                    "contact_name": contact_name,
                    "contact_id": contact_id,
                    "date": p.get("payment_date") or p.get("recorded_at", "")[:10],
                    "method": p.get("method", ""),
                    "bank_account": p.get("bank_account", ""),
                    "reference": p.get("reference", ""),
                    "amount": float(p.get("amount", 0)),
                    "status": p.get("status", "active"),
                    "currency": doc_currency,
                    "index": p.get("index", 0),
                })

        # Apply filters
        if tab == "received":
            payments_list = [p for p in payments_list if p["doc_type"] in ("invoice", "credit_note")]
        elif tab == "sent":
            payments_list = [p for p in payments_list if p["doc_type"] in ("bill", "purchase_order")]
        elif tab == "voided":
            payments_list = [p for p in payments_list if p["status"] == "voided"]

        if q:
            q_lower = q.lower()
            payments_list = [p for p in payments_list if q_lower in p["doc_number"].lower() or q_lower in p["reference"].lower()]
        if method_filter:
            payments_list = [p for p in payments_list if p["method"] == method_filter]
        if contact_filter:
            c_lower = contact_filter.lower()
            payments_list = [p for p in payments_list if c_lower in p["contact_name"].lower()]
        if date_from:
            payments_list = [p for p in payments_list if p["date"] >= date_from]
        if date_to:
            payments_list = [p for p in payments_list if p["date"] <= date_to]

        # Sort newest first
        payments_list.sort(key=lambda p: p["date"], reverse=True)

        # Summary cards
        active_payments = [p for p in payments_list if p["status"] == "active"]
        total_received = sum(p["amount"] for p in active_payments if p["doc_type"] in ("invoice", "credit_note"))
        total_sent = sum(p["amount"] for p in active_payments if p["doc_type"] in ("bill", "purchase_order"))

        # Build tabs
        tab_cls = lambda t: f"category-tab {'category-tab--active' if tab == t else ''}"
        extra_params = f"&q={q}&method={method_filter}&contact={contact_filter}" if any([q, method_filter, contact_filter]) else ""
        tabs = Div(
            A(t("doc.all"), href=f"/payments?tab=all{extra_params}", cls=tab_cls("all")),
            A(t("doc.received"), href=f"/payments?tab=received{extra_params}", cls=tab_cls("received")),
            A(t("doc.sent"), href=f"/payments?tab=sent{extra_params}", cls=tab_cls("sent")),
            A(t("doc.voided"), href=f"/payments?tab=voided{extra_params}", cls=tab_cls("voided")),
            cls="category-tabs",
        )

        # Summary
        summary_bar = Div(
            Span(f"Received: {fmt_money(total_received, currency)}", cls="val-chip"),
            Span(f"Sent: {fmt_money(total_sent, currency)}", cls="val-chip"),
            Span(f"Net: {fmt_money(total_received - total_sent, currency)}", cls="val-chip val-chip--alert"),
            cls="valuation-bar",
        )

        # Filters
        _methods_opts = [Option(t("doc.all_methods"), value=""), Option(t("doc.cash"), value="cash"),
                         Option(t("btn.transfer"), value="transfer"), Option(t("doc.card"), value="card"),
                         Option(t("doc.check"), value="check"), Option(t("doc.credit_note"), value="credit_note"),
                         Option(t("doc.other"), value="other")]
        filter_bar = Div(
            Input(type="search", name="q", value=q, placeholder="Search doc# or reference...",
                  cls="form-input form-input--sm", style="max-width:200px;",
                  onchange=f"window.location='/payments?tab={tab}&q='+this.value+'&method={method_filter}&contact={contact_filter}'"),
            Input(type="text", name="contact", value=contact_filter, placeholder="Contact...",
                  cls="form-input form-input--sm", style="max-width:200px;",
                  onchange=f"window.location='/payments?tab={tab}&q={q}&method={method_filter}&contact='+this.value"),
            Select(*_methods_opts, name="method",
                   cls="form-input form-input--sm", style="max-width:150px;",
                   onchange=f"window.location='/payments?tab={tab}&q={q}&method='+this.value+'&contact={contact_filter}'"),
            cls="filter-bar",
            style="display:flex;gap:0.5rem;flex-wrap:wrap;margin-bottom:1rem;",
        )

        # Table
        def _pay_row(p: dict) -> FT:
            voided = p["status"] == "voided"
            row_cls = "data-row" + (" payment-voided" if voided else "")
            return Tr(
                Td(format_value(p["date"], "date")),
                Td(A(p["doc_number"], href=f"/docs/{p['doc_id']}", cls="table-link")),
                Td(p["contact_name"] or EMPTY),
                Td(format_value(p["method"], "badge")),
                Td(p["reference"] or EMPTY),
                Td(fmt_money(p["amount"], p.get("currency")), cls="cell--number"),
                Td(
                    Span(t("doc.voided"), cls="badge badge--void") if voided
                    else Span(t("th.active"), cls="badge badge--green"),
                ),
                cls=row_cls,
            )

        payment_table = Table(
            Thead(Tr(Th(t("th.date")), Th(t("th.document")), Th(t("page.contact_detail")), Th(t("label.method")), Th(t("label.reference")), Th(t("label.amount")), Th(t("th.status")))),
            Tbody(*[_pay_row(p) for p in payments_list]) if payments_list else Tbody(Tr(Td(t("doc.no_payments_found"), colspan="7", cls="empty-state-msg"))),
            cls="data-table", id="payments-table",
        )

        lang = get_lang(request)
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Payments", None)]),
            page_header("Payments"),
            tabs,
            _date_filter_bar("/payments", date_from, date_to, preset, extra_params=f"&tab={tab}{extra_params}", lang=lang),
            summary_bar,
            filter_bar,
            payment_table,
            title="Payments - Celerp",
            nav_active="payments",
            lang=lang,
            request=request,
        )

    @app.get("/docs/{entity_id}/open-invoices")
    async def doc_open_invoices(request: Request, entity_id: str):
        """HTMX endpoint: fetch open invoices for same contact (for credit note application picker)."""
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse([], status_code=401)
        try:
            doc = await api.get_doc(token, entity_id)
            contact_id = doc.get("contact_id")
            if not contact_id:
                return JSONResponse([])
            resp = await api.list_docs(token, {"contact_id": contact_id, "doc_type": "invoice", "limit": 100})
            invoices = resp.get("items", [])
            # Filter to open invoices with outstanding > 0
            open_inv = []
            for inv in invoices:
                outstanding = float(inv.get("amount_outstanding") or inv.get("outstanding_balance") or 0)
                if inv.get("status") not in ("draft", "void", "paid") and outstanding > 0:
                    open_inv.append({
                        "id": inv.get("entity_id") or inv.get("id", ""),
                        "doc_number": inv.get("doc_number") or inv.get("ref_id") or "",
                        "outstanding": outstanding,
                        "contact_name": inv.get("contact_name") or "",
                    })
            return JSONResponse(open_inv)
        except Exception:
            return JSONResponse([])

    @app.post("/docs/{entity_id}/share")
    async def create_share_link_route(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            result = await api.create_share_link(token, entity_id)
            share_url = result.get("url") or result.get("token", "")
            return Span(
                Input(type="text", value=share_url, readonly=True,
                      cls="form-input form-input--inline share-url-input",
                      onclick="this.select()"),
                " ",
                A(t("doc.open"), href=share_url, target="_blank", cls="btn btn--secondary btn--xs"),
                cls="share-result",
            )
        except APIError as e:
            return Span(str(e.detail), cls="flash flash--error")

    # -----------------------------------------------------------------------
    # Fulfillment toggle routes
    # -----------------------------------------------------------------------

    @app.post("/docs/{entity_id}/fulfill")
    async def doc_fulfill(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.fulfill_doc(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(
                Span(str(e.detail), cls="flash flash--error"),
                hx_swap_oob="true", id="action-error",
            )
        # Re-fetch doc and return updated toggle
        try:
            doc = await api.get_doc(token, entity_id)
        except Exception:
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        from celerp_fulfillment.ui import render_fulfill_toggle
        el = render_fulfill_toggle(doc)
        if el is None:
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        return el

    @app.post("/docs/{entity_id}/unfulfill")
    async def doc_unfulfill(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.unfulfill_doc(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(
                Span(str(e.detail), cls="flash flash--error"),
                hx_swap_oob="true", id="action-error",
            )
        # Re-fetch doc and return updated toggle
        try:
            doc = await api.get_doc(token, entity_id)
        except Exception:
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        from celerp_fulfillment.ui import render_fulfill_toggle
        el = render_fulfill_toggle(doc)
        if el is None:
            return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})
        return el

    # -----------------------------------------------------------------------
    # List routes — folded here so lists.py is a thin shim
    # -----------------------------------------------------------------------

    @app.get("/lists")
    async def lists_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        list_type = request.query_params.get("type", "")
        status = request.query_params.get("status", "")
        view = request.query_params.get("view", "")
        page = int(request.query_params.get("page", 1))
        is_drafts_view = view == "drafts" or status == "draft"
        effective_status = "draft" if is_drafts_view else ("exclude_draft" if not status else status)
        try:
            params: dict = {"limit": _PER_PAGE, "offset": (page - 1) * _PER_PAGE}
            if q:
                params["q"] = q
            if list_type:
                params["list_type"] = list_type
            if effective_status == "exclude_draft":
                params["exclude_status"] = "draft"
            elif effective_status:
                params["status"] = effective_status
            result = await api.list_lists(token, params)
            lists = result.get("items", [])
            filtered_total = result.get("total", len(lists))
            draft_count = (await api.list_lists(token, {"status": "draft", "limit": 1})).get("total", 0)
            summary = await api.get_list_summary(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            lists, summary, draft_count, filtered_total = [], {}, 0, 0
        lang = get_lang(request)
        return base_shell(
            page_header(
                t("page.lists", lang),
                _list_drafts_tab(draft_count, is_drafts_view, list_type),
                search_bar(placeholder="Search ref, customer...", target="#list-table", url="/lists/search"),
                Button(t("page.new_list"), hx_post="/lists/create-blank", hx_swap="none", cls="btn btn--primary", title="Create blank draft"),
                A(t("btn.export_csv"), href="/lists/export/csv", cls="btn btn--secondary"),
                A(t("doc.import_csv"), href="/lists/import", cls="btn btn--secondary"),
            ),
            _list_type_tabs(list_type),
            _list_status_cards(summary, status),
            _list_table(lists, lang=lang),
            pagination(page, filtered_total, _PER_PAGE, "/lists",
                       f"q={q}&type={list_type}&status={status}&view={view}".strip("&")),
            title="Lists - Celerp",
            nav_active="lists",
            request=request,
        )

    @app.get("/lists/new")
    async def lists_new_redirect(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            result = await api.create_list(token, {"list_type": "quotation", "status": "draft"})
            return RedirectResponse(f"/lists/{result.get('entity_id') or result.get('id', '')}", status_code=302)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse("/lists", status_code=302)

    @app.post("/lists/new")
    async def lists_new_post_redirect(request: Request):
        return RedirectResponse("/lists", status_code=302)

    @app.get("/lists/search")
    async def lists_search(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        list_type = request.query_params.get("type", "")
        status = request.query_params.get("status", "")
        try:
            params: dict = {"limit": _PER_PAGE}
            if q:
                params["q"] = q
            if list_type:
                params["list_type"] = list_type
            if status:
                params["status"] = status
            lists = (await api.list_lists(token, params)).get("items", [])
        except APIError as e:
            logger.warning("API error on lists_search: %s", e.detail)
            lists = []
        return _list_table(lists, lang=get_lang(request))

    @app.get("/lists/export/csv")
    async def lists_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            data = await api.export_lists_csv(token)
        except APIError as e:
            logger.warning("API error on lists_export_csv: %s", e.detail)
            data = b"error\n"
        from starlette.responses import Response
        return Response(content=data, media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=lists.csv"})

    @app.post("/lists/create-blank")
    async def create_blank_list(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            result = await api.create_list(token, {"list_type": "quotation", "status": "draft"})
            entity_id = result.get("entity_id") or result.get("id", "")
        except APIError as e:
            logger.warning("API error on create_blank_list: %s", e.detail)
            return _R("", status_code=500)
        return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{entity_id}"})

    @app.post("/lists/from-items")
    async def list_from_items_modal(request: Request):
        """Modal: choose to create new draft list or add to existing."""
        token = _token(request)
        if not token:
            from starlette.responses import Response as _R
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        try:
            drafts_resp = await api.list_lists(token, {"status": "draft", "limit": 20})
            drafts = drafts_resp.get("items", [])
        except APIError:
            drafts = []
        hidden_items = [Input(type="hidden", name="selected", value=eid) for eid in entity_ids]
        return _send_to_modal("List", "/lists/from-items/new", "/lists/from-items/add",
                              "/lists/from-items/search", drafts, hidden_items, "list")

    @app.post("/lists/from-items/new")
    async def create_list_from_items(request: Request):
        """Create a draft list pre-populated with line items from selected inventory items."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="modal-container")
        line_items = await _line_items_from_inventory(token, entity_ids)
        try:
            result = await api.create_list(token, {
                "list_type": "quotation",
                "status": "draft",
                "line_items": line_items,
            })
            list_id = result.get("entity_id") or result.get("id", "")
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="modal-container")
        return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{list_id}"})

    @app.post("/lists/from-items/add")
    async def add_items_to_list(request: Request):
        """Append line items from selected inventory to an existing list."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        target_id = str(form.get("target_id", "")).strip()
        if not entity_ids or not target_id:
            return Div(P(t("label.no_items_or_target_selected"), cls="flash flash--warning"), id="modal-container")
        new_lines = await _line_items_from_inventory(token, entity_ids)
        try:
            lst = await api.get_list(token, target_id)
            existing_lines = lst.get("line_items") or []
            combined = existing_lines + new_lines
            subtotal = sum(l.get("quantity", 0) * l.get("unit_price", 0) for l in combined)
            await api.patch_list(token, target_id, {
                "line_items": combined,
                "subtotal": subtotal,
                "total": subtotal,
            })
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="modal-container")
        return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{target_id}"})

    @app.get("/lists/from-items/search")
    async def list_from_items_search(request: Request):
        """HTMX search endpoint for the list picker dropdown."""
        token = _token(request)
        if not token:
            return Div()
        q = request.query_params.get("q", "").strip()
        try:
            resp = await api.list_lists(token, {"q": q, "limit": 20} if q else {"status": "draft", "limit": 20})
            items = resp.get("items", [])
        except APIError:
            items = []
        return _send_to_option_list(items, "list")
    @app.get("/lists/{entity_id}")
    async def list_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            lst = await api.get_list(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if e.status == 404:
                from starlette.responses import HTMLResponse as _HR
                return _HR("<h2>List not found</h2><p><a href='/lists'>Back to Lists</a></p>", status_code=404)
            lst = {}

        # Inject doc_type so _doc_detail() treats it as a list
        lst.setdefault("doc_type", "list")

        # Map list "receiver"/"customer_name" → standard contact fields
        if not lst.get("contact_name"):
            lst["contact_name"] = lst.get("receiver") or lst.get("customer_name") or lst.get("customer_id") or ""
        if not lst.get("issue_date"):
            lst["issue_date"] = lst.get("created_at") or lst.get("date")

        # Inject company fields
        if not lst.get("company_name"):
            try:
                company = await api.get_company(token)
                lst.update({
                    "company_name": company.get("name") or "",
                    "company_address": company.get("address") or company.get("settings", {}).get("address") or "",
                    "company_phone": company.get("phone") or company.get("settings", {}).get("phone") or "",
                    "company_tax_id": company.get("tax_id") or company.get("settings", {}).get("tax_id") or "",
                    "company_email": company.get("email") or company.get("settings", {}).get("email") or "",
                })
            except Exception:
                pass

        # Fetch price lists
        price_lists: list[dict] = []
        try:
            price_lists = await api.get_price_lists(token)
        except Exception:
            pass

        # Fetch company timezone for notes display
        tz: str = "UTC"
        try:
            _co = await api.get_company(token)
            tz = _co.get("timezone") or "UTC"
        except Exception:
            pass
        company_taxes: list[dict] = []
        try:
            company_taxes = await api.get_taxes(token)
        except Exception:
            pass

        ref = lst.get("ref_id") or entity_id
        status = lst.get("status", "draft")
        status_label = status.replace("_", " ").title()
        list_type_label = (lst.get("list_type") or "List").replace("_", " ").title()
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Lists", "/lists"), (f"{status_label} {ref}", None)]),
            page_header(f"{list_type_label} - {status_label} {ref}"),
            _doc_detail(lst, price_lists=price_lists, tz=tz, company_taxes=company_taxes, role=_get_role(request)),
            title=f"List {ref} - Celerp",
            nav_active="lists",
            request=request,
        )

    @app.get("/lists/{entity_id}/field/{field}/edit")
    async def list_field_edit(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            lst = await api.get_list(token, entity_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        value = str(lst.get(field, "") or "")
        restore_url = f"/lists/{entity_id}/field/{field}/display"
        patch_url = f"/lists/{entity_id}/field/{field}"
        esc_js = (
            f"if(event.key==='Escape'){{"
            f"htmx.ajax('GET','{restore_url}',{{target:this.closest('.editable-cell'),swap:'outerHTML'}});"
            f"event.preventDefault();}}"
        )
        enter_js = "if(event.key==='Enter'){event.preventDefault();this.blur();}"
        if field == "list_type":
            input_el = Select(
                *[Option(lt.replace("_", " ").title(), value=lt, selected=(lt == value)) for lt in _LIST_TYPES],
                name="value",
                hx_patch=patch_url, hx_target="closest .editable-cell", hx_swap="outerHTML", hx_trigger="change",
                cls="cell-input cell-input--select", autofocus=True,
                onkeydown=esc_js,
            )
        elif field in _LIST_DATE_FIELDS or field in ("issue_date",):
            input_el = Input(
                type="date", name="value", value=value[:10] if value else "",
                hx_patch=patch_url, hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js + enter_js,
            )
        elif field == "status":
            _list_statuses = ["draft", "sent", "accepted", "completed", "void", "converted"]
            input_el = Select(
                *[Option(s.replace("_", " ").title(), value=s, selected=(s == value)) for s in _list_statuses],
                name="value",
                hx_patch=patch_url, hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="change", cls="cell-input cell-input--select", autofocus=True,
                onkeydown=esc_js,
            )
        else:
            input_el = Input(
                type="text", name="value", value=value,
                hx_patch=patch_url, hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js + enter_js,
            )
        return Div(input_el, cls="editable-cell editable-cell--editing")

    @app.get("/lists/{entity_id}/field/{field}/display")
    async def list_field_display(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            lst = await api.get_list(token, entity_id)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        return _doc_display_cell(entity_id, field, lst.get(field), "list")

    @app.patch("/lists/{entity_id}/field/{field}")
    async def list_field_patch(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))
        try:
            await api.patch_list(token, entity_id, {field: value})
            lst = await api.get_list(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _doc_display_cell(entity_id, field, lst.get(field), "list")

    @app.post("/lists/{entity_id}/lines")
    async def save_list_lines(request: Request, entity_id: str):
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        patch_data = {
            "line_items": body.get("line_items", []),
            "subtotal": body.get("subtotal", 0),
            "tax": body.get("tax", 0),
            "total": body.get("total", 0),
        }
        try:
            await api.patch_list(token, entity_id, patch_data)
        except APIError as e:
            return JSONResponse({"error": str(e.detail)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.post("/lists/{entity_id}/action/{action}")
    async def list_action(request: Request, entity_id: str, action: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            form = await request.form()
            if action == "send":
                await api.send_list(token, entity_id)
            elif action == "accept":
                await api.accept_list(token, entity_id)
            elif action == "complete":
                await api.complete_list(token, entity_id)
            elif action == "void":
                reason = str(form.get("reason", "")).strip() or None
                await api.void_list(token, entity_id, reason)
            elif action == "delete":
                await api.delete_list(token, entity_id)
                list_type = str(form.get("list_type", "")).strip() or "quotation"
                return _R("", status_code=204, headers={"HX-Redirect": f"/lists?type={list_type}"})
            elif action == "duplicate":
                result = await api.duplicate_list(token, entity_id)
                return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{result.get('id') or result.get('entity_id')}"})
            elif action == "convert-invoice":
                result = await api.convert_list(token, entity_id, "invoice")
                return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{result['target_doc_id']}"})
            elif action == "convert-memo":
                result = await api.convert_list(token, entity_id, "memo")
                return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{result['target_doc_id']}"})
            else:
                return _R("", status_code=400)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(e.detail), cls="flash flash--error"), hx_swap_oob="true", id="action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{entity_id}"})


def _doc_table(
    docs: list[dict],
    sort: str = "date",
    sort_dir: str = "desc",
    base_params: dict[str, str] | None = None,
    doc_type: str = "",
    lang: str = "en",
) -> FT:
    # Per-doc-type empty-state labels: (no_docs_key, create_btn_key)
    _EMPTY_STATE_KEYS: dict[str, tuple[str, str]] = {
        "invoice": ("label.no_documents_yet", "btn.new_invoice"),
        "memo": ("label.no_memos_yet", "btn.new_memo"),
        "bill": ("label.no_bills_yet", "btn.new_bill"),
        "purchase_order": ("label.no_purchase_orders_yet", "btn.new_purchase_order"),
        "consignment_in": ("label.no_consignment_in_yet", "btn.new_consignment_in"),
        "receipt": ("label.no_receipts_yet", "btn.new_receipt"),
        "credit_note": ("label.no_credit_notes_yet", "btn.new_credit_note"),
        "list": ("label.no_lists_yet", "btn.new_list"),
    }
    if not docs:
        dt_slug = doc_type if doc_type else "invoice"
        empty_keys = _EMPTY_STATE_KEYS.get(dt_slug, ("label.no_documents_yet", "btn.new_document"))
        return Div(
            empty_state_cta(t(empty_keys[0], lang), t(empty_keys[1], lang), f"/docs/create-blank?type={dt_slug}", hx_post=True),
            id="doc-table",
        )

    # Checkboxes for invoice/bill types (not lists, not quotations, not memos)
    show_checkboxes = doc_type in ("invoice", "bill")

    sort_keys = {
        "number": lambda d: str(d.get("doc_number") or d.get("ref") or ""),
        "type": lambda d: str(d.get("doc_type") or ""),
        "contact": lambda d: str(d.get("contact_name") or d.get("contact_id") or ""),
        "date": lambda d: str(d.get("issue_date") or d.get("created_at") or ""),
        "due": lambda d: str(d.get("due_date") or d.get("payment_due_date") or ""),
        "total": lambda d: float(d.get("total_amount") if d.get("total_amount") is not None else (d.get("total") or 0) or 0),
        "outstanding": lambda d: float(d.get("outstanding_balance") if d.get("outstanding_balance") is not None else (d.get("amount_outstanding") or 0) or 0),
        "status": lambda d: str(d.get("status") or ""),
    }
    key_fn = sort_keys.get(sort, sort_keys["date"])
    docs = sorted(docs, key=key_fn, reverse=(sort_dir == "desc"))

    def _th(label: str, key: str) -> FT:
        next_dir = "asc" if (sort == key and sort_dir == "desc") else "desc"
        marker = " ▲" if (sort == key and sort_dir == "asc") else (" ▼" if sort == key else "")

        params = dict(base_params or {})
        params["sort"] = key
        params["dir"] = next_dir
        href = f"/docs?{urlencode({k: v for k, v in params.items() if v not in (None, '', [])})}"
        return Th(A(f"{label}{marker}", href=href, cls="sort-link"))

    def _row(d: dict) -> FT:
        eid = d.get("entity_id") or d.get("id", "")
        doc_number = d.get("doc_number") or d.get("ref") or d.get("ref_id") or eid
        contact = d.get("contact_name") or d.get("contact_id") or d.get("contact_external_id")
        issue_date = d.get("issue_date") or d.get("created_at")
        due_date = d.get("due_date") or d.get("payment_due_date")
        total_amount = d.get("total_amount") if d.get("total_amount") is not None else d.get("total")
        outstanding_amount = d.get("outstanding_balance") if d.get("outstanding_balance") is not None else d.get("amount_outstanding")
        outstanding = float(outstanding_amount or 0)
        checkbox_td = [Td(Input(type="checkbox", cls="doc-row-select", value=eid,
                     data_contact_id=d.get("contact_id") or "",
                     data_outstanding=str(outstanding)),
                     cls="col-checkbox")] if show_checkboxes else []
        return Tr(
            *checkbox_td,
            Td(A(doc_number or EMPTY, href=f"/docs/{eid}", cls="table-link")),
            Td(format_value(d.get("doc_type"), "badge")),
            Td(format_value(contact)),
            Td(format_value(issue_date, "date")),
            Td(format_value(due_date, "date")),
            Td(format_value(total_amount, "money"), cls="cell--number"),
            Td(
                format_value(outstanding_amount, "money"),
                cls=f"cell--number {'cell--alert' if outstanding > 0 and d.get('doc_type') == 'invoice' else ''}",
            ),
            Td(format_value(d.get("status"), "badge")),
            id=f"doc-{eid}",
            cls="data-row",
        )

    checkbox_th = [Th(Input(type="checkbox", id="doc-select-all", title="Select all"), cls="col-checkbox")] if show_checkboxes else []

    # Bulk payment action bar (hidden by default, shown when checkboxes selected)
    bulk_bar = ""
    if show_checkboxes:
        bulk_bar = Div(
            Span(t("doc.0_selected"), id="doc-bulk-count", cls="bulk-count"),
            Button(t("btn.record_payment"), type="button", id="doc-bulk-pay-btn", cls="btn btn--primary btn--sm",
                   style="display:none;",
                   hx_get="/docs/bulk-payment-panel", hx_target="#bulk-payment-panel", hx_swap="innerHTML",
                   hx_include="this"),
            Div(id="bulk-payment-panel"),
            cls="bulk-action-bar", id="doc-bulk-bar",
        )

    bulk_js = ""
    if show_checkboxes:
        bulk_js = Script(f"""
(function() {{
    var table = document.getElementById('doc-table');
    if (!table) return;
    var selectAll = document.getElementById('doc-select-all');
    var countEl = document.getElementById('doc-bulk-count');
    var payBtn = document.getElementById('doc-bulk-pay-btn');
    function getSelected() {{
        return Array.from(table.querySelectorAll('.doc-row-select:checked'));
    }}
    function updateBar() {{
        var sel = getSelected();
        var n = sel.length;
        if (countEl) countEl.textContent = n + ' selected';
        if (payBtn) {{
            payBtn.style.display = n > 0 ? '' : 'none';
            // Build doc_ids param for HTMX request
            var ids = sel.map(cb => cb.value).join(',');
            payBtn.setAttribute('hx-vals', JSON.stringify({{doc_ids: ids}}));
            htmx.process(payBtn);
        }}
    }}
    if (selectAll) {{
        selectAll.addEventListener('change', function() {{
            table.querySelectorAll('.doc-row-select').forEach(cb => cb.checked = selectAll.checked);
            updateBar();
        }});
    }}
    table.addEventListener('change', function(e) {{
        if (e.target && e.target.classList.contains('doc-row-select')) updateBar();
    }});
}})();
""")

    return Div(
        bulk_bar,
        Table(
            Thead(Tr(
                *checkbox_th,
                _th("Number", "number"), _th("Type", "type"), _th("Contact", "contact"), _th("Date", "date"), _th("Due", "due"),
                _th("Total", "total"), _th("Outstanding", "outstanding"), _th("Status", "status"),
            )),
            Tbody(*[_row(d) for d in docs]),
            cls="data-table",
        ),
        bulk_js,
        id="doc-table",
    )


def _resolve_contact_display(doc: dict, field: str) -> str:
    """Resolve a contact field to its display name. DRY helper for all contact display contexts."""
    _NAME_MAP = {
        "contact_id": "contact_name",
        "commission_contact_id": "commission_contact_name",
    }
    name_field = _NAME_MAP.get(field)
    if name_field:
        name = doc.get(name_field)
        if name:
            return name
    raw = doc.get(field) or ""
    # If the raw value looks like an entity_id (contact:uuid), return "--" to signal unresolved
    if raw.startswith("contact:"):
        return "--"
    return raw


def _doc_display_cell(entity_id: str, field: str, value, doc_type: str = "") -> FT:
    _prefix = "/lists" if doc_type == "list" else "/docs"
    return Div(
        format_value(value, "badge" if field in {"status", "purchase_kind"} else ("money" if field in {"total_amount", "tax_amount", "outstanding_balance"} else "date" if field in {"issue_date", "due_date"} else "text")),
        hx_get=f"{_prefix}/{entity_id}/field/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        title="Click to edit",
        cls="editable-cell",
    )


def _tc_dropdown(entity_id: str, doc: dict, tc_templates: list[dict], doc_type: str, is_draft: bool) -> list:
    """Build T&C template dropdown + terms_text for a doc detail page."""
    # Filter templates by doc_type
    relevant = [tc for tc in tc_templates if doc_type in (tc.get("doc_types") or [])]
    current = doc.get("terms_template") or ""
    options = []
    for item in relevant:
        options.append(Option(item["name"], value=item["name"], selected=(item["name"] == current)))
    options.append(Option(t("label._add_new"), value="__add_new__"))
    settings_url = "/settings/sales?tab=terms-conditions" if doc_type not in ("purchase_order", "bill", "consignment_in") else "/settings/purchasing?tab=terms-conditions"
    # JS: if user picks __add_new__, navigate to settings
    select_js = f"if(this.value==='__add_new__'){{window.location.href='{settings_url}';return false;}}"
    return [
        Div(
            Div(t("doc.template"), cls="form-label"),
            Select(
                *options,
                name="value",
                hx_patch=f"/docs/{entity_id}/field/terms_template",
                hx_target="closest .doc-section",
                hx_swap="outerHTML",
                hx_trigger="change",
                cls="form-select",
                onchange=select_js,
            ) if is_draft else P(current or "--", cls="meta-value"),
            cls="form-group",
        ),
    ]


def _payment_section(doc: dict, bank_accounts: list[dict] | None = None) -> FT:
    """Shared payment/credit section for invoices, bills, and credit notes.

    DRY: one function, different labels based on doc_type.
    Hidden for drafts, void docs, and non-payable doc types.
    """
    entity_id = doc.get("entity_id") or doc.get("id") or ""
    doc_type = doc.get("doc_type", "")
    status = doc.get("status", "draft")
    currency = doc.get("currency") or "USD"

    # Determine if this doc type should show payments
    _PAYABLE_TYPES = ("invoice", "bill", "credit_note")
    if doc_type not in _PAYABLE_TYPES:
        return Span()
    if status in ("draft", "void"):
        return Span()

    is_credit_note = doc_type == "credit_note"
    is_bill = doc_type == "bill"

    # Labels
    section_title = "Credit Application" if is_credit_note else "Payments"
    section_icon = "\U0001f4b3" if is_credit_note else "\U0001f4b0"
    add_label = "Apply to Invoice" if is_credit_note else ("Record Payment" if is_bill else "Receive Payment")

    payments = doc.get("payments") or []
    total_val = float(doc.get("total") or doc.get("total_amount") or 0)
    amount_paid = float(doc.get("amount_paid") or 0)
    outstanding = float(doc.get("amount_outstanding") or doc.get("outstanding_balance") or 0)

    from datetime import date as _d
    today = _d.today().isoformat()

    # --- Payment history table ---
    history_rows = []
    for i, p in enumerate(payments):
        voided = p.get("status") == "voided"
        p_date = p.get("payment_date") or p.get("recorded_at", "")[:10]
        p_method = p.get("method", "")
        p_bank = p.get("bank_account", "")
        p_ref = p.get("reference", "")
        p_amount = float(p.get("amount", 0))
        p_source = p.get("source_doc_id") or ""
        p_target = p.get("target_doc_id") or ""

        # For credit notes, show "Applied To" instead of bank
        if is_credit_note and p_target:
            link_col = Td(A(p_target.split(":")[-1][:12], href=f"/docs/{p_target}", cls="table-link"))
        elif p_method == "credit_note" and p_source:
            link_col = Td(A(f"CN {p_source.split(':')[-1][:12]}", href=f"/docs/{p_source}", cls="table-link"))
        else:
            link_col = Td(p_bank or EMPTY)

        row_cls = "data-row" + (" payment-voided" if voided else "")
        void_cell = Td("")
        if voided:
            void_reason = p.get("void_reason") or ""
            void_cell = Td(Span(t("doc.voided"), cls="badge badge--void", title=void_reason))
        elif not voided and _is_manager:
            void_cell = Td(
                Details(
                    Summary("🗑", cls="btn btn--ghost btn--xs", title="Void this payment"),
                    Form(
                        Input(type="hidden", name="payment_index", value=str(i)),
                        Input(type="text", name="void_reason", placeholder="Reason...", cls="form-input form-input--sm"),
                        Button(t("btn.confirm_void"), type="submit", cls="btn btn--danger btn--xs"),
                        hx_post=f"/docs/{entity_id}/void-payment", hx_swap="none",
                        cls="inline-form inline-form--compact",
                    ),
                    cls="void-inline",
                ),
            )
        else:
            void_cell = Td()

        history_rows.append(Tr(
            Td(format_value(p_date, "date")),
            Td(format_value(p_method, "badge")),
            link_col,
            Td(p_ref or EMPTY),
            Td(fmt_money(p_amount, currency), cls="cell--number"),
            void_cell,
            cls=row_cls,
        ))

    history_table = ""
    if history_rows:
        _hist_header = "Applied To" if is_credit_note else "Bank"
        history_table = Table(
            Thead(Tr(Th(t("th.date")), Th(t("label.method")), Th(_hist_header), Th(t("label.reference")), Th(t("label.amount")), Th(""))),
            Tbody(*history_rows),
            cls="data-table data-table--compact",
        )

    # --- Summary line ---
    if is_credit_note:
        summary_line = Div(
            Span(f"Credit Total: {fmt_money(total_val, currency)}", cls="total-label"),
            Span(f"Applied: {fmt_money(amount_paid, currency)}", cls="total-label"),
            Span(f"Remaining: {fmt_money(outstanding, currency)}",
                 cls="total-value" + (" total-value--alert" if outstanding > 0 else " total-value--success")),
            cls="payment-summary",
        )
    else:
        paid_label = f"Total Paid: {fmt_money(amount_paid, currency)} / {fmt_money(total_val, currency)}"
        outstanding_label = "Paid in Full" if outstanding <= 0.005 else f"Outstanding: {fmt_money(outstanding, currency)}"
        outstanding_cls = "total-value--success" if outstanding <= 0.005 else "total-value--alert"
        summary_line = Div(
            Span(paid_label, cls="total-label"),
            Span(outstanding_label, cls=f"total-value {outstanding_cls}"),
            cls="payment-summary",
        )

    # --- Add Payment / Apply Credit form ---
    # Only show form if there's outstanding balance
    add_form = ""
    if outstanding > 0.005:
        _methods = [Option(t("doc.cash"), value="cash"), Option(t("doc.bank_transfer"), value="transfer"),
                    Option(t("doc.card"), value="card"), Option(t("doc.check"), value="check"), Option(t("doc.other"), value="other")]
        _bank_opts = [Option(f"{ba.get('account_code', '')} - {ba.get('name', '')}", value=ba.get("account_code", ""))
                      for ba in (bank_accounts or [])]
        if not _bank_opts:
            _bank_opts = [Option("1110 - Default", value="1110")]

        if is_credit_note:
            # Two forms: Apply to Invoice + Refund to Customer
            add_form = Div(
                # Apply to Invoice form
                Div(
                    H4(t("page.apply_to_invoice"), cls="form-subtitle"),
                    Form(
                        Div(
                            Div(Label(t("label.invoice"), cls="form-label"),
                                Select(
                                    Option(t("doc.loading_invoices"), value=""),
                                    name="target_doc_id", cls="form-input", id="cn-invoice-picker",
                                ), cls="form-group"),
                            Div(Label(t("label.amount"), cls="form-label"),
                                Input(type="number", name="amount", value=f"{outstanding:.2f}",
                                      step="0.01", min="0", cls="form-input", id="cn-apply-amount"), cls="form-group"),
                            Div(Label(t("th.date"), cls="form-label"),
                                Input(type="date", name="date", value=today, cls="form-input"), cls="form-group"),
                            cls="form-row",
                        ),
                        Span("", id="payment-error"),
                        Button(t("btn.apply"), type="submit", cls="btn btn--primary btn--sm"),
                        hx_post=f"/docs/{entity_id}/apply-credit", hx_swap="none", cls="form-card",
                    ),
                    cls="payment-form-section",
                ),
                # Refund to Customer form
                Div(
                    H4(t("page.refund_to_customer"), cls="form-subtitle"),
                    Form(
                        Div(
                            Div(Label(t("label.amount"), cls="form-label"),
                                Input(type="number", name="amount", value=f"{outstanding:.2f}",
                                      step="0.01", min="0", cls="form-input"), cls="form-group"),
                            Div(Label(t("th.date"), cls="form-label"),
                                Input(type="date", name="date", value=today, cls="form-input"), cls="form-group"),
                            Div(Label(t("label.method"), cls="form-label"),
                                Select(*_methods, name="method", cls="form-input"), cls="form-group"),
                            Div(Label(t("label.bank_account"), cls="form-label"),
                                Select(*_bank_opts, name="bank_account", cls="form-input"), cls="form-group"),
                            Div(Label(t("label.reference"), cls="form-label"),
                                Input(type="text", name="reference", cls="form-input"), cls="form-group"),
                            cls="form-row",
                        ),
                        Span("", id="payment-error"),
                        Button(t("btn.refund"), type="submit", cls="btn btn--secondary btn--sm"),
                        hx_post=f"/docs/{entity_id}/refund-credit", hx_swap="none", cls="form-card",
                    ),
                    cls="payment-form-section",
                ),
                # JS to populate invoice picker
                Script(f"""
(function() {{
    fetch('/docs/{entity_id}/open-invoices')
        .then(r => r.json())
        .then(invoices => {{
            const sel = document.getElementById('cn-invoice-picker');
            if (!sel) return;
            sel.innerHTML = '<option value="">-- Select Invoice --</option>';
            invoices.forEach(inv => {{
                const opt = document.createElement('option');
                opt.value = inv.id;
                opt.textContent = inv.doc_number + ' — ' + inv.contact_name + ' — Outstanding: ' + inv.outstanding.toFixed(2);
                sel.appendChild(opt);
            }});
            sel.addEventListener('change', function() {{
                const inv = invoices.find(i => i.id === sel.value);
                if (inv) {{
                    const amtEl = document.getElementById('cn-apply-amount');
                    if (amtEl) amtEl.value = Math.min({outstanding}, inv.outstanding).toFixed(2);
                }}
            }});
        }})
        .catch(() => {{}});
}})();
"""),
            )
        else:
            # Standard payment form for invoices and bills
            add_form = Div(
                H4(add_label, cls="form-subtitle"),
                Form(
                    Div(
                        Div(Label(t("label.amount"), cls="form-label"),
                            Input(type="number", name="amount", value=f"{outstanding:.2f}",
                                  step="0.01", min="0", cls="form-input"), cls="form-group"),
                        Div(Label(t("th.date"), cls="form-label"),
                            Input(type="date", name="payment_date", value=today, cls="form-input"), cls="form-group"),
                        Div(Label(t("label.method"), cls="form-label"),
                            Select(*_methods, name="method", cls="form-input"), cls="form-group"),
                        Div(Label(t("label.bank_account"), cls="form-label"),
                            Select(*_bank_opts, name="bank_account", cls="form-input"), cls="form-group"),
                        Div(Label(t("label.reference"), cls="form-label"),
                            Input(type="text", name="reference", cls="form-input"), cls="form-group"),
                        cls="form-row",
                    ),
                    Span("", id="payment-error"),
                    Button(t("btn.save_payment"), type="submit", cls="btn btn--primary btn--sm"),
                    hx_post=f"/docs/{entity_id}/payment", hx_swap="none", cls="form-card",
                ),
                cls="payment-form-section",
            )

    return Div(
        Div(Span(section_icon, cls="section-icon"), H3(section_title, cls="section-title"), cls="section-header"),
        history_table,
        summary_line,
        add_form,
        cls="doc-section payment-section",
    )


def _company_address_picker(doc_id: str, current_address: str, company_locations: list) -> FT:
    """Render address as a location picker dropdown if locations exist, else a plain editable cell."""
    if not company_locations:
        # Fallback: plain editable cell (no locations configured)
        from fasthtml.common import Span, Td
        display = current_address or "--"
        return Td(
            Span(display, cls="cell-text"),
            title="Click to edit",
            hx_get=f"/docs/{doc_id}/field/company_address/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            cls="cell cell--clickable",
        )

    def _addr_text(loc: dict) -> str:
        addr = loc.get("address") or {}
        if isinstance(addr, dict):
            return addr.get("text") or addr.get("line1") or loc.get("name") or ""
        return str(addr)

    options = [Option("-- select address --", value="", selected=(not current_address))]
    for loc in company_locations:
        addr_text = _addr_text(loc)
        options.append(Option(
            loc.get("name") or addr_text,
            value=addr_text,
            selected=(addr_text == current_address),
        ))
    # Free-text option if current_address doesn't match any location
    known = {_addr_text(l) for l in company_locations}
    if current_address and current_address not in known and current_address != "--":
        options.append(Option(f"Custom: {current_address[:40]}", value=current_address, selected=True))

    return Select(
        *options,
        name="company_address",
        hx_post=f"/docs/{doc_id}/patch",
        hx_target="this",
        hx_swap="outerHTML",
        hx_trigger="change",
        cls="cell-input cell-input--select",
    )



def _doc_detail(doc: dict, locations: list | None = None, ledger: list | None = None, price_lists: list | None = None, tc_templates: list | None = None, tz: str = "UTC", company_taxes: list | None = None, bank_accounts: list | None = None, company_locations: list | None = None, role: str = "owner") -> FT:
    def _pick(*keys: str):
        for k in keys:
            if k in doc and doc.get(k) is not None:
                return doc.get(k)
        return None

    line_items = doc.get("line_items", [])
    entity_id = doc.get("entity_id") or doc.get("id") or ""
    status = doc.get("status", "draft")
    doc_type = doc.get("doc_type", "")
    is_draft = status == "draft"
    ref = _pick("ref_id", "doc_number", "ref", "external_id") or entity_id
    from celerp.services.auth import ROLE_LEVELS as _RL
    _user_level = _RL.get(role, _RL["owner"])
    _is_manager = _user_level >= _RL["manager"]
    _is_operator = _user_level >= _RL["operator"]

    def _cell(field: str, value) -> FT:
        """Editable display cell, routing to the correct /docs/ or /lists/ URL."""
        return _doc_display_cell(entity_id, field, value, doc_type)

    contact_value = _resolve_contact_display(doc, "contact_id")
    issue_date_value = _pick("issue_date", "created_at")
    due_date_value = _pick("due_date", "payment_due_date")
    total_value = _pick("total_amount", "total")
    tax_value = _pick("tax_amount", "tax")
    outstanding_value = _pick("outstanding_balance", "amount_outstanding")
    subtotal_value = _pick("subtotal")
    discount_value = _pick("discount_amount") or 0
    currency = doc.get("currency") or "USD"
    is_list = doc_type == "list"
    # Lists use /lists/ endpoints; docs use /docs/
    _base = f"/lists/{entity_id}" if is_list else f"/docs/{entity_id}"

    # --- List type selector (shown above action buttons for lists) ---
    list_type_selector = ""
    if is_list:
        _current_lt = doc.get("list_type") or "quotation"
        if is_draft:
            list_type_selector = Div(
                Span(t("doc.list_type"), cls="meta-label"),
                Select(
                    *[Option(lt.replace("_", " ").title(), value=lt, selected=(lt == _current_lt)) for lt in _LIST_TYPES],
                    name="value",
                    hx_patch=f"/lists/{entity_id}/field/list_type",
                    hx_swap="none",
                    cls="form-select",
                ),
                cls="list-type-bar",
                style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.75rem;",
            )
        else:
            list_type_selector = Div(
                Span(t("doc.list_type"), cls="meta-label"),
                Span(_current_lt.replace("_", " ").title(), cls=f"badge badge--{_current_lt}"),
                cls="list-type-bar",
                style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.75rem;",
            )

    # --- Action buttons ---
    action_btns = []
    if doc_type == "invoice" and status in ("sent", "final", "partial", "awaiting_payment"):
        pass  # Payment section is now a separate component rendered below
    if doc_type == "quotation" and status not in ("void", "converted"):
        action_btns.append(
            Button(t("btn.convert"), hx_post=f"/docs/{entity_id}/convert",
                   hx_swap="none", cls="btn btn--primary")
        )
    # Issued memos can be converted to invoices (customer keeps goods)
    if doc_type == "memo" and status in ("final", "sent", "received", "partially_received"):
        action_btns.append(
            Button(t("btn.convert"), hx_post=f"/docs/{entity_id}/convert",
                   hx_swap="none", cls="btn btn--secondary")
        )
    # Issued consignment_in can be converted to vendor bills (vendor keeps goods)
    if doc_type == "consignment_in" and status in ("final", "sent", "received", "partially_received"):
        action_btns.append(
            Button(t("btn.convert_to_vendor_bill"), hx_post=f"/docs/{entity_id}/convert",
                   hx_swap="none", cls="btn btn--secondary")
        )
    # List-specific lifecycle buttons
    if is_list:
        if status == "draft":
            action_btns.append(Button(t("btn.send"), hx_post=f"/lists/{entity_id}/action/send", hx_swap="none", cls="btn btn--primary"))
        if status == "sent":
            action_btns.append(Button(t("btn.accept"), hx_post=f"/lists/{entity_id}/action/accept", hx_swap="none", cls="btn btn--primary"))
        if status == "accepted":
            action_btns.append(Button(t("btn.complete"), hx_post=f"/lists/{entity_id}/action/complete", hx_swap="none", cls="btn btn--primary"))
        if status not in ("void", "converted"):
            action_btns.append(Button(t("btn.convert"), hx_post=f"/lists/{entity_id}/action/convert-invoice", hx_swap="none", cls="btn btn--secondary"))
            action_btns.append(Button(t("btn.convert_to_memo"), hx_post=f"/lists/{entity_id}/action/convert-memo", hx_swap="none", cls="btn btn--secondary"))
        action_btns.append(Button(t("btn.duplicate"), hx_post=f"/lists/{entity_id}/action/duplicate", hx_swap="none", cls="btn btn--secondary"))
    if status in ("draft", "sent") and not is_list:
        _finalize_labels = {
            "invoice": "Issue Invoice",
            "purchase_order": "Convert to Bill",
            "memo": "Issue Memo",
            "consignment_in": "Issue Consignment In",
            "credit_note": "Issue Credit Note",
            "receipt": "Issue Receipt",
        }
        finalize_label = _finalize_labels.get(doc_type, "Finalize")
        if _is_manager:
            action_btns.append(
                Button(finalize_label, hx_post=f"/docs/{entity_id}/action/finalize",
                       hx_swap="none", cls="btn btn--primary")
            )
    if status == "draft" and not is_list:
        # --- Send form (inline email composition) ---
        contact_email = doc.get("contact_email") or ""
        doc_number = doc.get("ref_id") or doc.get("doc_number") or ""
        company_name = doc.get("company_name") or "Your Company"
        type_label = doc_type.replace("_", " ").title()
        default_subject = f"{type_label} #{doc_number} from {company_name}" if doc_number else ""
        default_body = f"Please find attached {type_label} #{doc_number}." if doc_number else ""
        action_btns.append(
            Details(
                Summary(t("btn.send"), cls="btn btn--secondary"),
                Form(
                    Div(Label(t("label.to_email"), cls="form-label"),
                        Input(type="email", name="sent_to", value=contact_email,
                              placeholder="recipient@example.com", cls="form-input", required=True),
                        cls="form-group"),
                    Div(Label(t("label.subject"), cls="form-label"),
                        Input(type="text", name="subject", value=default_subject,
                              cls="form-input"),
                        cls="form-group"),
                    Div(Label(t("label.message"), cls="form-label"),
                        Textarea(default_body, name="message", rows="3", cls="form-input"),
                        cls="form-group"),
                    Span("", id="send-error"),
                    Button(t("btn.send_document"), type="submit", cls="btn btn--primary"),
                    hx_post=f"/docs/{entity_id}/action/send", hx_swap="none", cls="form-card",
                ),
                cls="send-section",
            )
        )
    if status not in ("void", "draft") and _is_manager:
        action_btns.append(
            Details(
                Summary(t("btn.void"), cls="btn btn--danger"),
                Form(
                    Input(type="text", name="reason", placeholder="Void reason...", cls="form-input form-input--inline"),
                    Button(t("btn.confirm_void"), type="submit", cls="btn btn--danger"),
                    hx_post=f"{_base}/action/void", hx_swap="none", cls="inline-form",
                ),
                cls="void-section void-section--right",
            )
        )
    # "Revert to Draft" button - only from final/sent with no payments and no received items
    amount_paid_for_revert = float(doc.get("amount_paid") or 0)
    has_received_items = bool(doc.get("received_items"))
    if status in ("final", "sent") and amount_paid_for_revert == 0 and not has_received_items and _is_manager:
        action_btns.append(
            Details(
                Summary(t("doc.revert_to_draft"), cls="btn btn--secondary"),
                Form(
                    Input(type="text", name="reason", placeholder="Reason (optional)...", cls="form-input form-input--inline"),
                    Button(t("btn.confirm_revert"), type="submit", cls="btn btn--secondary"),
                    hx_post=f"{_base}/action/revert_to_draft", hx_swap="none", cls="inline-form",
                ),
                cls="void-section void-section--right",
            )
        )
    # "Unvoid" button - only from void with pre_void_status set
    if status == "void" and doc.get("pre_void_status") and _is_manager:
        action_btns.append(
            Details(
                Summary(t("doc.unvoid"), cls="btn btn--secondary"),
                Form(
                    P(f"Restore to '{doc['pre_void_status']}' status?", cls="text-muted"),
                    Button(t("btn.confirm_unvoid"), type="submit", cls="btn btn--secondary"),
                    hx_post=f"{_base}/action/unvoid", hx_swap="none", cls="inline-form",
                ),
                cls="void-section void-section--right",
            )
        )
    if status == "draft" and _is_manager:
        action_btns.append(
            Details(
                Summary(t("btn.delete"), cls="btn btn--danger"),
                Form(
                    Input(type="hidden", name="doc_type", value=doc_type),
                    P(t("doc.permanently_delete_this_draft_this_cannot_be_undon"), cls="text-muted"),
                    Button(t("btn.confirm_delete"), type="submit", cls="btn btn--danger"),
                    hx_post=f"{_base}/action/delete", hx_swap="none", cls="inline-form",
                ),
                cls="void-section void-section--right",
            )
        )
    # Refund is now handled via credit notes + void in the payment section
    # "Mark as Sent" button (instant, no confirmation; undo via "Unmark")
    if status in ("draft", "sent") and not is_list:
        if status == "draft":
            action_btns.append(
                Button(t("btn.mark_as_sent"), hx_post=f"/docs/{entity_id}/action/mark_sent",
                       hx_swap="none", cls="btn btn--secondary")
            )
        else:
            # Already sent - offer undo
            action_btns.append(
                Button(t("btn.unmark_sent"), hx_post=f"/docs/{entity_id}/action/unmark_sent",
                       hx_swap="none", cls="btn btn--secondary")
            )
    # PDF button
    if not is_list:
        action_btns.append(A("PDF", href=f"/docs/{entity_id}/pdf", target="_blank", cls="btn btn--secondary"))
    # CSV line items export/import icons
    action_btns.append(
        A(NotStr(_ICON_CSV_EXPORT), href=f"{_base}/items/csv",
          cls="btn btn--ghost btn--icon", title=t("doc.export_line_items_csv")),
    )
    if is_draft:
        action_btns.append(
            Button(NotStr(_ICON_CSV_IMPORT), type="button",
                   cls="btn btn--ghost btn--icon", title=t("doc.import_line_items_csv"),
                   onclick="document.getElementById('csv-import-input').click()"),
        )
    action_btns.append(Span("", id="share-result"))
    action_btns.append(Span("", id="action-error"))

    # --- Slot: doc_detail_actions (module-contributed action buttons) ---
    from celerp.modules.slots import get as _get_slot
    for _contrib in _get_slot("doc_detail_actions"):
        _render_path = _contrib.get("render", "")
        if _render_path:
            try:
                _mod_path, _fn_name = _render_path.rsplit(":", 1)
                import importlib as _il
                _render_fn = getattr(_il.import_module(_mod_path), _fn_name)
                _el = _render_fn(doc)
                if _el is not None:
                    action_btns.append(_el)
            except Exception:
                pass

    # --- Slot: doc_detail_badges (module-contributed status badges) ---
    _slot_badges = []
    for _contrib in _get_slot("doc_detail_badges"):
        _render_path = _contrib.get("render", "")
        if _render_path:
            try:
                _mod_path, _fn_name = _render_path.rsplit(":", 1)
                import importlib as _il
                _render_fn = getattr(_il.import_module(_mod_path), _fn_name)
                _el = _render_fn(doc)
                if _el is not None:
                    _slot_badges.append(_el)
            except Exception:
                pass

    # --- Bill receive (per-line) ---
    po_receive_section = ""
    if doc_type in ("bill", "purchase_order") and status in ("awaiting_payment", "finalized", "sent", "final", "partially_received"):
        po_items = doc.get("line_items", [])
        if po_items:
            receive_rows = []
            for i, li in enumerate(po_items):
                qty_ordered = float(li.get("quantity", 0) or 0)
                qty_received = float(li.get("quantity_received", 0) or 0)
                qty_remaining = max(0, qty_ordered - qty_received)
                desc = str(li.get("description", "") or li.get("sku", "") or f"Item {i + 1}")
                receive_rows.append(Tr(
                    Td(desc),
                    Td(str(qty_ordered)),
                    Td(str(qty_received)),
                    Td(
                        Input(type="hidden", name=f"item_id_{i}", value=li.get("item_id", "") or ""),
                        Input(type="number", name=f"qty_{i}", value=str(qty_remaining),
                              step="any", min="0", max=str(qty_remaining),
                              cls="form-input form-input--sm"),
                    ) if qty_remaining > 0 else Td(Span(t("doc.fully_received"), cls="badge badge--green")),
                ))
            loc_opts = [Option(loc.get("name", ""), value=loc.get("name", "")) for loc in (locations or [])]
            po_receive_section = Details(
                Summary(t("doc.receive_goods"), cls="btn btn--secondary"),
                Form(
                    Table(
                        Thead(Tr(Th(t("th.item")), Th(t("th.ordered")), Th(t("doc.received")), Th(t("th.qty_to_receive")))),
                        Tbody(*receive_rows),
                        cls="data-table data-table--compact",
                    ),
                    Div(Label(t("th.location"), cls="form-label"),
                        Select(*loc_opts, name="location_name", cls="form-input") if loc_opts else
                        Input(type="text", name="location_name", placeholder="Location", cls="form-input"),
                        cls="form-group"),
                    Div(Label(t("th.notes"), cls="form-label"),
                        Textarea("", name="notes", rows="2", cls="form-input"), cls="form-group"),
                    Span("", id="action-error"),
                    Button(t("btn.record_receipt"), type="submit", cls="btn btn--primary"),
                    hx_post=f"/docs/{entity_id}/receive", hx_swap="none", cls="form-card",
                ),
                cls="receive-section",
            )

    # --- Price list bar (positioned in line items section) ---
    _pl_names = [pl.get("name", "") for pl in (price_lists or []) if pl.get("name")]
    _current_pl = doc.get("price_list") or ""
    if is_draft and _pl_names:
        _pl_select = Select(
            *[Option(name, value=name, selected=(name == _current_pl)) for name in _pl_names],
            id="doc-price-list",
            cls="cell-input cell-input--select",
            onchange=f"celerpReprice(this.value)",
        )
        _pl_bar = Div(
            Span(t("doc.price_list"), cls="meta-label"),
            _pl_select,
            cls="price-list-bar",
            style="display:flex;align-items:center;gap:0.5rem;justify-content:flex-end;max-width:250px;margin-left:auto;margin-bottom:0.5rem;",
        )
    else:
        _pl_bar = Div(
            Span(t("doc.price_list"), cls="meta-label"),
            Span(_current_pl or "-", cls="meta-value"),
            cls="price-list-bar",
            style="display:flex;align-items:center;gap:0.5rem;justify-content:flex-end;max-width:250px;margin-left:auto;margin-bottom:0.5rem;",
        ) if _current_pl else ""

    # --- Line items section ---
    line_body_id = "line-body"
    if is_draft:
        def _sku_input(val: str = "", entity_id: str = "") -> FT:
            eye_cls = "item-link item-link--active" if entity_id else "item-link item-link--inactive"
            eye_href = f"/items/{entity_id}" if entity_id else "#"
            eye = A("👁", href=eye_href, target="_blank" if entity_id else "",
                     cls=eye_cls, data_name="item_link",
                     title="View item details" if entity_id else "No linked item",
                     onclick="" if entity_id else "event.preventDefault();")
            return Div(
                eye,
                Input(type="text", value=val, data_name="sku", placeholder="SKU...",
                      cls="cell-input cell-input--sm catalog-ac-input",
                      autocomplete="off",
                      title="Type to search catalog or enter custom description",
                      oninput="celerpAcSearch(this,'sku')",
                      onblur="celerpAcBlur(this)",
                      onkeydown="celerpAcKey(event,this)"),
                Div(cls="catalog-ac-list", style="display:none"),
                cls="catalog-ac-wrap",
            )

        def _desc_input(val: str = "") -> FT:
            return Div(
                Input(type="text", value=val, data_name="description", placeholder="Description…",
                      cls="cell-input cell-input--sm catalog-ac-input",
                      autocomplete="off",
                      title="Type to search catalog or enter custom description",
                      oninput="celerpAcSearch(this,'description')",
                      onblur="celerpAcBlur(this)",
                      onkeydown="celerpAcKey(event,this)"),
                Div(cls="catalog-ac-list", style="display:none"),
                cls="catalog-ac-wrap",
            )

        import json as _json
        _taxes_list = company_taxes or []
        _default_tax = next((tax for tax in _taxes_list if tax.get("is_default")), None)
        _default_tax_value = f"{_default_tax.get('name', '')}|{float(_default_tax.get('rate', 0))}" if _default_tax else "|0"

        def _tax_select(current_rate: float = 0.0, current_code: str = "", current_label: str = "") -> FT:
            """Build tax <select> + hidden custom-rate input + hidden label for a line item."""
            # Determine selected value: match by code first, then by rate
            selected_val = "|0"
            is_custom = False
            for tax in _taxes_list:
                tcode = tax.get("name", "")
                trate = float(tax.get("rate", 0))
                if current_code and tcode == current_code:
                    selected_val = f"{tcode}|{trate}"
                    break
                if not current_code and trate == current_rate and current_rate != 0:
                    selected_val = f"{tcode}|{trate}"
                    break
            else:
                if current_rate != 0 and not any(float(tax.get("rate", 0)) == current_rate for tax in _taxes_list):
                    selected_val = "|custom"
                    is_custom = True

            options = [Option(t("doc.no_tax"), value="|0", selected=(selected_val == "|0"))]
            for tax in _taxes_list:
                tcode = tax.get("name", "")
                trate = float(tax.get("rate", 0))
                val = f"{tcode}|{trate}"
                options.append(Option(f"{tcode} ({trate}%)", value=val, selected=(selected_val == val)))
            options.append(Option(t("doc.custom"), value="|custom", selected=is_custom))

            custom_input = Input(
                type="number", value=str(current_rate) if is_custom else "0",
                step="0.01", data_name="tax_rate_custom",
                cls="cell-input cell-input--xs",
                style=("display:inline-block;" if is_custom else "display:none;"),
            )
            return Div(
                Select(*options, data_name="tax_select",
                       cls="cell-input cell-input--select cell-input--xs",
                       onchange="celerpTaxChange(this)",
                       onblur="celerpAutoSave()"),
                custom_input,
                Input(type="hidden", value=current_label, data_name="tax_label"),
                style="display:flex;gap:2px;align-items:center;",
            )

        def _li_editable_row(li: dict, idx: int) -> FT:
            qty = li.get("quantity", 0)
            price = li.get("unit_price", 0)
            discount_pct = float(li.get("discount_pct") or 0)
            discounted = float(qty or 0) * float(price or 0) * (1 - discount_pct / 100)
            line_tot = discounted
            li_entity_id = li.get("entity_id") or ""
            li_allow_splitting = "1" if li.get("allow_splitting") else ""
            account_cell = Td(Input(type="text", value=li.get("account_code", "") or "",
                         data_name="account_code", placeholder="e.g. 1130",
                         cls="cell-input cell-input--xs",
                         onblur="celerpAutoSave()")) if doc_type in ("purchase_order", "bill") else None
            cells = [
                Td(_sku_input(li.get("sku", "") or "", li_entity_id)),
                Td(_desc_input(li.get("description", "") or li.get("name", ""))),
                Td(Input(type="number", value=str(qty), step="any",
                         data_name="quantity", oninput="celerpUpdateTotals()",
                         onblur="celerpQtyBlur(this); celerpAutoSave()",
                         cls="cell-input cell-input--xs")),
                Td(Span(li.get("unit", "") or "", data_name="unit", cls="meta-value meta-value--muted",
                         style="font-size:12px;display:inline-block;min-width:40px;")),
                Td(Input(type="number", value=str(price), step="0.01",
                         data_name="unit_price", oninput="celerpUpdateTotals()",
                         onblur="celerpAutoSave()",
                         cls="cell-input cell-input--xs")),
                Td(Input(type="number", value=str(discount_pct) if discount_pct else "0", step="0.01",
                         data_name="discount_pct", oninput="celerpUpdateTotals()",
                         onblur="celerpAutoSave()",
                         cls="cell-input cell-input--xs")),
                Td(_tax_select(float(li.get("tax_rate", 0) or 0), li.get("tax_code", "") or "",
                              ((li.get("taxes") or [{}])[0].get("label", "") if li.get("taxes") else ""))),
                Td(Input(type="hidden", value=li.get("hs_code", "") or "", data_name="hs_code"),
                   Input(type="hidden", value=li_entity_id, data_name="entity_id"),
                   Input(type="hidden", value=li_allow_splitting, data_name="allow_splitting"),
                   Input(type="hidden", value=str(li.get("item_quantity") or qty), data_name="item_quantity")),
            ]
            if account_cell:
                cells.append(account_cell)
            cells.extend([
                Td(Span(fmt_money(line_tot, currency), cls="line-total"), cls="cell--number"),
                Td(Button("x", type="button", cls="btn btn--danger btn--xs",
                          onclick="this.closest('tr').remove(); celerpUpdateTotals(); celerpAutoSave();")),
            ])
            return Tr(*cells)

        def _li_empty_row() -> FT:
            return Tr(
                Td(_sku_input()),
                Td(_desc_input()),
                Td(Input(type="number", value="1", step="any", data_name="quantity",
                         oninput="celerpUpdateTotals()", onblur="celerpQtyBlur(this); celerpAutoSave()",
                         cls="cell-input cell-input--xs")),
                Td(Span("", data_name="unit", cls="meta-value meta-value--muted",
                         style="font-size:12px;display:inline-block;min-width:40px;")),
                Td(Input(type="number", value="0", step="0.01", data_name="unit_price",
                         oninput="celerpUpdateTotals()", onblur="celerpAutoSave()",
                         cls="cell-input cell-input--xs")),
                Td(Input(type="number", value="0", step="0.01", data_name="discount_pct",
                         oninput="celerpUpdateTotals()", onblur="celerpAutoSave()",
                         cls="cell-input cell-input--xs")),
                Td(_tax_select()),
                Td(Input(type="hidden", value="", data_name="hs_code"),
                   Input(type="hidden", value="", data_name="entity_id"),
                   Input(type="hidden", value="", data_name="allow_splitting"),
                   Input(type="hidden", value="", data_name="item_quantity")),
                Td(Span(fmt_money(0, currency), cls="line-total"), cls="cell--number"),
                Td(Button("x", type="button", cls="btn btn--danger btn--xs",
                          onclick="this.closest('tr').remove(); celerpUpdateTotals(); celerpAutoSave();")),
            )

        rows = [_li_editable_row(li, i) for i, li in enumerate(line_items)]
        if not rows:
            rows = [_li_empty_row()]

        _line_headers = [Th(t("th.skuitem")), Th(t("th.description")), Th(t("th.qty")), Th(t("th.unit")), Th(t("th.unit_price")), Th(t("th.disc")), Th(t("th.tax"))]
        if doc_type in ("purchase_order", "bill"):
            _line_headers.append(Th(t("th.account")))
        _line_headers.extend([Th(t("th.total")), Th("")])

        # CSV import hidden file input + JS handler
        _csv_import_el = Div(
            Input(type="file", id="csv-import-input", accept=".csv,.tsv,.txt",
                  style="display:none",
                  onchange=f"celerpCsvImport(this, '{entity_id}')"),
            cls="csv-import-hidden",
        )

        lines_section = Div(
            _csv_import_el,
            Template(_li_empty_row(), id="line-row-tpl"),
            Div(
                Span("📷", cls="scan-bar-icon"),
                Input(type="text", id="scan-bar-input", placeholder="Scan barcode or type SKU and press Enter",
                      cls="scan-bar-input", autocomplete="off", autofocus=False),
                Span("", id="scan-bar-status", cls="scan-bar-status"),
                cls="scan-bar",
            ),
            Div(
                _pl_bar,
                cls="line-toolbar",
            ),
            Table(
                Thead(Tr(*_line_headers)),
                Tbody(*rows, id=line_body_id),
                cls="data-table doc-lines",
            ),
            Div(
                Button(t("btn._add_item"), type="button", cls="btn btn--secondary",
                       onclick="celerpAddLine()"),
                Span("", id="save-status", cls="save-status"),
                cls="line-actions gap-sm",
            ),
            Script(f"""
const _CELERP_EID = {repr(entity_id)};
const _CELERP_BASE = {'"/lists/"' if is_list else '"/docs/"'};
const _CELERP_TAXES = {_json.dumps(_taxes_list)};
const _CELERP_DEFAULT_TAX = {repr(_default_tax_value)};
/* ── Price list helper ── */
function _celerpPriceListParam() {{
    const plSelect = document.getElementById('doc-price-list');
    return plSelect ? '&price_list=' + encodeURIComponent(plSelect.value) : '';
}}
/* ── Barcode scan bar ── */
(function() {{
    const scanInput = document.getElementById('scan-bar-input');
    const scanStatus = document.getElementById('scan-bar-status');
    if (!scanInput) return;
    scanInput.addEventListener('keydown', async function(e) {{
        if (e.key !== 'Enter') return;
        e.preventDefault();
        const code = scanInput.value.trim();
        if (!code) return;
        scanStatus.textContent = 'Looking up...';
        scanStatus.className = 'scan-bar-status';
        try {{
            const resp = await fetch('/docs/catalog-lookup?sku=' + encodeURIComponent(code) + _celerpPriceListParam());
            if (!resp.ok) throw new Error('lookup failed');
            const data = await resp.json();
            if (data.description || data.sku) {{
                // Add a new line with the item data
                const tpl = document.getElementById('line-row-tpl').content.cloneNode(true);
                const row = tpl.querySelector('tr') || tpl.children[0];
                if (row) {{
                    const d = {{...data, sku: data.sku || code}};
                    celerpFillRow(row, d);
                }}
                document.getElementById('{line_body_id}').appendChild(tpl);
                celerpUpdateTotals();
                celerpAutoSave();
                scanStatus.textContent = '✓ ' + (data.sku || code);
                scanStatus.className = 'scan-bar-status scan-bar-status--ok';
            }} else {{
                scanStatus.textContent = '✗ Not found: ' + code;
                scanStatus.className = 'scan-bar-status scan-bar-status--err';
            }}
        }} catch (err) {{
            scanStatus.textContent = '✗ Lookup error';
            scanStatus.className = 'scan-bar-status scan-bar-status--err';
        }}
        scanInput.value = '';
        scanInput.focus();
        setTimeout(() => {{ scanStatus.textContent = ''; }}, 3000);
    }});
}})();
function celerpFillRow(row, data) {{
    const descEl = row.querySelector('[data-name="description"]');
    const priceEl = row.querySelector('[data-name="unit_price"]');
    const unitEl = row.querySelector('[data-name="unit"]');
    const qtyEl = row.querySelector('[data-name="quantity"]');
    const skuEl = row.querySelector('[data-name="sku"]');
    const hsCodeEl = row.querySelector('[data-name="hs_code"]');
    const entityIdEl = row.querySelector('[data-name="entity_id"]');
    const allowSplitEl = row.querySelector('[data-name="allow_splitting"]');
    const itemQtyEl = row.querySelector('[data-name="item_quantity"]');
    if (skuEl && data.sku) skuEl.value = data.sku;
    if (descEl && data.description && !descEl.value) descEl.value = data.description;
    if (hsCodeEl && data.hs_code) hsCodeEl.value = data.hs_code;
    if (priceEl && data.unit_price != null) priceEl.value = data.unit_price;
    if (unitEl && data.sell_by) unitEl.textContent = data.sell_by;
    if (entityIdEl) entityIdEl.value = data.entity_id || '';
    if (allowSplitEl) allowSplitEl.value = data.allow_splitting ? '1' : '';
    if (itemQtyEl && data.quantity) itemQtyEl.value = data.quantity;
    // Set quantity: if allow_splitting is false, use full item quantity
    if (qtyEl) {{
        if (!data.allow_splitting && data.quantity > 0) {{
            qtyEl.value = data.quantity;
        }} else if (data.quantity > 0 && (!qtyEl.value || qtyEl.value === '1')) {{
            qtyEl.value = data.quantity;
        }}
    }}
    // Update eye icon link
    const linkEl = row.querySelector('[data-name="item_link"]');
    if (linkEl) {{
        if (data.entity_id) {{
            linkEl.href = '/items/' + data.entity_id;
            linkEl.target = '_blank';
            linkEl.className = 'item-link item-link--active';
            linkEl.title = 'View item details';
            linkEl.onclick = null;
        }} else {{
            linkEl.href = '#';
            linkEl.target = '';
            linkEl.className = 'item-link item-link--inactive';
            linkEl.title = 'No linked item';
            linkEl.onclick = (e) => e.preventDefault();
        }}
    }}
    // Set tax dropdown: use item tax_code/tax_rate, fall back to company default
    const taxSel = row.querySelector('[data-name="tax_select"]');
    if (taxSel) {{
        let matched = false;
        if (data.tax_code) {{
            for (const opt of taxSel.options) {{
                if (opt.value.split('|')[0] === data.tax_code) {{ taxSel.value = opt.value; matched = true; break; }}
            }}
        }}
        if (!matched && data.tax_rate != null && data.tax_rate > 0) {{
            for (const opt of taxSel.options) {{
                if (parseFloat(opt.value.split('|')[1]) === data.tax_rate && opt.value !== '|custom') {{
                    taxSel.value = opt.value; matched = true; break;
                }}
            }}
        }}
        if (!matched) taxSel.value = _CELERP_DEFAULT_TAX;
        celerpTaxChange(taxSel);
    }}
}}
/* ── Catalog autocomplete ── */
let _celerpAcTimer = null;
async function celerpAcSearch(input, field) {{
    const q = input.value.trim();
    const wrap = input.parentElement;
    const list = wrap.querySelector('.catalog-ac-list');
    if (!q || q.length < 2) {{ list.style.display = 'none'; return; }}
    clearTimeout(_celerpAcTimer);
    _celerpAcTimer = setTimeout(async () => {{
        const pl = _celerpPriceListParam();
        const resp = await fetch('/docs/catalog-search?q=' + encodeURIComponent(q) + pl);
        if (!resp.ok) return;
        const items = await resp.json();
        list.innerHTML = '';
        items.forEach(item => {{
            const opt = document.createElement('div');
            opt.className = 'catalog-ac-option';
            const label = field === 'sku'
                ? (item.sku || '') + (item.description ? ' – ' + item.description : '')
                : (item.description || '') + (item.sku ? ' [' + item.sku + ']' : '');
            opt.textContent = label;
            opt.addEventListener('mousedown', e => {{
                e.preventDefault();
                const row = input.closest('tr');
                celerpFillRow(row, {{...item, description: item.description}});
                list.style.display = 'none';
                celerpUpdateTotals();
                celerpAutoSave();
            }});
            list.appendChild(opt);
        }});
        // Always append "Use as custom entry" at the bottom
        const custom = document.createElement('div');
        custom.className = 'catalog-ac-option catalog-ac-option--custom';
        custom.textContent = '✏ Use as custom entry: "' + q + '"';
        custom.addEventListener('mousedown', e => {{
            e.preventDefault();
            list.style.display = 'none';
            // Keep whatever the user typed – no catalog fill
        }});
        list.appendChild(custom);
        list.style.display = 'block';
    }}, 250);
}}
function celerpAcBlur(input) {{
    const list = input.parentElement.querySelector('.catalog-ac-list');
    setTimeout(() => {{ list.style.display = 'none'; }}, 200);
    celerpAutoSave();
}}
function celerpAcKey(e, input) {{
    const list = input.parentElement.querySelector('.catalog-ac-list');
    const opts = list.querySelectorAll('.catalog-ac-option');
    const active = list.querySelector('.catalog-ac-option--active');
    if (e.key === 'ArrowDown') {{
        e.preventDefault();
        const next = active ? active.nextElementSibling : opts[0];
        if (active) active.classList.remove('catalog-ac-option--active');
        if (next) next.classList.add('catalog-ac-option--active');
    }} else if (e.key === 'ArrowUp') {{
        e.preventDefault();
        const prev = active ? active.previousElementSibling : opts[opts.length - 1];
        if (active) active.classList.remove('catalog-ac-option--active');
        if (prev) prev.classList.add('catalog-ac-option--active');
    }} else if (e.key === 'Enter' && active) {{
        e.preventDefault();
        active.dispatchEvent(new MouseEvent('mousedown'));
    }} else if (e.key === 'Escape') {{
        list.style.display = 'none';
    }}
}}
function celerpQtyBlur(input) {{
    const row = input.closest('tr');
    if (!row) return;
    const allowSplit = row.querySelector('[data-name="allow_splitting"]');
    const itemQtyEl = row.querySelector('[data-name="item_quantity"]');
    const entityIdEl = row.querySelector('[data-name="entity_id"]');
    if (!allowSplit || !entityIdEl || !entityIdEl.value) return;
    // allow_splitting = "1" means splittable, empty/"" means not splittable
    if (allowSplit.value === '1') return;
    const itemQty = parseFloat(itemQtyEl?.value || 0);
    const currentQty = parseFloat(input.value || 0);
    if (itemQty > 0 && currentQty !== itemQty) {{
        const eid = entityIdEl.value;
        const msg = 'Allow splitting is set to false for this item, so you cannot sell less than the full quantity (' + itemQty + '). '
            + 'You can modify this in the item details page: /items/' + eid;
        alert(msg);
        // Per UX rules: do NOT revert the value or make readonly - just warn
    }}
}}
function celerpTaxChange(sel) {{
    const customInput = sel.parentElement.querySelector('[data-name="tax_rate_custom"]');
    if (!customInput) return;
    if (sel.value === '|custom') {{
        customInput.style.display = 'inline-block';
        customInput.oninput = () => celerpUpdateTotals();
        customInput.onblur = () => celerpAutoSave();
    }} else {{
        customInput.style.display = 'none';
    }}
    celerpUpdateTotals();
}}
function _celerpTaxRate(row) {{
    const sel = row.querySelector('[data-name="tax_select"]');
    if (!sel) return 0;
    if (sel.value === '|custom') {{
        return parseFloat(row.querySelector('[data-name="tax_rate_custom"]')?.value || 0);
    }}
    return parseFloat(sel.value.split('|')[1] || 0);
}}
function _celerpTaxCode(row) {{
    const sel = row.querySelector('[data-name="tax_select"]');
    if (!sel || sel.value === '|custom' || sel.value === '|0') return '';
    return sel.value.split('|')[0];
}}
function _celerpEditTaxLabel(key, rate, labelEl) {{
    const currentText = labelEl.textContent.replace(/:$/, '').replace(/\s*\(\d+(\.\d+)?%\)$/, '');
    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentText;
    input.className = 'cell-input cell-input--xs';
    input.style.width = '120px';
    input.style.display = 'inline';
    const commit = () => {{
        const newLabel = input.value.trim() || 'Custom';
        // Update all matching line rows' hidden tax_label inputs
        document.querySelectorAll('#{line_body_id} tr').forEach(row => {{
            const sel = row.querySelector('[data-name="tax_select"]');
            if (sel && sel.value === '|custom') {{
                const rowRate = parseFloat(row.querySelector('[data-name="tax_rate_custom"]')?.value || 0);
                if (rowRate === rate) {{
                    const lbl = row.querySelector('[data-name="tax_label"]');
                    if (lbl) lbl.value = newLabel;
                }}
            }}
        }});
        celerpUpdateTotals();
        celerpAutoSave();
    }};
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', e => {{
        if (e.key === 'Enter') {{ e.preventDefault(); input.blur(); }}
        if (e.key === 'Escape') {{ e.preventDefault(); input.value = currentText; input.blur(); }}
    }});
    labelEl.textContent = '';
    labelEl.appendChild(input);
    input.focus();
    input.select();
}}
function celerpAddLine() {{
    const tpl = document.getElementById('line-row-tpl').content.cloneNode(true);
    const row = tpl.querySelector('tr') || tpl.children[0];
    if (row) {{
        const taxSel = row.querySelector('[data-name="tax_select"]');
        if (taxSel && _CELERP_DEFAULT_TAX) {{
            taxSel.value = _CELERP_DEFAULT_TAX;
            celerpTaxChange(taxSel);
        }}
    }}
    document.getElementById('{line_body_id}').appendChild(tpl);
    celerpUpdateTotals();
}}
function celerpUpdateTotals() {{
    const _cur = {repr(currency)};
    function _fmt(n) {{
        try {{ return new Intl.NumberFormat('en-US', {{style:'currency',currency:_cur}}).format(n); }}
        catch(e) {{ return _cur + ' ' + n.toLocaleString('en-US', {{minimumFractionDigits:2}}); }}
    }}
    let sub = 0;
    let grossSub = 0;
    let totalDiscount = 0;
    const taxByCode = {{}};
    document.querySelectorAll('#{line_body_id} tr').forEach(row => {{
        const qty = parseFloat(row.querySelector('[data-name="quantity"]')?.value || 0);
        const price = parseFloat(row.querySelector('[data-name="unit_price"]')?.value || 0);
        const discPct = parseFloat(row.querySelector('[data-name="discount_pct"]')?.value || 0);
        const gross = qty * price;
        const discAmt = gross * discPct / 100;
        const tot = gross - discAmt;
        grossSub += gross;
        totalDiscount += discAmt;
        const totalEl = row.querySelector('.line-total');
        if (totalEl) totalEl.textContent = _fmt(tot);
        sub += tot;
        const rate = _celerpTaxRate(row);
        if (rate !== 0) {{
            const code = _celerpTaxCode(row);
            const key = code || ('custom_' + rate);
            const customLabel = row.querySelector('[data-name="tax_label"]')?.value || '';
            const label = code
                ? ((_CELERP_TAXES.find(t => t.name === code) || {{}}).name || code) + ' (' + rate + '%)'
                : (customLabel || 'Custom') + ' (' + rate + '%)';
            if (!taxByCode[key]) taxByCode[key] = {{label, amount: 0, isCustom: !code, rate}};
            taxByCode[key].amount += tot * rate / 100;
        }}
    }});
    // Gross subtotal + discount breakdown (only when line discounts exist)
    const grossEl = document.getElementById('doc-gross-subtotal');
    const discEl = document.getElementById('doc-line-discount');
    if (totalDiscount > 0.005) {{
        if (!grossEl) {{
            // Insert gross subtotal + discount rows before the net subtotal
            const subEl = document.getElementById('doc-subtotal');
            if (subEl) {{
                const subRow = subEl.closest('.total-row');
                if (subRow) {{
                    const discRow = document.createElement('div');
                    discRow.className = 'total-row';
                    discRow.innerHTML = '<span class="total-label">Discount:</span><span class="total-value" id="doc-line-discount">-' + _fmt(totalDiscount) + '</span>';
                    subRow.parentNode.insertBefore(discRow, subRow);
                    const grossRow = document.createElement('div');
                    grossRow.className = 'total-row';
                    grossRow.innerHTML = '<span class="total-label">Subtotal:</span><span class="total-value" id="doc-gross-subtotal">' + _fmt(grossSub) + '</span>';
                    discRow.parentNode.insertBefore(grossRow, discRow);
                    // Relabel net subtotal
                    const lbl = subRow.querySelector('.total-label');
                    if (lbl) lbl.textContent = 'Net Subtotal:';
                }}
            }}
        }} else {{
            grossEl.textContent = _fmt(grossSub);
            if (discEl) discEl.textContent = '-' + _fmt(totalDiscount);
            // Ensure label says Net Subtotal
            const subEl = document.getElementById('doc-subtotal');
            if (subEl) {{
                const lbl = subEl.closest('.total-row')?.querySelector('.total-label');
                if (lbl) lbl.textContent = 'Net Subtotal:';
            }}
        }}
    }} else {{
        // Remove gross/discount rows if no discount
        if (grossEl) grossEl.closest('.total-row')?.remove();
        if (discEl) discEl.closest('.total-row')?.remove();
        const subEl = document.getElementById('doc-subtotal');
        if (subEl) {{
            const lbl = subEl.closest('.total-row')?.querySelector('.total-label');
            if (lbl) lbl.textContent = 'Subtotal:';
        }}
    }}
    const subEl = document.getElementById('doc-subtotal');
    if (subEl) subEl.textContent = _fmt(sub);
    // Update per-code tax rows
    const taxContainer = document.getElementById('doc-tax-rows');
    if (taxContainer) {{
        taxContainer.innerHTML = '';
        let totalTax = 0;
        Object.entries(taxByCode).forEach(([key, t]) => {{
            totalTax += t.amount;
            const row = document.createElement('div');
            row.className = 'total-row';
            if (t.isCustom) {{
                const lbl = document.createElement('span');
                lbl.className = 'total-label total-label--editable';
                lbl.textContent = t.label + ':';
                lbl.title = 'Double-click to rename';
                lbl.style.cursor = 'pointer';
                lbl.addEventListener('dblclick', () => _celerpEditTaxLabel(key, t.rate, lbl));
                const val = document.createElement('span');
                val.className = 'total-value';
                val.textContent = _fmt(t.amount);
                row.appendChild(lbl);
                row.appendChild(val);
            }} else {{
                row.innerHTML = '<span class="total-label">' + t.label + ':</span><span class="total-value">' + _fmt(t.amount) + '</span>';
            }}
            taxContainer.appendChild(row);
        }});
        const totEl = document.getElementById('doc-total');
        if (totEl) totEl.textContent = _fmt(sub + totalTax);
    }} else {{
        const totEl = document.getElementById('doc-total');
        if (totEl) totEl.textContent = _fmt(sub);
    }}
}}
function _celerpCollectLines() {{
    const lines = [];
    document.querySelectorAll('#{line_body_id} tr').forEach(row => {{
        const desc = row.querySelector('[data-name="description"]')?.value;
        const sku = row.querySelector('[data-name="sku"]')?.value;
        const qty = parseFloat(row.querySelector('[data-name="quantity"]')?.value || 0);
        const unitEl = row.querySelector('[data-name="unit"]'); const unit = unitEl ? (unitEl.value || unitEl.textContent || '').trim() : '';
        const price = parseFloat(row.querySelector('[data-name="unit_price"]')?.value || 0);
        const discPct = parseFloat(row.querySelector('[data-name="discount_pct"]')?.value || 0);
        const rate = _celerpTaxRate(row);
        const code = _celerpTaxCode(row);
        const hsCode = row.querySelector('[data-name="hs_code"]')?.value || null;
        const entityId = row.querySelector('[data-name="entity_id"]')?.value || null;
        const taxLabel = row.querySelector('[data-name="tax_label"]')?.value || '';
        if (desc || sku || price) {{
            const discounted = qty * price * (1 - discPct / 100);
            const taxList = rate !== 0 ? [{{code: code, rate: rate, amount: 0, order: 0, is_compound: false, label: taxLabel}}] : [];
            lines.push({{description: desc || '', sku: sku || '', quantity: qty || 1, unit,
                         unit_price: price, discount_pct: discPct, tax_rate: rate, taxes: taxList,
                         line_total: discounted, hs_code: hsCode || undefined,
                         entity_id: entityId || undefined}});
        }}
    }});
    return lines;
}}
async function _celerpPersist() {{
    const lines = _celerpCollectLines();
    if (!lines.length) return;
    const subtotal = lines.reduce((s, l) => s + l.line_total, 0);
    const tax = lines.reduce((s, l) => s + l.line_total * (l.tax_rate / 100), 0);
    const statusEl = document.getElementById('save-status');
    const resp = await fetch(_CELERP_BASE + _CELERP_EID + '/lines', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{line_items: lines, subtotal, tax, total: subtotal + tax}})
    }});
    if (resp.ok) {{
        statusEl.textContent = '✓';
        setTimeout(() => {{ statusEl.textContent = ''; }}, 1500);
    }} else {{
        statusEl.textContent = '✗ Save failed';
        statusEl.style.color = 'red';
    }}
}}
/* Auto-save on blur away from any row cell */
let _celerpSaveTimer = null;
function celerpAutoSave() {{
    clearTimeout(_celerpSaveTimer);
    _celerpSaveTimer = setTimeout(_celerpPersist, 400);
}}
async function celerpReprice(priceList) {{
    /* Save current lines first, then reprice via API and reload */
    await _celerpPersist();
    const resp = await fetch(_CELERP_BASE + _CELERP_EID + '/reprice', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{price_list: priceList}})
    }});
    if (resp.ok) {{
        window.location.reload();
    }} else {{
        const err = await resp.json().catch(() => ({{}}));
        alert(err.error || 'Reprice failed');
    }}
}}
/* ── CSV import ── */
async function celerpCsvImport(input, entityId) {{
    const file = input.files && input.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    const plSelect = document.getElementById('doc-price-list');
    if (plSelect) formData.append('price_list', plSelect.value);
    try {{
        const resp = await fetch(_CELERP_BASE + entityId + '/items/csv', {{
            method: 'POST', body: formData
        }});
        const data = await resp.json();
        if (resp.ok && data.ok) {{
            window.location.reload();
        }} else {{
            alert(data.error || 'Import failed');
        }}
    }} catch (err) {{
        alert('Import failed: ' + err.message);
    }}
    input.value = '';
}}
"""),
            cls="lines-section",
        )
    else:
        def _li_row(li: dict) -> FT:
            qty = float(li.get("quantity", 0) or 0)
            price = float(li.get("unit_price", 0) or 0)
            discount_pct = float(li.get("discount_pct") or 0)
            discounted = qty * price * (1 - discount_pct / 100) if discount_pct else qty * price
            line_total = float(li.get("line_total", 0) or 0) or discounted
            return Tr(
                Td(format_value(li.get("description") or li.get("name"))),
                Td(format_value(li.get("sku") or None)),
                Td(format_value(li.get("quantity"))),
                Td(format_value(li.get("unit") or None)),
                Td(format_value(li.get("unit_price"), "money"), cls="cell--number"),
                Td(f"{discount_pct:.1f}%" if discount_pct else "-"),
                Td(format_value(li.get("tax_rate"))),
                Td(format_value(line_total, "money"), cls="cell--number"),
            )
        lines_section = Div(
            Table(
                Thead(Tr(Th(t("th.description")), Th(t("th.skuitem")), Th(t("th.qty")), Th(t("th.unit")), Th(t("th.unit_price")), Th(t("th.disc")), Th(t("th.tax")), Th(t("th.total")))),
                Tbody(*([_li_row(li) for li in line_items] if line_items else [
                    Tr(Td(t("doc.no_line_items"), colspan="8", cls="empty-state-msg"))
                ])),
                cls="data-table doc-lines",
            ),
        )

    # --- Totals ---
    # Compute gross (pre-discount) and net (post-discount) subtotals
    def _li_gross(li: dict) -> float:
        return float(li.get("quantity", 0) or 0) * float(li.get("unit_price", 0) or 0)

    def _li_discounted(li: dict) -> float:
        gross = _li_gross(li)
        dpct = float(li.get("discount_pct") or 0)
        return gross * (1 - dpct / 100) if dpct else gross

    gross_subtotal = sum(_li_gross(li) for li in line_items) if line_items else 0.0
    subtotal = float(subtotal_value or 0) or sum(_li_discounted(li) for li in line_items)
    line_discount = gross_subtotal - subtotal  # total discount from line-level discount_pct
    tax_amount = float(tax_value or 0)
    total_amount = float(total_value or 0) or (subtotal + tax_amount)
    discount = float(discount_value or 0)

    # Build per-code tax rows: prefer `taxes` list on line items, fall back to tax_rate
    doc_taxes = doc.get("doc_taxes") or []
    code_totals: dict[str, dict] = {}  # key → {label, amount}

    if doc_taxes:
        # doc_taxes already have computed amounts (server-side)
        for dtax in doc_taxes:
            code = dtax.get("code", "Tax")
            amt = float(dtax.get("amount", 0) or 0)
            if code not in code_totals:
                code_totals[code] = {"label": code, "amount": 0.0}
            code_totals[code]["amount"] += amt
    elif line_items:
        for li in line_items:
            li_total = _li_discounted(li)
            li_taxes = li.get("taxes") or []
            if li_taxes:
                for item in li_taxes:
                    code = item.get("code") or ""
                    rate = float(item.get("rate", 0) or 0)
                    custom_label = item.get("label") or ""
                    amt = float(item.get("amount", 0) or 0) or round(li_total * rate / 100, 2)
                    key = code or f"custom_{rate}"
                    label = f"{code} ({rate}%)" if code else f"{custom_label or 'Tax'} ({rate}%)"
                    if key not in code_totals:
                        code_totals[key] = {"label": label, "amount": 0.0}
                    code_totals[key]["amount"] += amt
            else:
                rate = float(li.get("tax_rate", 0) or 0)
                if rate != 0:
                    amt = round(li_total * rate / 100, 2)
                    key = f"rate_{rate}"
                    label = f"Tax ({rate}%)"
                    if key not in code_totals:
                        code_totals[key] = {"label": label, "amount": 0.0}
                    code_totals[key]["amount"] += amt

    tax_rows = [
        Div(Span(f"{v['label']}:", cls="total-label"),
            Span(fmt_money(v["amount"], currency), cls="total-value"),
            cls="total-row")
        for v in code_totals.values()
    ]

    if not tax_amount and code_totals:
        tax_amount = sum(v["amount"] for v in code_totals.values())
        total_amount = subtotal - discount + tax_amount

    total_panel = Div(
        Div(Span(t("doc.subtotal"), cls="total-label"),
            Span(fmt_money(gross_subtotal, currency), id="doc-gross-subtotal", cls="total-value"), cls="total-row") if line_discount > 0.005 else "",
        Div(Span(t("doc.discount"), cls="total-label"),
            Span(f"-{fmt_money(line_discount, currency)}", id="doc-line-discount", cls="total-value"), cls="total-row") if line_discount > 0.005 else "",
        Div(Span("Net Subtotal:" if line_discount > 0.005 else "Subtotal:", cls="total-label"),
            Span(fmt_money(subtotal, currency), id="doc-subtotal", cls="total-value"), cls="total-row"),
        Div(Span(t("doc.discount"), cls="total-label"),
            Span(fmt_money(discount, currency), cls="total-value"), cls="total-row") if discount else "",
        Div(*tax_rows, id="doc-tax-rows"),
        Div(Span(t("doc.total"), cls="total-label total-label--final"),
            Span(fmt_money(total_amount, currency), id="doc-total", cls="total-value total-value--final"),
            cls="total-row total-row--final"),
        cls="total-panel",
    )

    contact_label = {
        "invoice": "Bill to", "purchase_order": "Supplier", "quotation": "Quote to",
        "memo": "Receiver", "credit_note": "Issued to", "receipt": "Customer",
        "list": "Customer",
    }.get(doc_type, "Contact")

    # Build contact detail rows - hide payment terms/outstanding for lists
    _contact_rows: list = [
        Div(Span("👤", cls="section-icon"), H3(contact_label, cls="section-title"), cls="section-header"),
        Div(Div(t("doc.contact"), cls="form-label"), _cell("contact_id", contact_value), cls="form-group"),
        Div(Div(t("doc.company"), cls="form-label"), _cell("contact_company_name", doc.get("contact_company_name") or "--"), cls="form-group"),
        Div(Div(t("doc.address"), cls="form-label"), _cell("contact_billing_address", doc.get("contact_billing_address") or doc.get("contact_address")), cls="form-group"),
        Div(Div(t("doc.phone"), cls="form-label"), _cell("contact_phone", doc.get("contact_phone")), cls="form-group"),
        Div(Div(t("doc.email"), cls="form-label"), P(doc.get("contact_email") or "--", cls="meta-value"), cls="form-group"),
        Div(Div(t("doc.tax_id"), cls="form-label"), _cell("contact_tax_id", doc.get("contact_tax_id")), cls="form-group"),
        Hr(cls="section-divider"),
    ]
    if not is_list:
        _contact_rows.append(Div(Div(t("doc.payment_terms"), cls="form-label"), _cell("payment_terms", doc.get("payment_terms")), cls="form-group"))
    _contact_rows.append(Div(Div(t("doc.status"), cls="form-label"), _cell("status", status), *_slot_badges, cls="form-group"))
    if not is_list and outstanding_value is not None:
        _contact_rows.append(Div(Div(t("doc.outstanding"), cls="form-label"), Span(fmt_money(float(outstanding_value or 0), currency), cls="meta-value"), cls="form-group"))

    return Div(
        list_type_selector,
        Div(*action_btns, cls="doc-actions") if action_btns else "",
        po_receive_section,
        # Metadata bar: Doc ID | Reference | Issue date | Due date
        Div(
            Div(Div(t("doc.doc"), cls="meta-label"), _cell("ref_id", ref), cls="meta-cell"),
            Div(Div(t("doc.reference"), cls="meta-label"), _cell("reference", doc.get("reference")), cls="meta-cell"),
            Div(Div(t("doc.issue_date"), cls="meta-label"), _cell("issue_date", issue_date_value), cls="meta-cell"),
            Div(Div(t("doc.due_date"), cls="meta-label"), _cell("due_date", due_date_value), cls="meta-cell") if not is_list else "",
            cls="doc-meta-bar",
        ),
        # Company (left) + Contact/Ship To (right, stacked)
        Div(
            Div(
                Div(Span("🏢", cls="section-icon"), H3(t("page.from"), cls="section-title"), cls="section-header"),
                Div(Div(t("doc.company"), cls="form-label"), _cell("company_name", doc.get("company_name") or "--"), cls="form-group"),
                Div(
                    Div(t("doc.address"), cls="form-label"),
                    _company_address_picker(entity_id, doc.get("company_address") or "", company_locations or []),
                    cls="form-group",
                ),
                Div(Div(t("doc.phone"), cls="form-label"), _cell("company_phone", doc.get("company_phone") or "--"), cls="form-group"),
                Div(Div(t("doc.email"), cls="form-label"), _cell("company_email", doc.get("company_email") or "--"), cls="form-group"),
                Div(Div(t("doc.tax_id"), cls="form-label"), _cell("company_tax_id", doc.get("company_tax_id") or "--"), cls="form-group"),
                cls="doc-section doc-section--half",
            ),
            Div(
                Div(*_contact_rows, cls="doc-section"),
                Div(
                    Div(Span("🚚", cls="section-icon"), H3(t("page.ship_to"), cls="section-title"), cls="section-header"),
                    Div(Div(t("doc.address"), cls="form-label"), _cell("contact_shipping_address", doc.get("contact_shipping_address")), cls="form-group"),
                    Div(Div(t("doc.attn"), cls="form-label"), _cell("shipping_attn", doc.get("shipping_attn")), cls="form-group"),
                    cls="doc-section", style="margin-top:0.75rem",
                ),
                cls="doc-section doc-section--half",
            ),
            cls="doc-row",
        ),
        # Line items + price list bar
        Div(
            lines_section,
            cls="doc-section",
        ),
        # Totals + optional quotation valid-until
        Div(
            Div(Div(t("doc.valid_until"), cls="form-label"), _cell("valid_until", doc.get("valid_until")), cls="form-group") if doc_type == "quotation" else "",
            total_panel,
            cls="doc-section doc-section--totals",
        ),
        # Payment section (invoices, bills, credit notes - not drafts/voids)
        _payment_section(doc, bank_accounts=bank_accounts),
        # Term & Conditions + Note to customer (2 columns)
        Div(
            Div(
                Div(Span("📄", cls="section-icon"), H3(t("page.terms_conditions"), cls="section-title"), cls="section-header"),
                *(_tc_dropdown(entity_id, doc, tc_templates or [], doc_type, is_draft) if is_draft and not is_list else [
                    Div(Div(t("doc.template"), cls="form-label"), P(doc.get("terms_template") or "--", cls="meta-value"), cls="form-group"),
                ]),
                Div(
                    Div(t("doc.terms_text"), cls="form-label"),
                    Textarea(doc.get("terms_text") or "", name="terms_text", rows="4",
                             placeholder="Terms & conditions text", cls="form-input",
                             hx_post=f"{_base}/field/terms_text",
                             hx_trigger="blur", hx_swap="none") if is_draft
                    else Div(doc.get("terms_text") or "--", cls="meta-value"),
                    cls="form-group",
                ),
                cls="doc-section doc-section--half",
            ),
            Div(
                Div(Span("💬", cls="section-icon"), H3(t("page.note_to_customer"), cls="section-title"), cls="section-header"),
                Div(
                    Textarea(doc.get("customer_note") or "", name="customer_note", rows="4",
                             placeholder="Add a note to your customer", cls="form-input",
                             hx_post=f"{_base}/field/customer_note",
                             hx_trigger="blur", hx_swap="none") if is_draft
                    else Div(doc.get("customer_note") or "-", cls="meta-value"),
                    cls="form-group",
                ),
                cls="doc-section doc-section--half",
            ),
            cls="doc-row",
        ),
        # Internal information
        Details(
            Summary(
                H2(t("page.additional_internal_information"), cls="internal-section-title"),
                P(t("doc.this_will_not_be_seen_by_your_clients"), cls="internal-section-sub"),
            ),
            Div(
                Div(
                    Div(Span("📝", cls="section-icon"), H3(t("page.internal_notes"), cls="section-title"), cls="section-header"),
                    _internal_notes_section(entity_id, doc, is_list, tz),
                    cls="doc-section doc-section--half",
                ),
                Div(
                    Div(Span("🤝", cls="section-icon"), H3(t("page.sales_commissions"), cls="section-title"), cls="section-header"),
                    P(t("doc.commission_agent_and_fee_for_this_document_agent_m"), cls="section-hint"),
                    Div(Div(t("doc.commission_agent"), cls="form-label"), _cell("commission_contact_id", _resolve_contact_display(doc, "commission_contact_id")), cls="form-group"),
                    Div(Div(t("doc.commission"), cls="form-label"), _cell("commission_rate_pct", doc.get("commission_rate_pct")), cls="form-group"),
                    cls="doc-section doc-section--half",
                ),
                cls="doc-row",
            ),
            cls="doc-internal",
        ),
        # --- History / Activity section ---
        _doc_history_section(ledger or []),
        cls="doc-detail doc-detail--gc",
    )



def _internal_notes_section(entity_id: str, doc: dict, is_list: bool, tz: str = "UTC") -> FT:
    """Render append-only internal notes timeline + add-note form."""
    from datetime import datetime, timezone as _tz
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            _zone = ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            _zone = _tz.utc
    except ImportError:
        _zone = _tz.utc

    def _fmt_ts(iso: str) -> str:
        if not iso:
            return ""
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt.astimezone(_zone).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return iso[:16].replace("T", " ")

    _base = f"/lists/{entity_id}" if is_list else f"/docs/{entity_id}"

    # Collect all notes: legacy string first (oldest), then structured list (oldest→newest displayed newest first)
    all_notes: list[dict] = []
    legacy = doc.get("internal_note") or ""
    if legacy:
        all_notes.append({"text": legacy, "created_at": "", "created_by": ""})
    all_notes.extend(doc.get("internal_notes") or [])
    # Newest first
    all_notes = list(reversed(all_notes))

    timeline_items = []
    for n in all_notes:
        text = n.get("text") or ""
        ts_display = _fmt_ts(n.get("created_at") or "")
        author = n.get("created_by") or ""
        timeline_items.append(
            Div(
                Div(
                    Small(ts_display, cls="note-timestamp") if ts_display else "",
                    Span(f" · {author}", cls="note-author-name") if author else "",
                    cls="note-meta",
                ),
                P(text, cls="note-text"),
                cls="note-item",
            )
        )

    note_input_id = f"note-input-{entity_id}"
    form_id = f"note-form-{entity_id}"
    add_btn_id = f"note-add-btn-{entity_id}"

    add_form = Form(
        Textarea(
            name="text", placeholder="Write a note...", rows="3", cls="form-input",
            id=note_input_id,
            style="display:none;width:100%;",
            **{
                "onkeydown": f"if(event.key==='Escape'){{document.getElementById('{note_input_id}').style.display='none';document.getElementById('{add_btn_id}').style.display='';document.getElementById('save-btn-{entity_id}').style.display='none';}}",
            },
        ),
        Div(
            Button(t("btn.save_note"), type="submit", cls="btn btn--primary btn--sm",
                style="display:none;", id=f"save-btn-{entity_id}",
            ),
            style="margin-top:0.4rem;",
        ),
        Button(t("btn._add_note"), type="button", cls="btn btn--ghost btn--sm", id=add_btn_id,
            onclick=f"document.getElementById('{note_input_id}').style.display='';document.getElementById('{note_input_id}').focus();document.getElementById('save-btn-{entity_id}').style.display='';this.style.display='none';",
        ),
        hx_post=f"{_base}/notes",
        hx_target=f"#{form_id}",
        hx_swap="outerHTML",
        id=form_id,
    )

    return Div(
        add_form,
        Div(*timeline_items, cls="notes-timeline") if timeline_items else P(t("label.no_notes_yet"), cls="meta-value"),
        cls="form-group",
    )


def _doc_history_section(ledger: list[dict]) -> FT:
    """Render a timeline of ledger events for a document."""
    return activity_table(
        ledger,
        title="History",
        section_cls="doc-section",
        icon="\U0001f4dc",
        empty_msg="No activity recorded yet.",
        max_display=50,
    )


def _doc_status_cards(docs: list[dict], active_status: str, summary: dict | None = None, currency: str | None = None, doc_type: str = "", lang: str = "en") -> FT:
    """Aggregate counts/totals per status and render status_cards.

    Card definitions are doc-type-aware: only statuses valid for the
    document lifecycle are shown.
    """
    # Per-doc-type card definitions: (status_key, label, color)
    _CARDS_BY_TYPE: dict[str, list[tuple[str, str, str]]] = {
        "invoice": [
            ("draft", t("status.pro_forma", lang), "gray"),
            ("sent", t("doc.sent", lang), "blue"),
            ("awaiting_payment", t("status.awaiting_payment", lang), "yellow"),
            ("paid", t("label.paid", lang), "green"),
            ("overdue", t("status.overdue", lang), "red"),
            ("void", t("btn.void", lang), "gray"),
        ],
        "purchase_order": [
            ("draft", t("status.purchase_order", lang), "gray"),
            ("sent", t("doc.sent", lang), "blue"),
            ("void", t("btn.void", lang), "gray"),
        ],
        "bill": [
            ("awaiting_payment", t("status.awaiting_payment", lang), "yellow"),
            ("paid", t("label.paid", lang), "green"),
            ("void", t("btn.void", lang), "gray"),
        ],
        "credit_note": [
            ("draft", t("status.draft", lang), "gray"),
            ("sent", t("doc.sent", lang), "blue"),
            ("void", t("btn.void", lang), "gray"),
        ],
        "memo": [
            ("draft", t("status.draft", lang), "gray"),
            ("final", t("status.issued", lang), "blue"),
            ("converted", t("status.converted", lang), "green"),
            ("void", t("btn.void", lang), "gray"),
        ],
        "consignment_in": [
            ("draft", t("status.draft", lang), "gray"),
            ("final", t("status.issued", lang), "blue"),
            ("converted", t("status.converted", lang), "green"),
            ("void", t("btn.void", lang), "gray"),
        ],
        "receipt": [
            ("draft", t("status.draft", lang), "gray"),
            ("sent", t("doc.sent", lang), "blue"),
            ("void", t("btn.void", lang), "gray"),
        ],
        "list": [
            ("draft", t("status.draft", lang), "gray"),
            ("sent", t("doc.sent", lang), "blue"),
            ("accepted", t("status.accepted", lang), "yellow"),
            ("completed", t("status.completed", lang), "green"),
            ("void", t("btn.void", lang), "gray"),
        ],
    }
    _DEFAULT_CARDS = [
        ("draft", t("status.draft", lang), "gray"),
        ("awaiting_payment", t("status.awaiting_payment", lang), "yellow"),
        ("paid", t("label.paid", lang), "green"),
        ("overdue", t("status.overdue", lang), "red"),
        ("void", t("btn.void", lang), "gray"),
    ]
    card_defs = _CARDS_BY_TYPE.get(doc_type, _DEFAULT_CARDS)

    # Use API-level counts from summary when available (full dataset, not just current page)
    api_counts = (summary or {}).get("count_by_status", {})
    counts: dict[str, int] = {s: api_counts.get(s, 0) for s, _, _ in card_defs}
    totals: dict[str, float] = {s: 0.0 for s, _, _ in card_defs}
    for d in docs:
        s = str(d.get("status") or "").lower()
        if not api_counts and s in counts:
            counts[s] += 1
        if s in totals:
            amt = d.get("total_amount") if d.get("total_amount") is not None else d.get("total")
            try:
                totals[s] += float(amt or 0)
            except (ValueError, TypeError):
                pass
    cards = [
        {"label": label, "count": counts[s], "total": totals[s], "status": s, "color": color}
        for s, label, color in card_defs
    ]
    base_url = f"/docs?type={doc_type}" if doc_type else "/docs"
    return status_cards(cards, base_url, active_status or None, currency=currency)


def _summary_bar(summary: dict, doc_type: str = "", currency: str | None = None, lang: str = "en") -> FT:
    # Only show invoice-specific metrics when viewing invoices or all types
    if doc_type and doc_type != "invoice":
        count = summary.get(f"{doc_type}_count", summary.get("total_count", 0))
        return Div(
            Span(f"{doc_type.replace('_', ' ').title()}s: {count}", cls="val-chip"),
            cls="valuation-bar",
        )
    return Div(
        Span(f"{t('chip.ar', lang)}: {fmt_money(float(summary.get('ar_outstanding', 0) or 0), currency)}", cls="val-chip val-chip--alert"),
        Span(f"{t('chip.billed', lang)}: {fmt_money(float(summary.get('ar_total', 0) or 0), currency)}", cls="val-chip"),
        Span(f"{t('chip.invoices', lang)}: {summary.get('invoice_count', 0)}", cls="val-chip"),
        cls="valuation-bar",
    )


def _drafts_tab(draft_count: int, is_active: bool, doc_type: str = "", status: str = "", lang: str = "en") -> FT:
    """Drafts pill - visible when drafts exist, active when in drafts view."""
    if status == "draft":
        return Span()
    type_param = f"&type={doc_type}" if doc_type else ""
    href = f"/docs?view=drafts{type_param}"
    # Invoice drafts are called "Pro Forma" since they use proforma numbering
    label = t("status.pro_forma", lang) if doc_type == "invoice" else t("status.drafts", lang)
    if is_active:
        return A(
            f"{label} ({draft_count})",
            href="/docs" + (f"?type={doc_type}" if doc_type else ""),
            cls="drafts-tab drafts-tab--active",
            title=f"Viewing {label.lower()} - click to return to live documents",
        )
    if draft_count == 0:
        return Span()
    return A(
        f"{label} ({draft_count})",
        href=href,
        cls="drafts-tab",
        title="Click to view draft documents",
    )



# ---------------------------------------------------------------------------
# List listing-page helpers (kept for /lists table view)
# ---------------------------------------------------------------------------

def _list_table(lists: list[dict], lang: str = "en") -> FT:
    if not lists:
        return Div(
            empty_state_cta(t("label.no_lists_yet", lang), t("btn.new_list", lang), "/lists/create-blank", hx_post=True),
            id="list-table",
        )

    def _row(d: dict) -> FT:
        eid = d.get("entity_id") or d.get("id", "")
        ref = d.get("ref_id") or eid
        items = d.get("line_items", [])
        weight = sum(float(li.get("weight_ct") or li.get("weight") or 0) for li in items)
        return Tr(
            Td(A(ref, href=f"/lists/{eid}", cls="table-link")),
            Td(format_value(d.get("list_type"), "badge")),
            Td(format_value(d.get("customer_name") or d.get("receiver") or d.get("customer_id"))),
            Td(format_value(d.get("created_at") or d.get("date"), "date")),
            Td(str(len(items)), cls="cell--number"),
            Td(f"{weight:.2f}" if weight else EMPTY, cls="cell--number"),
            Td(format_value(d.get("total"), "money"), cls="cell--number"),
            Td(format_value(d.get("status"), "badge")),
            cls="data-row",
        )

    return Table(
        Thead(Tr(
            Th(t("th.ref")), Th(t("th.doc_type")), Th(t("th.customer")),
            Th(t("label.issue_date")), Th(t("th.items")), Th(t("th.weight")), Th(t("label.amount")),
            Th(t("th.status")),
        )),
        Tbody(*[_row(d) for d in lists]),
        cls="data-table",
        id="list-table",
    )


def _list_status_cards(summary: dict, active_status: str = "") -> FT:
    _CARD_DEFS = [
        ("draft", "Draft", "gray"),
        ("sent", "Sent", "blue"),
        ("accepted", "Accepted", "yellow"),
        ("completed", "Completed", "green"),
        ("void", "Void", "gray"),
    ]
    count_by_status = summary.get("count_by_status", {})
    all_total = summary.get("total_count", sum(count_by_status.values()))
    cards = [
        {"label": label, "count": count_by_status.get(s, 0), "total": None, "status": s, "color": color}
        for s, label, color in _CARD_DEFS
    ]
    return status_cards(cards, "/lists", active_status or None, total_override=all_total)


def _list_type_tabs(active: str) -> FT:
    all_cls = "category-tab" + (" category-tab--active" if not active else "")
    tabs = [A(t("doc.all"), href="/lists", hx_get="/lists/search", hx_target="#list-table",
               hx_swap="outerHTML", hx_push_url="/lists", cls=all_cls)]
    for lt in _LIST_TYPES:
        label = lt.replace("_", " ").title()
        cls = "category-tab" + (" category-tab--active" if lt == active else "")
        tabs.append(A(
            label,
            href=f"/lists?type={lt}",
            hx_get=f"/lists/search?type={lt}",
            hx_target="#list-table",
            hx_swap="outerHTML",
            hx_push_url=f"/lists?type={lt}",
            cls=cls,
        ))
    return Div(*tabs, cls="category-tabs", id="type-tabs")


def _list_drafts_tab(draft_count: int, is_active: bool, list_type: str = "") -> FT:
    type_param = f"&type={list_type}" if list_type else ""
    if is_active:
        return A(f"Drafts ({draft_count})", href="/lists" + (f"?type={list_type}" if list_type else ""),
                 cls="drafts-tab drafts-tab--active", title="Viewing drafts - click to return")
    if draft_count == 0:
        return Span()
    return A(f"Drafts ({draft_count})", href=f"/lists?view=drafts{type_param}", cls="drafts-tab")
