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
from ui.components.activity import format_timestamp
from ui.components.icons import import_icon
from ui.components.operation_key import FIELD, operation_key_vals, submitted_operation_key
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import breadcrumbs, display_enum, empty_state_cta, fmt_money, pagination, per_page_value, search_bar, status_cards
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
_SCHEDULE_EDITS = {"frequency": str, "anchor_date": str, "custom_interval_days": int,
                   "generation_mode": str, "auto_send": lambda v: v == "true", "catchup_periods": int}
# Edits that change what the Billing section and the header badge show, so both are drawn again.
_BILLING_FIELDS = ("generation_mode", "auto_send", "catchup_periods")
# The billing modes offered here; collecting a payment is offered where collection is available.
_MODES = ("manual_due", "auto_invoice")
_MODE_BADGE = {"auto_invoice": "subscriptions.bills_automatically",
               "auto_invoice_collect": "subscriptions.bills_and_collects_automatically"}
_CONSENTS = ("generation", "sending", "collection")
# Escape clears an inline preview or result panel (GDR 2j).
_PANEL_ESC_JS = Script("""
document.addEventListener('keydown', function(e) {
  if (e.key !== 'Escape') return;
  document.querySelectorAll('[data-esc-clear]').forEach(function(p) { p.innerHTML = ''; });
});
""")

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

    left: list = [_status_badge(status), _auto_badge(sub)]
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
        reopen = Button(t("btn.reopen"), cls="btn btn--secondary btn--sm", hx_post=f"{base}/reopen", hx_swap="none",
                        title=t("subscriptions.reopen_title"))
        if not row.get("history"):
            return reopen
        withdraw = Button(t("subscriptions.history_withdraw"), cls="btn btn--secondary btn--sm", hx_swap="none",
                          hx_post=f"/subscriptions/{entity_id}/history/{row['doc_id']}/withdraw")
        return Div(withdraw, reopen, style="display:flex;gap:.25rem")
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


def _list_banners(catch_up: list[dict], summary: dict) -> list:
    """The periods held for the catch-up preview, and what ran unattended since this user
    last looked, each with the page that handles it."""
    banners = []
    if catch_up:
        m = sum(len(i.get("periods") or []) for i in catch_up)
        banners.append(Div(t("subscriptions.catchup_banner", n=len(catch_up), m=m), " ",
                           A(t("subscriptions.catchup_open"), href="/subscriptions/catch-up"),
                           cls="flash flash--warning"))
    counts = summary.get("counts") or {}
    if any(counts.values()):
        banners.append(Div(t("subscriptions.summary_banner", generated=counts.get("generated", 0),
                             sent=counts.get("sent", 0), failed=counts.get("send_failed", 0)), " ",
                           A(t("subscriptions.summary_open"), href="/subscriptions/summary"),
                           cls="flash flash--info"))
    return banners


def _sub_amount(sub: dict) -> FT:
    return Td(fmt_money(float(sub.get("total") or 0), sub.get("currency")), cls="cell--number")


def _period_text(p: dict) -> str:
    return f"{p['service_start']} - {p['service_end']}"


def _due_panel(preview: dict, subs: dict[str, dict]) -> FT:
    """What Generate all due bills: each listed agreement's oldest due period, from the same
    query the run uses. Agreements with the most overdue bill date come first, the order
    they fall behind in. Nothing due names the next bill date and its agreement."""
    items = [i for i in preview.get("items") or [] if i["entity_id"] in subs]
    if not items:
        upcoming = sorted((s["next_bill_date"], eid) for eid, s in subs.items()
                          if s.get("status") == "active" and s.get("next_bill_date"))
        if not upcoming:
            return Div(P(t("subscriptions.due_none"), cls="hint"), cls="settings-card")
        day, eid = upcoming[0]
        return Div(P(A(t("subscriptions.due_empty", date=day, name=template_label(subs[eid], eid)),
                       href=f"/subscriptions/{eid}"), cls="hint"), cls="settings-card")
    items.sort(key=lambda i: i["periods"][0]["bill_date"])

    def _row(i: dict) -> FT:
        eid, first, more = i["entity_id"], i["periods"][0], len(i["periods"]) - 1
        sub = subs[eid]
        return Tr(
            Td(Input(type="checkbox", name="ids", value=eid, checked=True), cls="col-checkbox"),
            Td(A(template_label(sub, eid), href=f"/subscriptions/{eid}")),
            Td(sub.get("contact_name") or "--"),
            Td(_period_text(first)),
            Td(first["bill_date"]),
            Td(t("subscriptions.due_more", n=more) if more else ""),
            _sub_amount(sub),
        )

    return Form(
        P(t("subscriptions.due_hint", n=len(items)), cls="text-muted"),
        Table(Thead(Tr(Th(""), Th(t("th.name")), Th(t("th.contact")), Th(t("th.period")), Th(t("th.bill_date")),
                       Th(""), Th(t("th.total"), cls="cell--number"))),
              Tbody(*[_row(i) for i in items]), cls="data-table"),
        Div(Button(t("btn.generate_due"), type="submit", cls="btn btn--primary btn--sm"),
            Button(t("btn.close"), type="button", cls="btn btn--ghost btn--sm",
                   onclick="document.getElementById('sub-due-panel').innerHTML=''"),
            cls="form-actions"),
        hx_post="/subscriptions/generate-due", hx_target="#sub-due-panel", hx_swap="innerHTML",
        cls="settings-card",
    )


