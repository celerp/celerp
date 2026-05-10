# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Subscription template UI routes.

Subscription templates are docs with doc_type "subscription_invoice" or
"subscription_po".  The list page mirrors /docs?type=invoice (status cards,
search, pagination).  The detail page delegates to _doc_detail() from the docs
module; subscription-specific controls (frequency, start date, lifecycle
actions) live in the page_header row, suppressing the standard doc action bar.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import breadcrumbs, pagination, search_bar, status_cards
from ui.config import get_token as _token, get_role as _get_role
from ui.i18n import get_lang, t

logger = logging.getLogger(__name__)

_PER_PAGE = 50

_FREQUENCIES = ["weekly", "biweekly", "monthly", "quarterly", "annually", "custom"]

_STATUS_CSS = {
    "active": "badge--active",
    "paused": "badge--paused",
    "cancelled": "badge--void",
    "draft": "badge--draft",
}

_DIRECTION_DOC_TYPE = {
    "sales": "subscription_invoice",
    "purchasing": "subscription_po",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _status_badge(status: str) -> FT:
    return Span(status.replace("_", " ").capitalize(), cls=f"badge {_STATUS_CSS.get(status, 'badge--draft')}")


def _direction_tabs(direction: str, status: str = "") -> FT:
    def _tab(label: str, dir_: str) -> FT:
        params = f"direction={dir_}"
        if status:
            params += f"&status={status}"
        return A(label, href=f"/subscriptions?{params}",
                 cls=f"tab-link {'tab-link--active' if direction == dir_ else ''}")
    return Nav(_tab("Sales", "sales"), _tab("Purchasing", "purchasing"), cls="tab-nav")


def _sub_status_cards(items: list[dict], active_status: str, direction: str) -> FT:
    counts: dict[str, int] = {}
    for it in items:
        s = it.get("status", "draft")
        counts[s] = counts.get(s, 0) + 1
    base = f"/subscriptions?direction={direction}"
    cards = [
        {"label": "Active",    "count": counts.get("active", 0),    "total": None, "status": "active",    "color": "green",  "_url": f"{base}&status=active"},
        {"label": "Paused",    "count": counts.get("paused", 0),    "total": None, "status": "paused",    "color": "yellow", "_url": f"{base}&status=paused"},
        {"label": "Cancelled", "count": counts.get("cancelled", 0), "total": None, "status": "cancelled", "color": "gray",   "_url": f"{base}&status=cancelled"},
        {"label": "Draft",     "count": counts.get("draft", 0),     "total": None, "status": "draft",     "color": "gray",   "_url": f"{base}&status=draft"},
    ]
    return status_cards(cards, base, active_status or None, currency=None, show_all_card=True)


def _sub_table(subs: list[dict], direction: str) -> FT:
    if not subs:
        return Div(P("No subscription templates found.", cls="text-muted empty-state"), id="sub-table")

    def _row(s: dict) -> FT:
        eid = s.get("id") or s.get("entity_id", "")
        name = s.get("name") or eid
        contact = s.get("contact_name") or s.get("contact_company_name") or "-"
        freq = (s.get("frequency") or "-").capitalize()
        next_run = s.get("next_run_date") or "-"
        status = s.get("status", "draft")
        return Tr(
            Td(A(name, href=f"/subscriptions/{eid}")),
            Td(contact),
            Td(freq),
            Td(next_run),
            Td(_status_badge(status)),
        )

    return Div(
        Table(
            Thead(Tr(Th("Name"), Th("Contact"), Th("Frequency"), Th("Next Run"), Th("Status", style="text-align:center"))),
            Tbody(*[_row(s) for s in subs]),
            cls="data-table",
        ),
        id="sub-table",
    )


# ---------------------------------------------------------------------------
# Subscription action bar (replaces standard doc Finalize/Send/Mark-as-Sent)
# ---------------------------------------------------------------------------

def _sub_action_controls(entity_id: str, sub: dict) -> list:
    """Return header controls for the subscription detail page.

    Draft: [Frequency select] [Start Date input] [Custom days input, if custom] [Activate btn]
    Active: [status badge] [Generate Now] [Pause] [Cancel]
    Paused: [status badge] [Resume] [Cancel]
    Cancelled: [status badge]
    """
    status = sub.get("status", "draft")
    freq = sub.get("frequency") or "monthly"
    start = sub.get("start_date") or ""
    interval = sub.get("custom_interval_days") or ""

    if status == "draft":
        # Frequency select - submits instantly via PATCH to docs field endpoint
        freq_select = Select(
            *[Option(f.capitalize(), value=f, selected=(f == freq)) for f in _FREQUENCIES],
            name="value",
            hx_patch=f"/docs/{entity_id}/field/frequency",
            hx_swap="none",
            hx_trigger="change",
            title="Frequency",
            cls="form-input form-input--inline",
            style="width:130px",
        )
        # Start date
        start_input = Input(
            type="date", name="value", value=start,
            hx_patch=f"/docs/{entity_id}/field/start_date",
            hx_swap="none",
            hx_trigger="change",
            title="Start date",
            cls="form-input form-input--inline",
            style="width:145px",
        )
        # Custom interval - shown/hidden by JS based on freq select
        custom_input = Input(
            type="number", name="value", value=str(interval) if interval else "",
            placeholder="Days", min="1",
            hx_patch=f"/docs/{entity_id}/field/custom_interval_days",
            hx_swap="none",
            hx_trigger="change",
            title="Custom interval (days)",
            cls="form-input form-input--inline",
            style=f"width:80px;{'display:none' if freq != 'custom' else ''}",
            id="sub-custom-days",
        )
        activate_btn = Form(
            Button("Activate", type="submit", cls="btn btn--primary"),
            method="post",
            action=f"/subscriptions/{entity_id}/activate",
            title="Set frequency and start date, then activate",
        )
        # JS: show/hide custom days field when frequency changes
        toggle_js = Script("""
(function(){
  var sel = document.querySelector('select[hx-patch$="/field/frequency"]');
  var inp = document.getElementById('sub-custom-days');
  if (sel && inp) {
    sel.addEventListener('change', function(){ inp.style.display = sel.value === 'custom' ? '' : 'none'; });
  }
})();
""")
        return [freq_select, start_input, custom_input, activate_btn, toggle_js]

    # Non-draft: status badge + lifecycle buttons
    controls = [_status_badge(status)]
    if status == "active":
        controls.append(
            Form(Button("Generate Now", type="submit", cls="btn btn--secondary btn--sm"),
                 method="post", action=f"/subscriptions/{entity_id}/generate")
        )
        controls.append(
            Form(Button("Pause", type="submit", cls="btn btn--warning btn--sm"),
                 method="post", action=f"/subscriptions/{entity_id}/pause")
        )
    elif status == "paused":
        controls.append(
            Form(Button("Resume", type="submit", cls="btn btn--success btn--sm"),
                 method="post", action=f"/subscriptions/{entity_id}/resume")
        )
    if status not in ("cancelled", "draft"):
        controls.append(
            Form(Button("Cancel", type="submit", cls="btn btn--danger btn--sm"),
                 method="post", action=f"/subscriptions/{entity_id}/cancel")
        )
    # Generated docs count badge
    gen_count = len(sub.get("generated_doc_ids") or [])
    if gen_count:
        controls.append(
            Span(f"{gen_count} doc{'s' if gen_count != 1 else ''} generated", cls="text-muted", style="font-size:.85em")
        )
    return controls


# ---------------------------------------------------------------------------
# Route setup
# ---------------------------------------------------------------------------

def setup_routes(app) -> None:

    # --- List page ---

    @app.get("/subscriptions")
    async def subscriptions_list(request: Request, direction: str = "sales", status: str = ""):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params.get("q", "")
        page = int(request.query_params.get("page", 1))

        try:
            all_resp = await api.list_subscriptions(token, {"direction": direction, "limit": 1000})
            all_items = all_resp if isinstance(all_resp, list) else all_resp.get("items", [])
        except APIError:
            all_items = []

        filtered = all_items
        if status:
            filtered = [i for i in filtered if i.get("status") == status]
        if q:
            ql = q.lower()
            filtered = [i for i in filtered if
                        ql in (i.get("name") or "").lower()
                        or ql in (i.get("contact_name") or "").lower()
                        or ql in (i.get("contact_company_name") or "").lower()]

        total = len(filtered)
        page_items = filtered[(page - 1) * _PER_PAGE: page * _PER_PAGE]
        title = "Sales Subscriptions" if direction == "sales" else "Purchasing Subscriptions"
        extra = urlencode({k: v for k, v in {"direction": direction, "status": status, "q": q}.items() if v})

        content = Div(
            page_header(
                title,
                search_bar(placeholder="Search name, contact...", target="#sub-table",
                           url=f"/subscriptions/search?direction={direction}&status={status}"),
                Button("+ New Subscription", hx_post=f"/subscriptions/new?direction={direction}",
                       hx_swap="none", cls="btn btn--primary"),
            ),
            _direction_tabs(direction, status),
            _sub_status_cards(all_items, status, direction),
            _sub_table(page_items, direction),
            pagination(page, total, _PER_PAGE, "/subscriptions", extra),
            cls="page-content",
        )
        return base_shell(request, content, title=title,
                          nav_active="subscriptions_sales" if direction == "sales" else "subscriptions_purchasing")

    # --- Search (HTMX) ---

    @app.get("/subscriptions/search")
    async def subscriptions_search(request: Request, direction: str = "sales", status: str = ""):
        token = _token(request)
        if not token:
            return Div("Unauthorized", id="sub-table")
        q = request.query_params.get("q", "")
        try:
            resp = await api.list_subscriptions(token, {"direction": direction, "limit": 1000})
            items = resp if isinstance(resp, list) else resp.get("items", [])
        except APIError:
            items = []
        if status:
            items = [i for i in items if i.get("status") == status]
        if q:
            ql = q.lower()
            items = [i for i in items if
                     ql in (i.get("name") or "").lower()
                     or ql in (i.get("contact_name") or "").lower()
                     or ql in (i.get("contact_company_name") or "").lower()]
        return _sub_table(items, direction)

    # --- Create blank draft → redirect to detail ---

    @app.post("/subscriptions/new")
    async def create_subscription(request: Request, direction: str = "sales"):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        doc_type = _DIRECTION_DOC_TYPE.get(direction, "subscription_invoice")
        try:
            result = await api.create_subscription(token, {"doc_type": doc_type, "frequency": "monthly"})
            doc_id = result.get("entity_id") or result.get("id") or ""
            return RedirectResponse(f"/subscriptions/{doc_id}", status_code=303)
        except APIError as e:
            return RedirectResponse(f"/subscriptions?direction={direction}&error={e}", status_code=303)

    # --- Detail page ---

    @app.get("/subscriptions/{entity_id}")
    async def subscription_detail(request: Request, entity_id: str):
        from ui.routes.documents import _doc_detail
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        try:
            doc = await api.get_subscription(token, entity_id)
        except APIError:
            return RedirectResponse("/subscriptions", status_code=302)

        doc_type = doc.get("doc_type", "subscription_invoice")
        direction = "sales" if doc_type == "subscription_invoice" else "purchasing"

        if not doc.get("company_name"):
            try:
                company = await api.get_company(token)
                doc = {**doc,
                       "company_name": company.get("name") or "",
                       "company_address": company.get("address") or "",
                       "company_phone": company.get("phone") or "",
                       "company_tax_id": company.get("tax_id") or "",
                       "company_email": company.get("email") or ""}
            except Exception:
                pass

        cid = doc.get("contact_id")
        if cid and not doc.get("contact_name"):
            try:
                contact = await api.get_contact(token, cid)
                doc["contact_name"] = contact.get("name") or ""
                doc["contact_company_name"] = contact.get("company_name") or ""
                doc["contact_email"] = contact.get("email") or ""
                doc["contact_phone"] = contact.get("phone") or ""
                doc["contact_tax_id"] = contact.get("tax_id") or ""
            except Exception:
                pass

        ledger: list = []
        try:
            lr = await api.list_ledger(token, {"entity_id": entity_id, "limit": 50})
            ledger = lr.get("items", []) if isinstance(lr, dict) else []
        except Exception:
            pass

        price_lists: list = []
        try:
            price_lists = await api.get_price_lists(token)
        except Exception:
            pass

        company_taxes: list = []
        try:
            company_taxes = await api.get_taxes(token)
        except Exception:
            pass

        tz = "UTC"
        company_currency = "USD"
        try:
            co = await api.get_company(token)
            tz = co.get("timezone") or "UTC"
            company_currency = co.get("currency") or "USD"
        except Exception:
            pass

        doc_notes: list = []
        try:
            doc_notes = await api.list_doc_notes(token, entity_id)
        except Exception:
            pass

        status = doc.get("status", "draft")
        ref = doc.get("name") or doc.get("ref_id") or doc.get("doc_number") or entity_id
        type_label = "Sales Subscription" if direction == "sales" else "Purchasing Subscription"
        status_label = status.capitalize()
        list_url = f"/subscriptions?direction={direction}"

        return base_shell(
            breadcrumbs([
                ("Dashboard", "/dashboard"),
                ("Sales Subscriptions" if direction == "sales" else "Purchasing Subscriptions", list_url),
                (f"{status_label} {ref}", None),
            ]),
            page_header(f"{type_label} - {status_label} {ref}"),
            _doc_detail(
                doc,
                ledger=ledger,
                price_lists=price_lists,
                company_taxes=company_taxes,
                tz=tz,
                company_currency=company_currency,
                role=_get_role(request),
                notes=doc_notes,
                suppress_doc_actions=True,
                extra_left_actions=_sub_action_controls(entity_id, doc),
                suppress_pdf=True,
            ),
            title=f"{type_label} {ref} - Celerp",
            nav_active="subscriptions_sales" if direction == "sales" else "subscriptions_purchasing",
            request=request,
        )

    # --- Lifecycle actions ---

    @app.post("/subscriptions/{entity_id}/activate")
    async def activate_ui(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.activate_subscription(token, entity_id)
        except APIError as e:
            logger.warning("activate %s failed: %s", entity_id, e)
        return RedirectResponse(f"/subscriptions/{entity_id}", status_code=303)

    @app.post("/subscriptions/{entity_id}/generate")
    async def generate_ui(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.generate_subscription(token, entity_id)
        except APIError as e:
            logger.warning("generate %s failed: %s", entity_id, e)
        return RedirectResponse(f"/subscriptions/{entity_id}", status_code=303)

    @app.post("/subscriptions/{entity_id}/pause")
    async def pause_ui(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.pause_subscription(token, entity_id)
        except APIError as e:
            logger.warning("pause %s failed: %s", entity_id, e)
        return RedirectResponse(f"/subscriptions/{entity_id}", status_code=303)

    @app.post("/subscriptions/{entity_id}/resume")
    async def resume_ui(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.resume_subscription(token, entity_id)
        except APIError as e:
            logger.warning("resume %s failed: %s", entity_id, e)
        return RedirectResponse(f"/subscriptions/{entity_id}", status_code=303)

    @app.post("/subscriptions/{entity_id}/cancel")
    async def cancel_ui(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.cancel_subscription(token, entity_id)
        except APIError as e:
            logger.warning("cancel %s failed: %s", entity_id, e)
        return RedirectResponse(f"/subscriptions/{entity_id}", status_code=303)
