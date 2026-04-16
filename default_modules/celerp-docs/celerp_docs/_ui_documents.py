# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from urllib.parse import urlencode

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import search_bar, EMPTY, pagination, searchable_select, breadcrumbs, status_cards, empty_state_cta, format_value as _val
from ui.config import get_token as _token
from ui.routes.reports import _date_filter_bar, _parse_dates
from ui.components.table import fmt_money
from ui.routes.documents import _doc_status_cards, _summary_bar, _drafts_tab

from celerp.modules.slots import get as _get_slots


def _get_nav_slots() -> list[dict]:
    return _get_slots("nav")

_PER_PAGE = 50
_PER_PAGE_OPTIONS = [25, 50, 100, 250]
_DOC_TYPES = ["invoice", "purchase_order", "receipt", "credit_note", "memo"]
_DOC_STATUSES = ["draft", "sent", "paid", "overdue", "void", "open", "awaiting_payment", "converted", "expired"]




from datetime import date as _date, timedelta as _timedelta
from ui.routes.documents import _calculate_due_date
from ui.i18n import t, get_lang


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
        from ui.routes.reports import _parse_dates as _pd, _resolve_preset
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
            date_from, date_to, preset = _pd(request)
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
            summary = await api.get_doc_summary(token)
        except (APIError, Exception) as e:
            if getattr(e, 'status', None) == 401:
                return RedirectResponse("/login", status_code=302)
            docs, summary, draft_count = [], {}, 0

        extra = f"&q={q}&type={doc_type}&status={status}&view={view}".strip("&")
        total_count = summary.get("total_count", len(docs))
        return base_shell(
            page_header(
                "Documents",
                _drafts_tab(draft_count, is_drafts_view, doc_type),
                search_bar(
                    placeholder="Search doc number, contact...",
                    target="#doc-table",
                    url="/docs/search",
                ),
                Button(t("page.new_invoice"),
                    hx_post="/docs/create-blank?type=invoice",
                    hx_swap="none",
                    cls="btn btn--primary",
                ),
                A(t("btn.export_csv"), href="/docs/export/csv", cls="btn btn--secondary"),
                A(t("doc.import_csv"), href="/docs/import", cls="btn btn--secondary"),
            ),
            *([] if is_drafts_view else [
                _date_filter_bar("/docs", date_from, date_to, preset, extra_params=f"&{extra}" if extra else ""),
            ]),
            _summary_bar(summary, doc_type, currency),
            _doc_status_cards(docs, status, summary, currency),
            _doc_table(
                docs,
                sort=sort,
                sort_dir=sort_dir,
                base_params={"q": q, "type": doc_type, "status": status, "view": view, "page": str(page), "per_page": str(per_page)},
                doc_type=doc_type if not is_drafts_view else doc_type,
            ),
            pagination(page, total_count, per_page, "/docs", f"q={q}&type={doc_type}&status={status}&view={view}&sort={sort}&dir={sort_dir}".strip("&")),
            title="Documents - Celerp",
            nav_active={"invoice": "invoices", "memo": "memos", "purchase_order": "purchase-orders"}.get(doc_type, "invoices"),
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
        except (APIError, Exception) as e:
            docs = []
        return _doc_table(
            docs,
            sort=sort,
            sort_dir=sort_dir,
            base_params={"q": q, "type": doc_type, "status": status, "page": str(page)},
            doc_type=doc_type,
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
        except (APIError, Exception) as e:
            if getattr(e, 'status', None) == 401:
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
        except (APIError, Exception) as e:
            if getattr(e, 'status', None) == 401:
                from starlette.responses import Response as _R
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            from starlette.responses import Response as _R
            return _R("", status_code=500)
        from starlette.responses import Response as _R
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

    @app.get("/docs/{entity_id}")
    async def doc_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            doc = await api.get_doc(token, entity_id)
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and getattr(e, 'status', None) == 401:
                return RedirectResponse("/login", status_code=302)
            doc = {}

        # Inject company fields so "My company info" box is populated
        if not doc.get("company_name"):
            try:
                company = await api.get_company(token)
                doc = {
                    **doc,
                    "company_name": company.get("name") or "",
                    "company_address": company.get("address") or "",
                    "company_phone": company.get("phone") or "",
                    "company_tax_id": company.get("tax_id") or "",
                }
            except Exception:
                pass

        doc_ref = doc.get("ref_id") or doc.get("doc_number") or doc.get("ref") or doc.get("external_id") or "Document"
        doc_type_label = doc.get("doc_type", "").replace("_", " ").title() or "Document"
        status = doc.get("status", "draft")
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Documents", "/docs"), (doc_ref, None)]),
            page_header(
                f"{doc_type_label} - {doc_ref}",
                Span(_val(status, "badge"), cls="page-header-badge"),
                A(t("label.back"), href="/docs", cls="btn btn--secondary"),
                A("PDF", href=f"/docs/{entity_id}/pdf", target="_blank", cls="btn btn--secondary"),
            ),
            _doc_detail(doc),
            title=f"{doc_ref} - Celerp",
            nav_active="docs",
            request=request,
        )

    @app.get("/docs/{entity_id}/pdf")
    async def doc_pdf_proxy(request: Request, entity_id: str):
        """Proxy PDF generation from the API app so the browser can access it on the UI port."""
        from starlette.responses import Response as StarletteResponse
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            import httpx
            from ui.config import API_BASE
            async with httpx.AsyncClient(base_url=API_BASE, headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as c:
                r = await c.get(f"/api/docs/{entity_id}/pdf")
                if r.status_code != 200:
                    return StarletteResponse(content=f"PDF generation failed ({r.status_code})", status_code=r.status_code)
                return StarletteResponse(
                    content=r.content,
                    media_type=r.headers.get("content-type", "application/pdf"),
                    headers={"Content-Disposition": r.headers.get("content-disposition", f'inline; filename="{entity_id}.pdf"')},
                )
        except Exception as e:
            return StarletteResponse(content=f"PDF error: {e}", status_code=500)

    @app.get("/docs/catalog-lookup")
    async def doc_catalog_lookup(request: Request):
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        sku = request.query_params.get("sku", "").strip()
        if not sku:
            return JSONResponse({})
        def _extract(item: dict) -> dict:
            return {
                "sku": item.get("sku") or "",
                "description": item.get("name") or item.get("description") or "",
                "unit_price": item.get("sale_price") or item.get("price") or item.get("unit_price") or 0,
                "sell_by": item.get("sell_by") or None,
                "weight": item.get("weight") or None,
                "weight_unit": item.get("weight_unit") or None,
            }

        try:
            resp = await api.list_items(token, {"sku": sku, "limit": 1})
            items = resp.get("items", []) if isinstance(resp, dict) else resp
            if items:
                return JSONResponse(_extract(items[0]))
        except Exception:
            pass
        return JSONResponse({})

    @app.get("/docs/{entity_id}/field/{field}/display")
    async def doc_field_display(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            doc = await api.get_doc(token, entity_id)
        except (APIError, Exception) as e:
            return P(f"Error: {getattr(e, 'detail', str(e))}", cls="cell-error")
        return _doc_display_cell(entity_id, field, doc.get(field))

    @app.get("/docs/{entity_id}/field/{field}/edit")
    async def doc_field_edit(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            doc = await api.get_doc(token, entity_id)
        except (APIError, Exception) as e:
            return P(f"Error: {getattr(e, 'detail', str(e))}", cls="cell-error")
        value = str(doc.get(field, "") or "")
        restore_url = f"/docs/{entity_id}/field/{field}/display"
        esc_js = f"if(event.key==='Escape'){{htmx.ajax('GET','{restore_url}',{{target:this.closest('.editable-cell'),swap:'outerHTML'}});event.preventDefault();}}"
        blur_restore = f"htmx.ajax('GET','{restore_url}',{{target:this.closest('.editable-cell'),swap:'outerHTML'}})"

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
            input_el = Input(
                type="date", name="value", value=value[:10] if value else "",
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js,
                onblur=f"if(!this.value.trim() && !this.dataset.dirty){{{blur_restore}}}",
                oninput="this.dataset.dirty='1'",
                data_orig=value,
            )
        elif field in ("contact_id", "commission_contact_id"):
            # Searchable contact picker
            try:
                contact_resp = await api.list_contacts(token, {"limit": 500})
                contacts = contact_resp.get("items", [])
            except (APIError, Exception) as e:
                contacts = []
            contact_opts = [(c.get("entity_id", ""), c.get("name", c.get("entity_id", ""))) for c in contacts]
            contact_opts.append(("__new__", "+ Add new contact"))
            patch_url = f"/docs/{entity_id}/field/{field}"
            input_el = searchable_select(
                name="value",
                options=contact_opts,
                value=value,
                placeholder="Search contacts...",
                hx_patch=patch_url,
                hx_target="closest .editable-cell",
                hx_swap="outerHTML",
                hx_trigger="change",
            )
        else:
            input_el = Input(
                type="text", name="value", value=value,
                hx_patch=f"/docs/{entity_id}/field/{field}",
                hx_target="closest .editable-cell", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms", cls="cell-input", autofocus=True,
                onkeydown=esc_js,
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
            return _R("", status_code=204, headers={"HX-Redirect": "/crm/new"})
        try:
            patch = {field: value}
            # Auto-populate payment_terms from contact when contact_id changes
            if field == "contact_id" and value:
                try:
                    contact = await api.get_contact(token, value)
                    contact_pt = contact.get("payment_terms")
                    if contact_pt:
                        patch["payment_terms"] = contact_pt
                        doc_pre = await api.get_doc(token, entity_id)
                        terms_list = await api.get_payment_terms(token)
                        new_due = _calculate_due_date(doc_pre.get("issue_date"), contact_pt, terms_list)
                        if new_due:
                            patch["due_date"] = new_due
                except APIError:
                    pass
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
            await api.patch_doc(token, entity_id, patch)
            doc = await api.get_doc(token, entity_id)
        except (APIError, Exception) as e:
            return P(str(getattr(e, 'detail', str(e))), cls="cell-error")
        return _doc_display_cell(entity_id, field, doc.get(field))

    @app.post("/docs/{entity_id}/field/{field}")
    async def doc_field_post(request: Request, entity_id: str, field: str):
        """Handle autosave of text fields (customer_note, internal_note) via hx_post."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401)
        form = await request.form()
        value = str(form.get(field, form.get("value", "")))
        try:
            await api.patch_doc(token, entity_id, {field: value})
        except APIError:
            pass  # silent autosave failure
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
        except (APIError, Exception) as e:
            return JSONResponse({"error": str(getattr(e, 'detail', str(e)))}, status_code=400)
        return JSONResponse({"ok": True})

    # T3: Document actions (finalize, void, send)
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
                await api.send_doc(token, entity_id)
            elif action == "void":
                reason = str(form.get("reason", "")).strip() or None
                await api.void_doc(token, entity_id, reason)
            else:
                return _R("", status_code=400)
        except (APIError, Exception) as e:
            if getattr(e, 'status', None) == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            # Return error inline
            return Div(
                Span(str(getattr(e, 'detail', str(e))), cls="flash flash--error"),
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
            await api.record_payment(token, entity_id, {
                "amount": amount,
                "method": method,
                "reference": reference,
            })
        except (APIError, Exception) as e:
            status_code = getattr(e, "status", None)
            if status_code == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            detail = getattr(e, "detail", str(e))
            return Div(Span(str(detail), cls="flash flash--error"), id="payment-error")
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
        except (APIError, Exception) as e:
            if getattr(e, 'status', None) == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(getattr(e, 'detail', str(e))), cls="flash flash--error"), id="action-error")
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
            location_id = str(form.get("location_id", "")).strip()
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
        except (APIError, Exception) as e:
            if getattr(e, 'status', None) == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(getattr(e, 'detail', str(e))), cls="flash flash--error"), id="action-error")
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
        except (APIError, Exception) as e:
            if getattr(e, 'status', None) == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return Div(Span(str(getattr(e, 'detail', str(e))), cls="flash flash--error"), id="refund-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{entity_id}"})

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
        except (APIError, Exception) as e:
            return Span(str(getattr(e, 'detail', str(e))), cls="flash flash--error")


def _doc_table(
    docs: list[dict],
    sort: str = "date",
    sort_dir: str = "desc",
    base_params: dict[str, str] | None = None,
    doc_type: str = "",
) -> FT:
    if not docs:
        dt_label = doc_type.replace("_", " ").title() if doc_type else "Invoice"
        dt_slug = doc_type if doc_type else "invoice"
        return Div(
            empty_state_cta(f"No {dt_label.lower()}s yet.", f"Create {dt_label}", f"/docs/create-blank?type={dt_slug}", hx_post=True),
            id="doc-table",
        )

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
        eid = d.get("entity_id", "")
        # Imported GemCloud docs often use ref/contact_external_id/created_at/total/amount_outstanding.
        doc_number = d.get("doc_number") or d.get("ref") or d.get("ref_id") or eid
        contact = d.get("contact_name") or d.get("contact_id") or d.get("contact_external_id")
        issue_date = d.get("issue_date") or d.get("created_at")
        due_date = d.get("due_date") or d.get("payment_due_date")
        total_amount = d.get("total_amount") if d.get("total_amount") is not None else d.get("total")
        outstanding_amount = d.get("outstanding_balance") if d.get("outstanding_balance") is not None else d.get("amount_outstanding")
        outstanding = float(outstanding_amount or 0)
        return Tr(
            Td(A(doc_number or EMPTY, href=f"/docs/{eid}", cls="table-link")),
            Td(_val(d.get("doc_type"), "badge")),
            Td(_val(contact)),
            Td(_val(issue_date, "date")),
            Td(_val(due_date, "date")),
            Td(_val(total_amount, "money"), cls="cell--number"),
            Td(
                _val(outstanding_amount, "money"),
                cls=f"cell--number {'cell--alert' if outstanding > 0 and d.get('doc_type') == 'invoice' else ''}",
            ),
            Td(_val(d.get("status"), "badge")),
            id=f"doc-{eid}",
            cls="data-row",
        )

    return Table(
        Thead(Tr(
            _th("Number", "number"), _th("Type", "type"), _th("Contact", "contact"), _th("Date", "date"), _th("Due", "due"),
            _th("Total", "total"), _th("Outstanding", "outstanding"), _th("Status", "status"),
        )),
        Tbody(*[_row(d) for d in docs]),
        cls="data-table",
        id="doc-table",
    )


def _doc_display_cell(entity_id: str, field: str, value) -> FT:
    return Div(
        _val(value, "badge" if field in {"status", "purchase_kind"} else ("money" if field in {"total_amount", "tax_amount", "outstanding_balance"} else "date" if field in {"issue_date", "due_date"} else "text")),
        hx_get=f"/docs/{entity_id}/field/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        title="Click to edit",
        cls="editable-cell",
    )


def _doc_detail(doc: dict) -> FT:
    def _pick(*keys: str):
        for k in keys:
            if k in doc and doc.get(k) is not None:
                return doc.get(k)
        return None

    line_items = doc.get("line_items", [])
    entity_id = doc.get("entity_id", "")
    status = doc.get("status", "draft")
    doc_type = doc.get("doc_type", "")
    is_draft = status == "draft"
    ref = _pick("ref_id", "doc_number", "ref", "external_id") or entity_id

    contact_value = _pick("contact_name", "contact_id", "contact_external_id")
    issue_date_value = _pick("issue_date", "created_at")
    due_date_value = _pick("due_date", "payment_due_date")
    total_value = _pick("total_amount", "total")
    tax_value = _pick("tax_amount", "tax")
    outstanding_value = _pick("outstanding_balance", "amount_outstanding")
    subtotal_value = _pick("subtotal")
    discount_value = _pick("discount_amount") or 0
    currency = doc.get("currency") or "USD"

    # --- Action buttons ---
    action_btns = []
    if doc_type == "invoice" and status in ("sent", "final", "partial", "awaiting_payment"):
        outstanding = float(outstanding_value or 0)
        action_btns.append(
            Details(
                Summary(t("doc.receive_payment"), cls="btn btn--primary"),
                Form(
                    Div(Label(t("label.amount"), cls="form-label"),
                        Input(type="number", name="amount", value=str(outstanding), step="0.01", min="0", cls="form-input"), cls="form-group"),
                    Div(Label(t("th.date"), cls="form-label"),
                        Input(type="date", name="payment_date", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.method"), cls="form-label"),
                        Select(Option(t("doc.cash"), value="cash"), Option(t("btn.transfer"), value="transfer"),
                               Option(t("doc.card"), value="card"), Option(t("doc.other"), value="other"),
                               name="method", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.reference"), cls="form-label"),
                        Input(type="text", name="reference", cls="form-input"), cls="form-group"),
                    Span("", id="payment-error"),
                    Button(t("btn.save"), type="submit", cls="btn btn--primary"),
                    hx_post=f"/docs/{entity_id}/payment", hx_swap="none", cls="form-card",
                ),
                cls="payment-section",
            )
        )
    if doc_type == "quotation" and status not in ("void", "converted"):
        action_btns.append(
            Button(t("btn.convert"), hx_post=f"/docs/{entity_id}/convert",
                   hx_swap="none", cls="btn btn--primary")
        )
    if status in ("draft", "sent"):
        action_btns.append(
            Button(t("btn.finalize"), hx_post=f"/docs/{entity_id}/action/finalize",
                   hx_swap="none", cls="btn btn--primary")
        )
    if status == "draft":
        action_btns.append(
            Button(t("btn.send"), hx_post=f"/docs/{entity_id}/action/send",
                   hx_swap="none", cls="btn btn--secondary")
        )
    if status != "void":
        action_btns.append(
            Details(
                Summary(t("btn.void"), cls="btn btn--danger"),
                Form(
                    Input(type="text", name="reason", placeholder="Void reason...", cls="form-input form-input--inline"),
                    Button(t("btn.confirm_void"), type="submit", cls="btn btn--danger"),
                    hx_post=f"/docs/{entity_id}/action/void", hx_swap="none", cls="inline-form",
                ),
                cls="void-section void-section--right",
            )
        )
    amount_paid = float(doc.get("amount_paid") or 0)
    if doc_type == "invoice" and amount_paid > 0:
        action_btns.append(
            Details(
                Summary(t("btn.refund"), cls="btn btn--secondary"),
                Form(
                    Div(Label(t("label.amount"), cls="form-label"),
                        Input(type="number", name="amount", value=str(amount_paid), step="0.01", min="0", max=str(amount_paid), cls="form-input"), cls="form-group"),
                    Div(Label(t("label.method"), cls="form-label"),
                        Input(type="text", name="method", cls="form-input"), cls="form-group"),
                    Div(Label(t("label.reference"), cls="form-label"),
                        Input(type="text", name="reference", cls="form-input"), cls="form-group"),
                    Span("", id="refund-error"),
                    Button(t("btn.submit"), type="submit", cls="btn btn--danger"),
                    hx_post=f"/docs/{entity_id}/refund", hx_swap="none", cls="form-card",
                ),
                cls="refund-section",
            )
        )
    action_btns.append(
        Button(t("btn.share"), hx_post=f"/docs/{entity_id}/share",
               hx_target="#share-result", hx_swap="innerHTML", cls="btn btn--secondary")
    )
    action_btns.append(Span("", id="share-result"))
    action_btns.append(Span("", id="action-error"))

    # --- PO receive ---
    po_receive_section = ""
    if doc_type == "purchase_order" and status in ("finalized", "sent", "final", "draft"):
        po_items = doc.get("line_items", [])
        if po_items:
            receive_rows = [Tr(
                Td(str(li.get("description", "") or li.get("sku", "") or f"Item {i + 1}")),
                Td(str(float(li.get("quantity", 0) or 0))),
                Td(Input(type="hidden", name=f"item_id_{i}", value=li.get("item_id", "") or ""),
                   Input(type="number", name=f"qty_{i}", value=str(float(li.get("quantity", 0) or 0)),
                         step="any", min="0", cls="form-input form-input--sm")),
            ) for i, li in enumerate(po_items)]
        else:
            receive_rows = [Tr(
                Td(Input(type="text", name="sku_0", placeholder="SKU", cls="form-input form-input--sm")),
                Td(Input(type="text", name="name_0", placeholder="Name", cls="form-input form-input--sm")),
                Td(Input(type="number", name="qty_0", value="1", step="any", min="0", cls="form-input form-input--sm")),
            )]
        po_receive_section = Details(
            Summary(t("doc.receive_goods"), cls="btn btn--secondary"),
            Form(
                Table(Thead(Tr(*(([Th(t("th.item")), Th(t("th.ordered")), Th(t("th.qty_received"))] if po_items else [Th("SKU"), Th(t("th.name")), Th(t("th.qty"))])))),
                      Tbody(*receive_rows), cls="data-table data-table--compact"),
                Div(Label(t("th.location"), cls="form-label"),
                    Input(type="text", name="location_id", placeholder="Location ID", cls="form-input"), cls="form-group"),
                Div(Label(t("th.notes"), cls="form-label"),
                    Textarea("", name="notes", rows="2", cls="form-input"), cls="form-group"),
                Span("", id="action-error"),
                Button(t("btn.record_receipt"), type="submit", cls="btn btn--primary"),
                hx_post=f"/docs/{entity_id}/receive", hx_swap="none", cls="form-card",
            ),
            cls="receive-section",
        )

    # --- Line items section ---
    line_body_id = "line-body"
    if is_draft:
        def _li_editable_row(li: dict, idx: int) -> FT:
            qty = li.get("quantity", 0)
            price = li.get("unit_price", 0)
            line_tot = float(qty or 0) * float(price or 0)
            return Tr(
                Td(Input(type="text", value=li.get("sku", "") or "",
                         data_name="sku", cls="cell-input cell-input--sm",
                         onblur="celerpSkuLookup(this)")),
                Td(Input(type="text", value=li.get("description", "") or li.get("name", ""),
                         data_name="description", cls="cell-input cell-input--sm")),
                Td(Input(type="number", value=str(qty), step="any",
                         data_name="quantity", oninput="celerpUpdateTotals()",
                         cls="cell-input cell-input--xs")),
                Td(Input(type="number", value=str(price), step="0.01",
                         data_name="unit_price", oninput="celerpUpdateTotals()",
                         cls="cell-input cell-input--xs")),
                Td(Input(type="number", value=str(li.get("tax_rate", 0) or 0),
                         step="0.01", data_name="tax_rate",
                         cls="cell-input cell-input--xs")),
                Td(Span(fmt_money(line_tot, currency), cls="line-total"), cls="cell--number"),
                Td(Button("x", type="button", cls="btn btn--danger btn--xs",
                          onclick="this.closest('tr').remove(); celerpUpdateTotals();")),
            )

        def _li_empty_row() -> FT:
            return Tr(
                Td(Input(type="text", data_name="sku", placeholder="SKU (optional)",
                         cls="cell-input cell-input--sm",
                         onblur="celerpSkuLookup(this)")),
                Td(Input(type="text", data_name="description", placeholder="Description",
                         cls="cell-input cell-input--sm")),
                Td(Input(type="number", value="1", step="any", data_name="quantity",
                         oninput="celerpUpdateTotals()", cls="cell-input cell-input--xs")),
                Td(Input(type="number", value="0", step="0.01", data_name="unit_price",
                         oninput="celerpUpdateTotals()", cls="cell-input cell-input--xs")),
                Td(Input(type="number", value="0", step="0.01", data_name="tax_rate",
                         cls="cell-input cell-input--xs")),
                Td(Span(fmt_money(0, currency), cls="line-total"), cls="cell--number"),
                Td(Button("x", type="button", cls="btn btn--danger btn--xs",
                          onclick="this.closest('tr').remove(); celerpUpdateTotals();")),
            )

        rows = [_li_editable_row(li, i) for i, li in enumerate(line_items)]
        if not rows:
            rows = [_li_empty_row()]

        # AI dropzone — only on draft bills/expenses when celerp-ai is loaded
        _ai_dropzone: list[FT] = []
        _ai_loaded = any(c.get("key") == "ai" for c in _get_nav_slots())
        if is_draft and doc_type in ("bill", "expense") and _ai_loaded:
            _ai_dropzone = [
                Div(
                    Div("✨", cls="ai-dropzone__icon"),
                    Div("Drop PDF or receipt image here to auto-fill this bill", cls="ai-dropzone__text"),
                    Div(t("doc.powered_by_celerp_ai_operator"), cls="ai-dropzone__sub"),
                    cls="ai-dropzone",
                    data_ai_dropzone="1",
                    onclick="celerpAiDropzoneClick()",
                    title="Auto-fill line items from receipts or invoices",
                ),
                Script("""
(function() {
  window.celerpAiDropzoneClick = function() {
    var el = document.querySelector('[data-ai-dropzone]');
    if (el) {
      el.innerHTML = '✨ <a href="/ai" style="color:inherit;font-weight:600;">Unlock AI auto-fill</a> — Requires Celerp Cloud ($29/mo)';
      el.style.cursor = 'default'; el.onclick = null;
    }
  };
  var dz = document.querySelector('[data-ai-dropzone]');
  if (!dz) return;
  dz.addEventListener('dragover', function(e) { e.preventDefault(); dz.classList.add('ai-dropzone--over'); });
  dz.addEventListener('dragleave', function() { dz.classList.remove('ai-dropzone--over'); });
  dz.addEventListener('drop', function(e) { e.preventDefault(); dz.classList.remove('ai-dropzone--over'); celerpAiDropzoneClick(); });
})();
"""),
            ]

        lines_section = Div(
            *_ai_dropzone,
            Template(_li_empty_row(), id="line-row-tpl"),
            Table(
                Thead(Tr(Th(t("th.skuitem")), Th(t("th.description")), Th(t("th.qty")), Th(t("th.unit_price")),
                         Th(t("th.tax")), Th(t("th.total")), Th(""))),
                Tbody(*rows, id=line_body_id),
                cls="data-table doc-lines",
            ),
            Div(
                Button(t("btn._add_item"), type="button", cls="btn btn--secondary",
                       onclick="celerpAddLine()"),
                Button(t("btn.save_lines"), type="button", cls="btn btn--primary",
                       onclick="celerpSaveLines()"),
                Span("", id="save-status", cls="save-status"),
                cls="line-actions gap-sm",
            ),
            Script(f"""
const _CELERP_EID = {repr(entity_id)};
async function celerpSkuLookup(input) {{
    const sku = input.value.trim();
    if (!sku) return;
    const row = input.closest('tr');
    const resp = await fetch('/docs/catalog-lookup?sku=' + encodeURIComponent(sku));
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.description) {{
        const descEl = row.querySelector('[data-name="description"]');
        if (descEl && !descEl.value) {{
            let desc = data.description;
            if (data.sell_by === 'weight' && data.weight > 0) {{
                desc += ' (' + data.weight + ' ' + (data.weight_unit || 'ct') + ' @ ' + (data.unit_price || 0) + '/' + (data.weight_unit || 'unit') + ')';
            }}
            descEl.value = desc;
        }}
    }}
    if (data.unit_price != null) {{
        const priceEl = row.querySelector('[data-name="unit_price"]');
        if (priceEl) {{ priceEl.value = data.unit_price; }}
    }}
    if (data.sell_by === 'weight' && data.weight > 0) {{
        const qtyEl = row.querySelector('[data-name="quantity"]');
        if (qtyEl) qtyEl.value = data.weight;
    }}
    celerpUpdateTotals();
}}
function celerpAddLine() {{
    const tpl = document.getElementById('line-row-tpl').content.cloneNode(true);
    document.getElementById('{line_body_id}').appendChild(tpl);
    celerpUpdateTotals();
}}
function celerpUpdateTotals() {{
    let sub = 0;
    document.querySelectorAll('#{line_body_id} tr').forEach(row => {{
        const qty = parseFloat(row.querySelector('[data-name="quantity"]')?.value || 0);
        const price = parseFloat(row.querySelector('[data-name="unit_price"]')?.value || 0);
        const tot = qty * price;
        const totalEl = row.querySelector('.line-total');
        if (totalEl) totalEl.textContent = '$' + tot.toLocaleString('en-US', {{minimumFractionDigits: 2}});
        sub += tot;
    }});
    const subEl = document.getElementById('doc-subtotal');
    if (subEl) subEl.textContent = '$' + sub.toLocaleString('en-US', {{minimumFractionDigits: 2}});
    const totEl = document.getElementById('doc-total');
    if (totEl) totEl.textContent = '$' + sub.toLocaleString('en-US', {{minimumFractionDigits: 2}});
}}
async function celerpSaveLines() {{
    const rows = document.querySelectorAll('#{line_body_id} tr');
    const lines = [];
    rows.forEach(row => {{
        const desc = row.querySelector('[data-name="description"]')?.value;
        const sku = row.querySelector('[data-name="sku"]')?.value;
        const qty = parseFloat(row.querySelector('[data-name="quantity"]')?.value || 0);
        const price = parseFloat(row.querySelector('[data-name="unit_price"]')?.value || 0);
        const tax = parseFloat(row.querySelector('[data-name="tax_rate"]')?.value || 0);
        if (desc || sku || qty || price) {{
            lines.push({{description: desc || '', sku: sku || '', quantity: qty,
                         unit_price: price, tax_rate: tax, line_total: qty * price}});
        }}
    }});
    const subtotal = lines.reduce((s, l) => s + l.quantity * l.unit_price, 0);
    const tax = lines.reduce((s, l) => s + l.quantity * l.unit_price * (l.tax_rate / 100), 0);
    const resp = await fetch('/docs/' + _CELERP_EID + '/lines', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{line_items: lines, subtotal, tax, total: subtotal + tax}})
    }});
    const statusEl = document.getElementById('save-status');
    if (resp.ok) {{
        statusEl.textContent = 'Saved';
        setTimeout(() => {{ statusEl.textContent = ''; }}, 2000);
    }} else {{
        const err = await resp.json().catch(() => ({{}}));
        statusEl.textContent = err.error || 'Save failed';
        statusEl.style.color = 'red';
    }}
}}
"""),
            cls="lines-section",
        )
    else:
        def _li_row(li: dict) -> FT:
            qty = float(li.get("quantity", 0) or 0)
            price = float(li.get("unit_price", 0) or 0)
            line_total = float(li.get("line_total", 0) or 0) or qty * price
            return Tr(
                Td(_val(li.get("description") or li.get("name"))),
                Td(_val(li.get("sku") or None)),
                Td(_val(li.get("quantity"))),
                Td(_val(li.get("unit_price"), "money"), cls="cell--number"),
                Td(_val(li.get("tax_rate"))),
                Td(_val(line_total, "money"), cls="cell--number"),
            )
        lines_section = Table(
            Thead(Tr(Th(t("th.description")), Th(t("th.skuitem")), Th(t("th.qty")), Th(t("th.unit_price")), Th(t("th.tax")), Th(t("th.total")))),
            Tbody(*([_li_row(li) for li in line_items] if line_items else [
                Tr(Td(t("doc.no_line_items"), colspan="6", cls="empty-state-msg"))
            ])),
            cls="data-table doc-lines",
        )

    # --- Totals ---
    subtotal = float(subtotal_value or 0) or sum(
        float(li.get("quantity", 0) or 0) * float(li.get("unit_price", 0) or 0) for li in line_items
    )
    tax_amount = float(tax_value or 0)
    total_amount = float(total_value or 0) or (subtotal + tax_amount)
    discount = float(discount_value or 0)

    # Build per-name tax rows from doc_taxes list; fall back to single "Tax" row if absent
    doc_taxes = doc.get("doc_taxes") or []
    if doc_taxes:
        tax_rows = [
            Div(Span(f"{t.get('label') or t.get('code', 'Tax')}:", cls="total-label"),
                Span(fmt_money(float(t.get("amount", 0) or 0), currency), cls="total-value"),
                cls="total-row")
            for dtax in doc_taxes
        ]
    elif tax_amount:
        tax_rows = [Div(Span(t("doc.tax"), cls="total-label"),
                        Span(fmt_money(tax_amount, currency), cls="total-value"), cls="total-row")]
    else:
        tax_rows = []

    total_panel = Div(
        Div(Span(t("doc.subtotal"), cls="total-label"),
            Span(fmt_money(subtotal, currency), id="doc-subtotal", cls="total-value"), cls="total-row"),
        Div(Span(t("doc.discount"), cls="total-label"),
            Span(fmt_money(discount, currency), cls="total-value"), cls="total-row") if discount else "",
        *tax_rows,
        Div(Span(t("doc.total"), cls="total-label total-label--final"),
            Span(fmt_money(total_amount, currency), id="doc-total", cls="total-value total-value--final"),
            cls="total-row total-row--final"),
        cls="total-panel",
    )

    contact_label = {
        "invoice": "Bill to", "purchase_order": "Supplier", "quotation": "Quote to",
        "memo": "Receiver", "credit_note": "Issued to", "receipt": "Customer",
    }.get(doc_type, "Contact")

    return Div(
        Div(*action_btns, cls="doc-actions") if action_btns else "",
        po_receive_section,
        # Metadata bar: Doc ID | Reference | Issue date | Due date
        Div(
            Div(Div(t("doc.doc"), cls="meta-label"), _doc_display_cell(entity_id, "ref_id", ref), cls="meta-cell"),
            Div(Div(t("doc.reference"), cls="meta-label"), _doc_display_cell(entity_id, "reference", doc.get("reference")), cls="meta-cell"),
            Div(Div(t("doc.issue_date"), cls="meta-label"), _doc_display_cell(entity_id, "issue_date", issue_date_value), cls="meta-cell"),
            Div(Div(t("doc.due_date"), cls="meta-label"), _doc_display_cell(entity_id, "due_date", due_date_value), cls="meta-cell"),
            cls="doc-meta-bar",
        ),
        # Company + Contact (2 columns)
        Div(
            Div(
                Div(
                    P(Strong(doc.get("company_name") or "My Company"), cls="invoice-party-name"),
                    *(
                        [P(doc.get("company_address"), cls="invoice-party-line")]
                        if doc.get("company_address") else []
                    ),
                    *(
                        [P(f"Phone: {doc.get('company_phone')}", cls="invoice-party-line")]
                        if doc.get("company_phone") else []
                    ),
                    *(
                        [P(f"Tax ID: {doc.get('company_tax_id')}", cls="invoice-party-line")]
                        if doc.get("company_tax_id") else []
                    ),
                    cls="invoice-party",
                ),
                cls="doc-section doc-section--half",
            ),
            Div(
                Div(Span(t("doc.u0001f464"), cls="section-icon"), H3(contact_label, cls="section-title"), cls="section-header"),
                Div(Div(t("doc.receiver"), cls="form-label"), _doc_display_cell(entity_id, "contact_id", contact_value), cls="form-group"),
                Hr(cls="section-divider"),
                Div(Div(t("doc.payment_terms"), cls="form-label"), _doc_display_cell(entity_id, "payment_terms", doc.get("payment_terms")), cls="form-group"),
                Div(Div(t("doc.status"), cls="form-label"), _doc_display_cell(entity_id, "status", status), cls="form-group"),
                Div(Div(t("doc.outstanding"), cls="form-label"), Span(fmt_money(float(outstanding_value or 0), currency), cls="meta-value"), cls="form-group") if outstanding_value is not None else "",
                cls="doc-section doc-section--half",
            ),
            cls="doc-row",
        ),
        # List items
        Div(
            Div(Span(t("doc.u2630"), cls="section-icon"), H3(t("page.list_items"), cls="section-title"), cls="section-header"),
            Div(Div(Span(t("doc.currency"), cls="meta-label"), _doc_display_cell(entity_id, "currency", currency), cls="currency-bar-item"), cls="currency-bar"),
            lines_section,
            cls="doc-section",
        ),
        # Totals + optional quotation valid-until
        Div(
            Div(Div(t("doc.valid_until"), cls="form-label"), _doc_display_cell(entity_id, "valid_until", doc.get("valid_until")), cls="form-group") if doc_type == "quotation" else "",
            total_panel,
            cls="doc-section doc-section--totals",
        ),
        # Term & Conditions + Note to customer (2 columns)
        Div(
            Div(
                Div(Span(t("doc.u0001f4c4"), cls="section-icon"), H3(t("page.term_conditions"), cls="section-title"), cls="section-header"),
                Div(Div(t("doc.template"), cls="form-label"), _doc_display_cell(entity_id, "terms_template", doc.get("terms_template")), cls="form-group"),
                cls="doc-section doc-section--half",
            ),
            Div(
                Div(Span(t("doc.u0001f4ac"), cls="section-icon"), H3(t("page.note_to_customer"), cls="section-title"), cls="section-header"),
                Div(
                    Textarea(doc.get("customer_note") or "", name="customer_note", rows="4",
                             placeholder="Add a note to your customer", cls="form-input",
                             hx_post=f"/docs/{entity_id}/field/customer_note",
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
                    Div(Span(t("doc.u0001f4dd"), cls="section-icon"), H3(t("page.internal_note"), cls="section-title"), cls="section-header"),
                    Div(
                        Textarea(doc.get("internal_note") or "", name="internal_note", rows="4",
                                 placeholder="Add internal note", cls="form-input",
                                 hx_post=f"/docs/{entity_id}/field/internal_note",
                                 hx_trigger="blur", hx_swap="none") if is_draft
                        else Div(doc.get("internal_note") or "-", cls="meta-value"),
                        cls="form-group",
                    ),
                    cls="doc-section doc-section--half",
                ),
                Div(
                    Div(Span(t("doc.u0001f91d"), cls="section-icon"), H3(t("page.sales_commissions"), cls="section-title"), cls="section-header"),
                    P(t("doc.commission_agent_and_fee_for_this_document_agent_m"), cls="section-hint"),
                    Div(Div(t("doc.commission_agent"), cls="form-label"), _doc_display_cell(entity_id, "commission_contact_id", doc.get("commission_contact_id")), cls="form-group"),
                    Div(Div(t("doc.commission"), cls="form-label"), _doc_display_cell(entity_id, "commission_rate_pct", doc.get("commission_rate_pct")), cls="form-group"),
                    cls="doc-section doc-section--half",
                ),
                cls="doc-row",
            ),
            cls="doc-internal",
        ),
        cls="doc-detail doc-detail--gc",
    )
    
    ai_source_file_id = doc.get("metadata", {}).get("ai_source_file_id")
    if is_draft and doc_type in ("bill", "expense") and ai_source_file_id:
        return Div(
            Div(
                Iframe(src=f"/ai/file/{ai_source_file_id}", style="width:100%; height:100%; min-height: 80vh; border:none; border-radius: var(--radius);"),
                cls="ai-review-left"
            ),
            Div(
                main_content,
                cls="ai-review-right"
            ),
            cls="ai-review-container"
        )
    return main_content




# _new_doc_form removed: docs use create-blank quick flow, edit fields post-creation