def _due_results(result: dict, subs: dict[str, dict]) -> FT:
    """One row per agreement: the document it billed, or why it was refused with Retry.
    The list stays until it is closed."""
    def _row(r: dict) -> FT:
        eid = r["entity_id"]
        name = template_label(subs[eid], eid) if eid in subs else eid
        if r.get("ok"):
            outcome = Td(t("subscriptions.period_billed"), " ", A(r.get("ref_id") or r["doc_id"], href=f"/docs/{r['doc_id']}"))
            action = Td("")
        else:
            outcome = Td(r.get("error") or "")
            action = Td(Button(t("btn.retry"), cls="btn btn--secondary btn--sm", hx_post="/subscriptions/generate-due",
                               hx_vals={"ids": eid}, hx_target="#sub-due-panel", hx_swap="innerHTML"))
        return Tr(Td(A(name, href=f"/subscriptions/{eid}")), outcome, action)

    return Div(
        P(t("subscriptions.due_result", billed=result.get("billed", 0), refused=result.get("refused", 0))),
        Table(Thead(Tr(Th(t("th.name")), Th(t("th.status")), Th(""))),
              Tbody(*[_row(r) for r in result.get("results") or []]), cls="data-table"),
        Div(Button(t("btn.close"), type="button", cls="btn btn--ghost btn--sm",
                   onclick="document.getElementById('sub-due-panel').innerHTML=''"), cls="form-actions"),
        cls="settings-card",
    )


def _catch_up_card(items: list[dict], subs: dict[str, dict], results: list[dict] | None = None) -> FT:
    """The periods automatic billing held back, each agreement's oldest first: they bill in
    that order, so the top row of an agreement is the one billed next. Bill the chosen
    periods, or skip them with a reason; a refused one says why and the rest go ahead."""
    refused = [r for r in results or [] if not r.get("ok")]
    done = len(results or []) - len(refused)
    notes = []
    if results is not None:
        notes.append(Div(t("subscriptions.catchup_result", done=done, refused=len(refused)),
                         cls=f"flash flash--{'warning' if refused else 'success'}"))
        for r in refused:
            sub = subs.get(r["entity_id"]) or {}
            notes.append(P(f"{template_label(sub, r['entity_id'])}: {r.get('error') or ''}", cls="text-danger"))
    if not items:
        return Div(*notes, empty_state_cta(t("subscriptions.catchup_empty"), t("subscriptions.review_all"),
                                           "/subscriptions?direction=sales"), id="catchup-card")

    rows = []
    for i in items:
        eid = i["entity_id"]
        sub = subs.get(eid) or {}
        for p in i["periods"]:
            rows.append(Tr(
                Td(Input(type="checkbox", name="item", value=f"{eid}|{p['period_index']}", checked=True), cls="col-checkbox"),
                Td(A(template_label(sub, eid), href=f"/subscriptions/{eid}")),
                Td(_period_text(p)),
                Td(p["bill_date"]),
                _sub_amount(sub),
            ))
    return Div(
        *notes,
        P(t("subscriptions.catchup_hint"), cls="text-muted"),
        Form(
            Table(Thead(Tr(Th(""), Th(t("th.name")), Th(t("th.period")), Th(t("th.bill_date")),
                           Th(t("th.total"), cls="cell--number"))),
                  Tbody(*rows), cls="data-table"),
            Div(Button(t("subscriptions.catchup_bill"), type="submit", cls="btn btn--primary btn--sm",
                       hx_post="/subscriptions/catch-up/run"),
                Input(type="text", name="reason", placeholder=t("label.reason"), cls="form-input form-input--inline",
                      style="width:200px"),
                Button(t("subscriptions.catchup_skip"), type="submit", cls="btn btn--secondary btn--sm",
                       hx_post="/subscriptions/catch-up/skip"),
                cls="form-actions"),
            hx_target="#catchup-card", hx_swap="outerHTML",
        ),
        id="catchup-card",
    )


