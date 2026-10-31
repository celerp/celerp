# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Subscription template UI routes.

Subscription templates are docs with doc_type "subscription_invoice" or
"subscription_po".  The list page mirrors /docs?type=invoice (status cards,
search, pagination).  The detail page delegates to _doc_detail() from the docs
module; subscription-specific controls (frequency, first bill date, lifecycle
actions) live in the page_header row, suppressing the standard doc action bar, and
the agreement's periods are listed under the document.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from celerp.output.document_context import prepare_document_output
from celerp.services.record_quantities import template_label
from ui.components.icons import import_icon
from ui.components.operation_key import FIELD, operation_key_vals, submitted_operation_key
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import breadcrumbs, display_enum, empty_state_cta, pagination, per_page_value, search_bar, status_cards
from ui.config import get_token as _token, get_role as _get_role
from ui.routes.documents import _action_error
from ui.routes.reports import _get_company
from ui.routes.settings import _check_permission
from ui.i18n import refusal_text, t

logger = logging.getLogger(__name__)

_PER_PAGE = 50

_FREQUENCIES = ["weekly", "biweekly", "monthly", "quarterly", "annually", "custom"]

_STATUS_CSS = {
    "active": "badge--active",
    "paused": "badge--paused",
    "cancelled": "badge--void",
    "draft": "badge--draft",
}

# Billing actions carry the operation key as the request key, so a repeated click bills once.
_BILLING_ACTIONS = ("generate-due", "bill-early")
# The schedule fields the detail page edits, each with how its submitted text is read.
_SCHEDULE_EDITS = {"frequency": str, "anchor_date": str, "custom_interval_days": int}