def _summary_table(rows: list[dict], subs: dict[str, dict]) -> FT:
    """What ran unattended, newest first, each row linked to its agreement and document;
    a failed send offers Retry, which sends once."""
    def _row(r: dict) -> FT:
        eid = r["entity_id"]
        doc = A(r.get("ref_id") or r["doc_id"], href=f"/docs/{r['doc_id']}") if r.get("doc_id") else "--"
        retry = (Button(t("btn.retry"), cls="btn btn--secondary btn--sm", hx_post=r["retry_url"],
                        hx_vals={"back": "summary"}, hx_swap="none") if r.get("retry_url") else "")
        return Tr(Td(format_timestamp(r.get("at") or "") or "--"),
                  Td(A(template_label(subs.get(eid) or {}, eid), href=f"/subscriptions/{eid}")),
                  Td(r.get("message") or ""), Td(doc), Td(retry))

    return Table(Thead(Tr(Th(t("th.date")), Th(t("th.name")), Th(t("th.description")), Th(t("th.document")), Th(""))),
                 Tbody(*[_row(r) for r in rows]), cls="data-table")


def _auto_badge(sub: dict, oob: bool = False) -> FT:
    """The header badge naming an agreement that bills on its own; empty otherwise. ``oob``
    redraws the one already in the header."""
    key = _MODE_BADGE.get(sub.get("generation_mode") or "")
    attrs = {"id": "sub-auto-badge", **({"hx_swap_oob": "true"} if oob else {})}
    return Span(t(key), cls="badge badge--active", **attrs) if key else Span(**attrs)


def _billing_section(entity_id: str, sub: dict) -> FT:
    """How the agreement bills: by hand or on its own, whether it sends what it bills, and
    how many past periods automatic billing may still bill after a gap. Each automatic
    mode names when it was authorized; setting it back to manual undoes it."""
    base = f"/subscriptions/{entity_id}/schedule"
    attrs = {"hx_swap": "outerHTML", "hx_target": "#sub-billing", "hx_trigger": "change",
             "cls": "form-input form-input--inline"}
    mode = sub.get("generation_mode") or "manual_due"
    modes = _MODES if mode in _MODES else (*_MODES, mode)
    consents = sub.get("consents") or {}
    return Div(
        H3(t("subscriptions.billing_heading")),
        Div(
            Label(t("subscriptions.billing_mode"),
                  Select(*[Option(t(f"subscriptions.mode_{m}"), value=m, selected=(m == mode)) for m in modes],
                         name="value", hx_patch=f"{base}/generation_mode", **attrs)),
            Label(t("subscriptions.auto_send"),
                  Select(Option(t("settings.no"), value="false", selected=not sub.get("auto_send")),
                         Option(t("settings.yes"), value="true", selected=bool(sub.get("auto_send"))),
                         name="value", hx_patch=f"{base}/auto_send", **attrs)),
            Label(t("subscriptions.catchup_window"),
                  Input(type="number", name="value", min="0", max="12", value=str(sub.get("catchup_periods") or 0),
                        hx_patch=f"{base}/catchup_periods", style="width:80px", **attrs),
                  title=t("subscriptions.catchup_window_title")),
            style="display:flex;gap:1rem;flex-wrap:wrap;align-items:center",
        ),
        *[P(t(f"subscriptions.consent_{c}", date=format_timestamp(consents[c].get("at") or "")), cls="text-muted")
          for c in _CONSENTS if (consents.get(c) or {}).get("at")],
        id="sub-billing", cls="mt-lg",
    )


def _history_review(entity_id: str, review: list[dict]) -> FT | str:
    """Earlier documents that could not be matched to a period on their own: place each in
    the period it billed. Placing one can be withdrawn from the Periods table."""
    if not review:
        return ""

    def _row(r: dict) -> FT:
        doc_id = r["doc_id"]
        doc = A(r.get("ref_id") or doc_id, href=f"/docs/{doc_id}")
        places = [Button(t("subscriptions.history_place", period=_period_text(c)), cls="btn btn--secondary btn--sm",
                         hx_post=f"/subscriptions/{entity_id}/history/{doc_id}/confirm",
                         hx_vals={"period_index": c["period_index"]}, hx_swap="none")
                  for c in r.get("candidates") or []]
        return Tr(Td(doc), Td(*places) if places else Td(t("subscriptions.history_no_period")))

    return Div(
        H3(t("subscriptions.history_heading")),
        P(t("subscriptions.history_hint"), cls="text-muted"),
        Table(Thead(Tr(Th(t("th.document")), Th(t("th.period")))), Tbody(*[_row(r) for r in review]),
              cls="data-table"),
        id="history-review", cls="mt-lg",
    )


# ---------------------------------------------------------------------------
# Route setup
# ---------------------------------------------------------------------------

async def _held_and_unread(token: str) -> tuple[list[dict], dict]:
    """The catch-up preview and this user's unread summary; neither shown when unavailable."""
    try:
        catch_up = (await api.subscriptions_catch_up(token)).get("items") or []
    except APIError:
        catch_up = []
    try:
        summary = await api.subscriptions_summary(token)
    except APIError:
        summary = {}
    return catch_up, summary


async def _agreements(token: str, direction: str | None = None) -> dict[str, dict]:
    """Agreements by id, of one direction or both."""
    params = {"limit": 1000, **({"direction": direction} if direction else {})}
    resp = await api.list_subscriptions(token, params)
    items = resp if isinstance(resp, list) else resp.get("items", [])
    return {(i.get("id") or i.get("entity_id")): i for i in items}


def _catch_up_items(form) -> list[dict]:
    """The chosen catch-up periods, in the order the page lists them."""
    items = []
    for raw in form.getlist("item"):
        eid, _, k = str(raw).rpartition("|")
        if eid and k.lstrip("-").isdigit():
            items.append({"entity_id": eid, "period_index": int(k)})
    return items