_DIRECTION_DOC_TYPE = {
    "sales": "subscription_invoice",
    "purchasing": "subscription_po",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _status_badge(status: str) -> FT:
    return Span(display_enum(status, "subscription_status"), cls=f"badge {_STATUS_CSS.get(status, 'badge--draft')}")


def _direction_tabs(direction: str, status: str = "") -> FT:
    def _tab(label: str, dir_: str) -> FT:
        params = f"direction={dir_}"
        if status:
            params += f"&status={status}"
        return A(label, href=f"/subscriptions?{params}",
                 cls=f"tab-link {'tab-link--active' if direction == dir_ else ''}")
    return Nav(_tab(t("dashboard.sales"), "sales"), _tab(t("subscriptions.purchasing"), "purchasing"), cls="tab-nav")


def _sub_status_cards(items: list[dict], active_status: str, direction: str) -> FT:
    counts: dict[str, int] = {}
    for it in items:
        s = it.get("status", "draft")
        counts[s] = counts.get(s, 0) + 1
    base = f"/subscriptions?direction={direction}"
    issued_total = sum(counts.get(s, 0) for s in ("active", "paused", "cancelled"))
    # Draft first, then All Issued, then lifecycle statuses
    cards = [
        {"label": display_enum("draft", "subscription_status"),     "count": counts.get("draft", 0),     "total": None, "status": "draft",     "color": "gray",   "_url": f"{base}&status=draft"},
        {"label": t("status.all_issued"),                           "count": issued_total,               "total": None, "status": "",          "color": "blue",   "_url": base, "_active_key": ""},
        {"label": display_enum("active", "subscription_status"),    "count": counts.get("active", 0),    "total": None, "status": "active",    "color": "green",  "_url": f"{base}&status=active"},
        {"label": display_enum("paused", "subscription_status"),    "count": counts.get("paused", 0),    "total": None, "status": "paused",    "color": "yellow", "_url": f"{base}&status=paused"},
        {"label": display_enum("cancelled", "subscription_status"), "count": counts.get("cancelled", 0), "total": None, "status": "cancelled", "color": "gray",   "_url": f"{base}&status=cancelled"},
    ]
    return status_cards(cards, base, active_status or None, currency=None, show_all_card=False)


def _sub_table(subs: list[dict], direction: str, review: bool = False) -> FT:
    if not subs and review:
        return Div(empty_state_cta(t("subscriptions.review_empty"), t("subscriptions.review_all"),
                                   f"/subscriptions?direction={direction}"), id="sub-table")
    if not subs:
        return Div(empty_state_cta(t("label.no_subscription_templates_found"), t("page.new_subscription"),
                                   f"/subscriptions/new?direction={direction}", hx_post=True), id="sub-table")

    def _row(s: dict) -> FT:
        eid = s.get("id") or s.get("entity_id", "")
        name = template_label(s, eid)
        contact = s.get("contact_name") or s.get("contact_company_name") or "-"
        freq = display_enum(s["frequency"], "subscription_frequency") if s.get("frequency") else "-"
        next_due = s.get("next_bill_date") or "-"
        status = s.get("status", "draft")
        return Tr(
            Td(A(name, href=f"/subscriptions/{eid}")),
            Td(contact),
            Td(freq),
            Td(next_due),
            Td(_status_badge(status)),
        )

    return Div(
        Table(
            Thead(Tr(Th(t("th.name")), Th(t("th.contact")), Th(t("th.frequency")), Th(t("th.next_due")), Th(t("th.status"), style="text-align:center"))),
            Tbody(*[_row(s) for s in subs]),
            cls="data-table",
        ),
        id="sub-table",
    )


def _review_banner(items: list[dict], direction: str, review: bool) -> FT | str:
    """Names how many agreements wait for history review, with the filter that lists them."""
    n = sum(1 for i in items if i.get("history_review"))
    if not n or review:
        return ""
    return Div(t("subscriptions.review_banner", n=n), " ",
               A(t("subscriptions.review_open"), href=f"/subscriptions?direction={direction}&review=1"),
               cls="flash flash--warning")


# ---------------------------------------------------------------------------
# Subscription action bar (replaces standard doc Finalize/Send/Mark-as-Sent)
# ---------------------------------------------------------------------------

def _schedule_inputs(entity_id: str, sub: dict):
    """Frequency and first bill date controls, shared between draft and paused states. Each
    change goes through the agreement's schedule, which refuses one that would move a
    period already billed or skipped."""
    freq = sub.get("frequency") or "monthly"
    first_bill = sub.get("anchor_date") or ""
    interval = sub.get("custom_interval_days") or ""
    freq_select = Select(
        *[Option(display_enum(f, "subscription_frequency"), value=f, selected=(f == freq)) for f in _FREQUENCIES],
        name="value",
        hx_patch=f"/subscriptions/{entity_id}/schedule/frequency",
        hx_swap="none",
        hx_trigger="change",
        title=t("th.frequency"),
        cls="form-input form-input--inline",
        style="width:130px",
    )
    start_input = Input(
        type="date", name="value", value=first_bill,
        hx_patch=f"/subscriptions/{entity_id}/schedule/anchor_date",
        hx_swap="none",
        hx_trigger="change",
        title=t("label.first_bill_date"),
        cls="form-input form-input--inline",
        style="width:145px",
    )
    custom_input = Input(
        type="number", name="value", value=str(interval) if interval else "",
        placeholder=t("label.days"), min="1",
        hx_patch=f"/subscriptions/{entity_id}/schedule/custom_interval_days",
        hx_swap="none",
        hx_trigger="change",
        title=t("subscriptions.custom_interval_title"),
        cls="form-input form-input--inline",
        style=f"width:80px;{'display:none' if freq != 'custom' else ''}",
        id="sub-custom-days",
    )
    toggle_js = Script("""
(function(){
  var sel = document.querySelector('select[hx-patch$="/schedule/frequency"]');
  var inp = document.getElementById('sub-custom-days');
  if (sel && inp) {
    sel.addEventListener('change', function(){ inp.style.display = sel.value === 'custom' ? '' : 'none'; });
  }
})();
""")
    return [freq_select, start_input, custom_input, toggle_js]


def _bill_date_note(sub: dict, next_bill: str | None) -> FT | str:
    """Shows the first bill date until a period is billed, then the next bill date."""
    billed = any((e or {}).get("state") == "billed" for e in (sub.get("periods") or {}).values())
    if not billed and (first := next_bill or sub.get("anchor_date")):
        return Span(t("subscriptions.first_bill_on", date=first), cls="text-muted", style="font-size:.85em")
    if next_bill:
        return Span(t("subscriptions.next_bill_on", date=next_bill), cls="text-muted", style="font-size:.85em")
    return ""


def _sub_action_controls(entity_id: str, sub: dict, next_bill: str | None = None) -> tuple[list, list]:
    """Return (left_actions, right_actions) for the subscription detail page.

    Draft:   left=[Frequency, FirstBill, CustomDays, Activate, note]      right=[]
    Active:  left=[badge, GenerateDue, BillEarly, note]                   right=[Pause, Cancel]
    Paused:  left=[badge, Frequency, FirstBill, CustomDays, note]         right=[Resume, Cancel]
    Cancelled: left=[badge]                                               right=[]
    """
    status = sub.get("status", "draft")
    note = _bill_date_note(sub, next_bill)

    if status == "draft":
        return _schedule_inputs(entity_id, sub) + [
            Button(t("btn.activate"), cls="btn btn--primary", hx_post=f"/subscriptions/{entity_id}/activate",
                   hx_swap="none", title=t("subscriptions.activate_title")),
            note,
        ], []

    left: list = [_status_badge(status)]
    right: list = []

    if status == "active":
        left.extend([
            Button(t("btn.generate_due"), cls="btn btn--secondary btn--sm", hx_post=f"/subscriptions/{entity_id}/generate-due",
                   hx_swap="none", hx_vals=operation_key_vals()),
            Button(t("btn.bill_early"), cls="btn btn--secondary btn--sm", hx_post=f"/subscriptions/{entity_id}/bill-early",
                   hx_swap="none", hx_vals=operation_key_vals()),
        ])
        right.append(
            Button(t("btn.pause"), cls="btn btn--warning btn--sm", hx_post=f"/subscriptions/{entity_id}/pause", hx_swap="none")
        )
    elif status == "paused":
        # Allow editing schedule while paused
        left.extend(_schedule_inputs(entity_id, sub))
        right.append(
            Button(t("btn.resume"), cls="btn btn--success btn--sm", hx_post=f"/subscriptions/{entity_id}/resume", hx_swap="none")
        )

    if status not in ("cancelled", "draft"):
        right.append(
            Button(t("btn.cancel"), cls="btn btn--danger btn--sm", hx_post=f"/subscriptions/{entity_id}/cancel", hx_swap="none")
        )

    if status != "cancelled":
        left.append(note)
    gen_count = sum(1 for e in (sub.get("periods") or {}).values() if (e or {}).get("state") == "billed")
    if gen_count:
        left.append(
            Span(t("subscriptions.docs_generated", n=gen_count), cls="text-muted", style="font-size:.85em")
        )

    return left, right


_PERIOD_STATE_KEY = {"billed": "billed", "skipped": "skipped", "unresolved": "unresolved"}


def _period_actions(entity_id: str, row: dict) -> FT:
    """Skip an open period with a reason; un-skip a skipped one; reopen a billed one whose
    document is voided. The server refuses any that does not apply and says why."""
    k, state = row["period_index"], row.get("state")
    base = f"/subscriptions/{entity_id}/periods/{k}"
    if state == "skipped":
        return Button(t("btn.unskip"), cls="btn btn--secondary btn--sm", hx_post=f"{base}/unskip", hx_swap="none")
    if state == "billed":
        return Button(t("btn.reopen"), cls="btn btn--secondary btn--sm", hx_post=f"{base}/reopen", hx_swap="none",
                      title=t("subscriptions.reopen_title"))
    if state == "unresolved":
        return A(t("subscriptions.review_open"), href=f"/subscriptions/{entity_id}#history-review")
    return Form(
        Input(type="text", name="reason", placeholder=t("label.reason"), cls="form-input form-input--inline",
              style="width:160px", required=True),
        Button(t("btn.skip"), type="submit", cls="btn btn--secondary btn--sm"),
        hx_post=f"{base}/skip", hx_swap="none", style="display:flex;gap:.25rem",
    )


def _periods_table(entity_id: str, rows: list[dict]) -> FT:
    """The agreement's periods, newest first: dates, state, the document that billed it."""
    if not rows:
        return ""

    def _row(r: dict) -> FT:
        state = r.get("state")
        label = t(f"subscriptions.period_{_PERIOD_STATE_KEY.get(state, 'open')}")
        doc = A(r["ref_id"], href=f"/docs/{r['doc_id']}") if r.get("doc_id") else (r.get("reason") or "--")
        return Tr(Td(f"{r['service_start']} - {r['service_end']}"), Td(r["bill_date"]), Td(label), Td(doc),
                  Td(_period_actions(entity_id, r)))

    return Div(
        H3(t("subscriptions.periods_heading")),
        Table(
            Thead(Tr(Th(t("th.period")), Th(t("th.bill_date")), Th(t("th.status")), Th(t("th.document")), Th(""))),
            Tbody(*[_row(r) for r in rows]),
            cls="data-table",
        ),
        id="sub-periods", cls="mt-lg",
    )


# ---------------------------------------------------------------------------
# Route setup
# ---------------------------------------------------------------------------

def setup_routes(app) -> None:

    # --- List page ---

    @app.get("/subscriptions")
    async def subscriptions_list(request: Request, direction: str = "sales", status: str = "", review: bool = False):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "view_subscriptions", page_view=True)):
            return r
        q = request.query_params.get("q", "")
        page = int(request.query_params.get("page", 1))
        per_page = per_page_value(request.query_params.get("per_page"), _PER_PAGE)

        params = {"direction": direction, "limit": 1000, **({"review": "true"} if review else {})}
        try:
            all_resp = await api.list_subscriptions(token, params)
            all_items = all_resp if isinstance(all_resp, list) else all_resp.get("items", [])
        except APIError:
            all_items = []

        filtered = all_items
        if status:
            filtered = [i for i in filtered if i.get("status") == status]
        else:
            # "All Issued" default view excludes drafts
            filtered = [i for i in filtered if i.get("status") != "draft"]
        if q:
            ql = q.lower()
            filtered = [i for i in filtered if
                        ql in (i.get("ref_id") or i.get("doc_number") or "").lower()
                        or ql in (i.get("name") or "").lower()
                        or ql in (i.get("contact_name") or "").lower()
                        or ql in (i.get("contact_company_name") or "").lower()]

        total = len(filtered)
        page_items = filtered[(page - 1) * per_page: page * per_page]
        side = "sales" if direction == "sales" else "purchasing"
        title = t(f"nav.subscriptions_{side}")
        extra = urlencode({k: v for k, v in {"direction": direction, "status": status, "q": q,
                                             "review": "1" if review else ""}.items() if v})
        can_import = await _check_permission(request, "import_export_data") is None

        content = Div(
            page_header(
                title,
                search_bar(placeholder=t("subscriptions.search_placeholder"), target="#sub-table",
                           url=f"/subscriptions/search?direction={direction}&status={status}{'&review=1' if review else ''}",
                           label=t(f"subscriptions.search_{side}")),
                Button(t("page.new_subscription"), hx_post=f"/subscriptions/new?direction={direction}",
                       hx_swap="none", cls="btn btn--primary"),
                A(import_icon(), t("btn.import"), href="/subscriptions/import", cls="btn btn--secondary",
                  data_import_hint=True) if can_import else "",
            ),
            _review_banner(all_items, direction, review),
            _sub_status_cards(all_items, status, direction),
            _sub_table(page_items, direction, review),
            pagination(page, total, per_page, "/subscriptions", extra),
            cls="page-content",
        )
        return await base_shell(content, title=title, request=request,
                          nav_active="subscriptions_sales" if direction == "sales" else "subscriptions_purchasing")

    # --- Search (HTMX) ---

    @app.get("/subscriptions/search")
    async def subscriptions_search(request: Request, direction: str = "sales", status: str = "", review: bool = False):
        token = _token(request)
        if not token:
            return Div(t("error.session_expired"), id="sub-table")
        q = request.query_params.get("q", "")
        try:
            resp = await api.list_subscriptions(token, {"direction": direction, "limit": 1000,
                                                        **({"review": "true"} if review else {})})
            items = resp if isinstance(resp, list) else resp.get("items", [])
        except APIError:
            items = []
        if status:
            items = [i for i in items if i.get("status") == status]
        else:
            items = [i for i in items if i.get("status") != "draft"]
        if q:
            ql = q.lower()
            items = [i for i in items if
                     ql in (i.get("ref_id") or i.get("doc_number") or "").lower()
                     or ql in (i.get("name") or "").lower()
                     or ql in (i.get("contact_name") or "").lower()
                     or ql in (i.get("contact_company_name") or "").lower()]
        return _sub_table(items, direction, review)

    # --- Create blank draft → redirect to detail ---

    @app.post("/subscriptions/new")
    async def create_subscription(request: Request, direction: str = "sales"):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        doc_type = _DIRECTION_DOC_TYPE.get(direction, "subscription_invoice")
        try:
            result = await api.create_subscription(token, {"doc_type": doc_type, "frequency": "monthly"})
            doc_id = result.get("entity_id") or result.get("id") or ""
            return _R("", status_code=204, headers={"HX-Redirect": f"/subscriptions/{doc_id}"})
        except APIError as e:
            return _R("", status_code=204, headers={"HX-Redirect": f"/subscriptions?direction={direction}&error={e}"})

    # --- Detail page ---

    @app.get("/subscriptions/{entity_id}")
    async def subscription_detail(request: Request, entity_id: str):
        from ui.routes.documents import _doc_detail, _merge_company_letterhead, doc_detail_connectors
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        try:
            doc = await api.get_subscription(token, entity_id)
        except APIError:
            return RedirectResponse("/subscriptions", status_code=302)

        doc_type = doc.get("doc_type", "subscription_invoice")
        direction = "sales" if doc_type == "subscription_invoice" else "purchasing"

        doc = await _merge_company_letterhead(token, doc)

        cid = doc.get("contact_id")
        if cid:
            try:
                doc = prepare_document_output(doc, contact=await api.get_contact(token, cid))
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
            company_taxes = await api.get_document_taxes(token, doc_type)
        except Exception:
            pass

        co = await _get_company(token)
        tz = co.get("timezone") or "UTC"
        company_currency = co.get("currency") or "USD"
        co_settings: dict = co.get("settings") or {}
        co_connectors: set[str] = set()
        try:
            co_connectors = await doc_detail_connectors(co)
        except Exception:
            pass

        doc_notes: list = []
        try:
            doc_notes = await api.list_doc_notes(token, entity_id)
        except Exception:
            pass

        schedule: dict = {}
        try:
            schedule = await api.get_subscription_periods(token, entity_id)
        except Exception:
            pass

        status = doc.get("status", "draft")
        ref = doc.get("ref_id") or doc.get("doc_number") or entity_id.split(":")[-1]
        type_label = t("subscriptions.type_sales") if direction == "sales" else t("subscriptions.type_purchasing")
        status_label = display_enum(status, "subscription_status")
        list_url = f"/subscriptions?direction={direction}"

        doc = {**doc, "next_bill_date": schedule.get("next_bill_date")}
        _sub_left_actions, _sub_right_actions = _sub_action_controls(entity_id, doc, doc["next_bill_date"])

        return await base_shell(
            breadcrumbs([
                (t("nav.dashboard"), "/dashboard"),
                (t("nav.subscriptions_sales") if direction == "sales" else t("nav.subscriptions_purchasing"), list_url),
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
                settings=co_settings,
                connected_connectors=co_connectors,
                notes=doc_notes,
                suppress_doc_actions=True,
                extra_left_actions=_sub_left_actions,
                extra_right_actions=_sub_right_actions,
                suppress_pdf=True,
            ),
            _periods_table(entity_id, schedule.get("rows") or []),
            title=f"{type_label} {ref} - Celerp",
            nav_active="subscriptions_sales" if direction == "sales" else "subscriptions_purchasing",
            request=request,
        )

    # --- Lifecycle actions ---

    @app.post("/subscriptions/{entity_id}/{action}")
    async def subscription_action_ui(request: Request, entity_id: str, action: str):
        if action not in api.SUBSCRIPTION_ACTIONS:
            return Response(status_code=404)
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        key = submitted_operation_key(await request.form())
        body = {"request_key": key[FIELD]} if action in _BILLING_ACTIONS and key else None
        try:
            await api.subscription_action(token, entity_id, action, body)
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        return Response(status_code=204, headers={"HX-Redirect": f"/subscriptions/{entity_id}"})

    @app.post("/subscriptions/{entity_id}/periods/{k}/{action}")
    async def subscription_period_ui(request: Request, entity_id: str, k: int, action: str):
        if action not in api.PERIOD_ACTIONS:
            return Response(status_code=404)
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        reason = str((await request.form()).get("reason", "")).strip()
        try:
            await api.subscription_period_action(token, entity_id, k, action,
                                                 {"reason": reason} if action == "skip" else None)
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        return Response(status_code=204, headers={"HX-Redirect": f"/subscriptions/{entity_id}"})

    @app.patch("/subscriptions/{entity_id}/schedule/{field}")
    async def subscription_schedule_ui(request: Request, entity_id: str, field: str):
        if field not in _SCHEDULE_EDITS:
            return Response(status_code=404)
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        raw = str((await request.form()).get("value", "")).strip()
        try:
            value = _SCHEDULE_EDITS[field](raw) if raw else None
            await api.update_subscription_schedule(token, entity_id, {field: value})
        except ValueError:
            return _action_error(t("subscriptions.err_custom_days"))
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        return Response(status_code=204)