def setup_routes(app) -> None:

    # --- Generate all due, catch-up and summary (before the detail route, which takes any id) ---

    @app.get("/subscriptions/due")
    async def subscriptions_due_panel(request: Request, direction: str = "sales"):
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        try:
            preview = await api.subscriptions_due(token)
            subs = await _agreements(token, direction)
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        return _due_panel(preview, subs)

    @app.post("/subscriptions/generate-due")
    async def subscriptions_generate_all_due(request: Request):
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        ids = [str(i) for i in (await request.form()).getlist("ids")]
        if not ids:
            return _action_error(t("label.select_rows_first"))
        try:
            result = await api.generate_all_due(token, ids)
            subs = await _agreements(token)
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        return _due_results(result, subs)

    @app.get("/subscriptions/catch-up")
    async def subscriptions_catch_up_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "view_subscriptions", page_view=True)):
            return r
        try:
            items = (await api.subscriptions_catch_up(token)).get("items") or []
            subs = await _agreements(token)
        except APIError as e:
            items, subs = [], {}
            logger.warning("Catch-up preview unavailable: %s", e.detail)
        title = t("subscriptions.catchup_title")
        return await base_shell(page_header(title), _catch_up_card(items, subs), title=title, request=request,
                                nav_active="subscriptions_sales")

    @app.post("/subscriptions/catch-up/{action}")
    async def subscriptions_catch_up_action(request: Request, action: str):
        if action not in ("run", "skip"):
            return Response(status_code=404)
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        chosen = _catch_up_items(form)
        if not chosen:
            return _action_error(t("label.select_rows_first"))
        try:
            if action == "run":
                results = (await api.run_subscriptions_catch_up(token, chosen)).get("results") or []
            else:
                reason = str(form.get("reason", "")).strip()
                results = []
                for item in chosen:
                    try:
                        await api.subscription_period_action(token, item["entity_id"], item["period_index"], "skip",
                                                             {"reason": reason})
                        results.append({**item, "ok": True})
                    except APIError as e:
                        results.append({**item, "ok": False, "error": refusal_text(e.data or e.detail)})
            items = (await api.subscriptions_catch_up(token)).get("items") or []
            subs = await _agreements(token)
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        return _catch_up_card(items, subs, results)

    @app.get("/subscriptions/summary")
    async def subscriptions_summary_page(request: Request, all: bool = False):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "view_subscriptions", page_view=True)):
            return r
        try:
            summary = await api.subscriptions_summary(token, everything=all)
            subs = await _agreements(token)
        except APIError as e:
            summary, subs = {}, {}
            logger.warning("Subscription summary unavailable: %s", e.detail)
        rows = summary.get("rows") or []
        counts = summary.get("counts") or {}
        title = t("subscriptions.summary_title")
        toggle = (A(t("subscriptions.summary_unread"), href="/subscriptions/summary", cls="btn btn--secondary") if all
                  else A(t("subscriptions.summary_all"), href="/subscriptions/summary?all=1", cls="btn btn--secondary"))
        content = Div(
            page_header(title,
                        Button(t("subscriptions.summary_ack"), hx_post="/subscriptions/summary/ack", hx_swap="none",
                               cls="btn btn--primary") if rows and not all else "",
                        toggle),
            P(t("subscriptions.summary_counts", generated=counts.get("generated", 0), sent=counts.get("sent", 0),
                failed=counts.get("send_failed", 0)), cls="text-muted"),
            _summary_table(rows, subs) if rows else empty_state_cta(
                t("subscriptions.summary_empty"), t("subscriptions.summary_all"), "/subscriptions/summary?all=1"),
            cls="page-content",
        )
        return await base_shell(content, title=title, request=request, nav_active="subscriptions_sales")

    @app.post("/subscriptions/summary/ack")
    async def subscriptions_summary_ack(request: Request):
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.acknowledge_subscriptions_summary(token)
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        return Response(status_code=204, headers={"HX-Redirect": "/subscriptions/summary"})

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
        catch_up, summary = await _held_and_unread(token)

        content = Div(
            page_header(
                title,
                search_bar(placeholder=t("subscriptions.search_placeholder"), target="#sub-table",
                           url=f"/subscriptions/search?direction={direction}&status={status}{'&review=1' if review else ''}",
                           label=t(f"subscriptions.search_{side}")),
                Button(t("page.new_subscription"), hx_post=f"/subscriptions/new?direction={direction}",
                       hx_swap="none", cls="btn btn--primary"),
                Button(t("btn.generate_all_due"), hx_get=f"/subscriptions/due?direction={direction}",
                       hx_target="#sub-due-panel", hx_swap="innerHTML", cls="btn btn--secondary"),
                A(import_icon(), t("btn.import"), href="/subscriptions/import", cls="btn btn--secondary",
                  data_import_hint=True) if can_import else "",
            ),
            *_list_banners(catch_up, summary),
            _review_banner(all_items, direction, review),
            Div(id="sub-due-panel", data_esc_clear="1"),
            _PANEL_ESC_JS,
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
            _billing_section(entity_id, doc),
            _history_review(entity_id, schedule.get("review") or []),
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
        form = await request.form()
        reason = str(form.get("reason", "")).strip()
        try:
            await api.subscription_period_action(token, entity_id, k, action,
                                                 {"reason": reason} if action == "skip" else None)
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        back = "/subscriptions/summary" if form.get("back") == "summary" else f"/subscriptions/{entity_id}"
        return Response(status_code=204, headers={"HX-Redirect": back})

    @app.post("/subscriptions/{entity_id}/history/{doc_id}/{action}")
    async def subscription_history_ui(request: Request, entity_id: str, doc_id: str, action: str):
        if action not in api.HISTORY_ACTIONS:
            return Response(status_code=404)
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        k = str((await request.form()).get("period_index", ""))
        if action == "confirm" and not k.lstrip("-").isdigit():
            return _action_error(t("subscriptions.err_history_not_candidate"))
        try:
            await api.subscription_history_action(token, entity_id, doc_id, action,
                                                  {"period_index": int(k)} if action == "confirm" else None)
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        return Response(status_code=204, headers={"HX-Redirect": f"/subscriptions/{entity_id}#history-review"})

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
            return _action_error(t("subscriptions.err_custom_days" if field == "custom_interval_days"
                                   else "subscriptions.err_catchup_window"))
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        if field not in _BILLING_FIELDS:
            return Response(status_code=204)
        try:
            sub = await api.get_subscription(token, entity_id)
        except APIError as e:
            return _action_error(refusal_text(e.data or e.detail))
        return _billing_section(entity_id, sub), _auto_badge(sub, oob=True)
