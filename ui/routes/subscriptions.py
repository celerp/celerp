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

import json
import logging
import math
from urllib.parse import urlencode, urlsplit

from fasthtml.common import *
from starlette.datastructures import QueryParams
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from celerp.output.document_context import prepare_document_output
from celerp.services.record_quantities import template_label
from celerp.accounting_roles import RECOGNIZE_FIELD
from celerp.services.business_time import business_today
from celerp_subscriptions.billing import ENDED, amendments_of, bills, doc_kind, lines_on, send_written
from celerp_subscriptions.periods import COLLECT_MODE, shown
from celerp_subscriptions.summary import NEEDS_YOU, SUMMARY_WINDOW
from ui.components.activity import change_text, format_timestamp, register_fields
from ui.components.icons import import_icon
from ui.components.operation_key import FIELD, operation_key_vals, operation_key_input, required_operation_key, submitted_operation_key
from ui.api_client import APIError, error_message
from ui.components.shell import base_shell, module_active, page_header, toast_header
from ui.components.table import (EMPTY, breadcrumbs, display_enum, select_all_th, empty_state_cta, fmt_money, pagination, per_page_value,
                                 search_bar, searchable_select, status_cards)
from ui.config import get_token as _token, get_role as _get_role
from ui.routes.action_answers import _done, _redrawn, _refused, _refused_redrawn
from ui.routes.documents import _action_error, persist_then_post
from ui.routes.reports import _get_company
from ui.routes.settings import _check_permission
from ui.i18n import refusal_text, sentence, t
from ui.components.encode import as_js, html_response, safe_url, to_markup

logger = logging.getLogger(__name__)

_PER_PAGE = 50


# How a send from the page reads after it, by billing.send_status: the toast it answers with.
_SEND_TOAST = {"sent": "success", "failed": "error"}


def _send_button(url: str, send: str | None, again: bool, attempt: int, **vals) -> FT:
    """Send a billed period's email: Retry after a failure or while a send is with the mail server
    (the server says to check again then), Send again (behind a confirm that it may already have
    arrived) after a cut-off send, Send otherwise. It names the ``attempt`` on screen, so the same
    click arriving twice sends once."""
    label = "btn.send_again" if again else "btn.retry" if send in ("failed", "sending", "unconfirmed") else "btn.send"
    confirm = {"hx_confirm": t("subscriptions.send_again_confirm")} if again else {}
    return Button(t(label), cls="btn btn--secondary btn--sm", hx_post=url, hx_swap="none",
                  hx_vals={**vals, "attempt": attempt, **({"again": "1"} if again else {})}, **confirm)

_FREQUENCIES = ["weekly", "biweekly", "monthly", "quarterly", "annually", "custom"]
_TIMINGS = ("advance", "arrears")

_STATUS_CSS = {
    "active": "badge--active",
    "paused": "badge--paused",
    "cancelled": "badge--void",
    "draft": "badge--draft",
}

# Billing actions carry the operation key as the request key, so a repeated click bills once.
_BILLING_ACTIONS = ("generate-due", "bill-early")
# The schedule fields the detail page edits, each with how its submitted text is read.
_SCHEDULE_EDITS = {"frequency": str, "anchor_date": str, "trial_end": str, "custom_interval_days": int, "billing_timing": str,
                   "term_start": str, "term_end": str, "cancel_at_period_end": lambda v: v == "true",
                   "generation_mode": str, "auto_send": lambda v: v == "true", "catchup_periods": int,
                   RECOGNIZE_FIELD: lambda v: v == "true", "kind": str, "beneficiary_contact_id": str,
                   "grace_days": int}
# What a schedule value that does not read as its type is told.
_FIELD_ERRORS = {"custom_interval_days": "subscriptions.err_custom_days",
                 "catchup_periods": "subscriptions.err_catchup_window",
                 "grace_days": "subscriptions.err_grace_days"}
# What each lifecycle action says it did; a billing action names its document and period.
_ACTION_DONE = {"activate": "subscriptions.done_activated", "pause": "subscriptions.done_paused",
                "resume": "subscriptions.done_resumed", "cancel": "subscriptions.done_cancelled"}
# What each period action says it did, naming the period's dates. Send goes back to the
# summary, whose outcome row says how the send went.
_PERIOD_DONE = {"skip": "subscriptions.done_skipped", "unskip": "subscriptions.done_unskipped",
                "reopen": "subscriptions.done_reopened"}
# What each history review action says it did: Withdraw returns a placed or a set-aside
# document to review alike.
_HISTORY_DONE = {"confirm": "subscriptions.done_history_placed", "set-aside": "subscriptions.done_history_set_aside",
                 "withdraw": "subscriptions.done_history_returned"}
# How the Periods table marks each period status; one not listed is plain text.
_PERIOD_BADGE = {"overdue": "badge--overdue", "due": "badge--warning", "draft": "badge--draft", "voided": "badge--void"}
# A period's email by billing.send_status; a sent one needs no mark.
_SEND_BADGE = {"waiting": ("subscriptions.period_send_pending", "badge--warning"),
               "not_sent": ("subscriptions.period_send_not_sent", "badge--draft"),
               "failed": ("subscriptions.period_send_failed", "badge--overdue"),
               "sending": ("subscriptions.period_send_sending", "badge--warning"),
               "unconfirmed": ("subscriptions.period_send_unconfirmed", "badge--warning")}
_MODES = ("manual_due", "auto_invoice")
_MODE_BADGE = {"auto_invoice": "subscriptions.bills_automatically", COLLECT_MODE: "subscriptions.bills_and_collects"}
# The agreement keeps a consent only for a mode that runs, so each one kept is shown.
_CONSENTS = ("generation", "sending", "collection")
# The card on file section's actions: the API's own path under /collection for each.
_COLLECTION_ACTIONS = ("setup-link", "revoke")


# A schedule or billing control saves on change; its answer is every element of the page
# that shows the schedule, each swapped by its id (_schedule_views), the control's own section included.
_SCHEDULE_CONTROL = {"hx_swap": "none", "hx_trigger": "change"}


def _yes_no(on) -> str:
    return t("settings.yes") if on else t("settings.no")


# Each agreement field the page sets: its label key and how its value reads. Both read the
# value through celerp_subscriptions.periods.shown (an unset value as the agreement holds it),
# as write_agreement compares it, so the page's controls, the event and History agree.
_FIELD_TEXT = {
    "frequency": ("th.frequency", lambda v: display_enum(v, "subscription_frequency")),
    "custom_interval_days": ("subscriptions.custom_interval_title", lambda v: str(v) if v else None),
    "anchor_date": ("label.first_bill_date", lambda v: v),
    "trial_end": ("subscriptions.trial_end", lambda v: v),
    "start_date": ("label.start_date", lambda v: v),
    "billing_timing": ("subscriptions.billing_timing", lambda v: t(f"subscriptions.timing_{v}")),
    "term_start": ("subscriptions.term_start", lambda v: v),
    "term_end": ("subscriptions.term_end", lambda v: v),
    "cancel_at_period_end": ("subscriptions.stop_at_period_end", lambda v: _yes_no(v is not None)),
    "generation_mode": ("subscriptions.billing_mode", lambda v: t(f"subscriptions.mode_{v}")),
    "auto_send": ("subscriptions.auto_send", _yes_no),
    "catchup_periods": ("subscriptions.catchup_window", lambda v: str(v)),
    RECOGNIZE_FIELD: ("item.recognize_over_service_period", _yes_no),
    "kind": ("subscriptions.kind", lambda v: t(f"subscriptions.kind_{v}")),
    "beneficiary_contact_id": ("subscriptions.member", lambda v: v),
    "grace_days": ("subscriptions.grace_days", lambda v: str(v)),
}
_KINDS = ("subscription", "retainer", "membership")


def _label(field: str) -> str:
    return t(_FIELD_TEXT[field][0])


def _value_text(field: str, value) -> str | None:
    return _FIELD_TEXT[field][1](shown(field, value))


def _term_change(field: str):
    return lambda c: change_text(_label(field), _value_text(field, c.get("old")), _value_text(field, c.get("new")))


def _member_change(c: dict) -> str | None:
    """The member before and after, each by the name the change recorded (else its id)."""
    names = c.get("names") or {}
    return change_text(_label("beneficiary_contact_id"), names.get(c.get("old"), c.get("old")),
                       names.get(c.get("new"), c.get("new")))


def _consents_change(c: dict) -> str:
    old, new = c.get("old") or {}, c.get("new") or {}
    return ", ".join(
        [sentence(f"subscriptions.consent_{m}", date=format_timestamp((new[m] or {}).get("at") or ""))
         for m in _CONSENTS if m in new and new[m] != old.get(m)]
        + [t(f"subscriptions.consent_{m}_withdrawn") for m in _CONSENTS if m in old and m not in new])


def _period_change(old: dict, new: dict, start: str, end: str, ref: str | None = None) -> str | None:
    """What happened to one period: billed, skipped, opened again, held for review, or its
    email; a billed or reopened period names its document."""
    was, now = old.get("state"), new.get("state")
    dates = {"start": start, "end": end}
    if now == "billed" and was != "billed":
        return sentence("subscriptions.history_billed_ref" if ref else "subscriptions.history_billed", ref=ref, **dates)
    if now == "released" and was == "billed" and ref:
        return sentence("subscriptions.history_reopened_ref", ref=ref, **dates)
    if now == "skipped" and was != "skipped":
        return sentence("subscriptions.history_skipped", reason=new.get("reason") or "", **dates)
    if now == "released" and was in ("billed", "skipped"):
        return sentence("subscriptions.done_reopened" if was == "billed" else "subscriptions.done_unskipped", **dates)
    if now == "unresolved" and was != "unresolved":
        return sentence("subscriptions.history_unresolved", **dates)
    send = send_written(new.get("send"))
    if send and new.get("send") != old.get("send"):
        # Sent from the document's own page (by email or marked sent there), which settles it.
        settled = send == "sent" and (new.get("send") or {}).get("via")
        return sentence("subscriptions.history_send_settled" if settled else f"subscriptions.history_send_{send}", **dates)
    return None


def _periods_change(c: dict) -> str | None:
    old, new = c.get("old") or {}, c.get("new") or {}
    if old == new:
        return None
    refs = c.get("refs") or {}
    lines = [_period_change(old.get(k) or {}, new.get(k) or {}, *span, refs.get(k))
             for k, span in sorted((c.get("dates") or {}).items(), key=lambda kv: int(kv[0]))]
    return ", ".join(x for x in lines if x) or t("activity.generic_updated", field=t("subscriptions.periods_heading"))


def _billing_failure_change(c: dict) -> str | None:
    """A failed automatic bill names its period; the failure ending says so."""
    if c.get("drop"):
        return t("subscriptions.history_billing_failure_ended") if c.get("old") else None
    span = (c.get("dates") or {}).get(str((c.get("new") or {}).get("period_index")))
    return sentence("subscriptions.history_billing_failed", start=span[0], end=span[1]) if span \
        else t("subscriptions.notice_billing_failed_title")


def _count_change(label: str):
    """The number of entries after the change; nothing when the number does not change."""
    def describe(c: dict) -> str | None:
        old, new = len(c.get("old") or []), len(c.get("new") or [])
        return None if old == new else f"{t(label)}: {new}"
    return describe


def _amendments_change(c: dict) -> str | None:
    """Each amendment the change added, by the day it takes effect."""
    old = c.get("old") or []
    return ", ".join(sentence("subscriptions.history_amended", day=a.get("effective"))
                     for a in c.get("new") or [] if isinstance(a, dict) and a not in old) or None


register_fields({
    **{f: _term_change(f) for f in _FIELD_TEXT},
    "amendments": _amendments_change,
    "beneficiary_contact_id": _member_change,
    "consents": _consents_change,
    "periods": _periods_change,
    "billing_failure": _billing_failure_change,
    "history_review": _count_change("subscriptions.history_heading"),
    "history_set_aside": _count_change("subscriptions.history_set_aside"),
    "resume_restarts": lambda c: None,
    "released_bills": lambda c: None,
})
# Escape clears an inline preview or result panel (GDR 2j).
_PANEL_ESC_JS = Script("""
document.addEventListener('keydown', function(e) {
  // Only a key the user pressed: a refusal's restore step sends its own Escape.
  if (e.key !== 'Escape' || !e.isTrusted) return;
  document.querySelectorAll('[data-esc-clear]').forEach(function(p) { p.replaceChildren(); });
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


def _sub_status_cards(items: list[dict], state: dict) -> FT:
    counts: dict[str, int] = {}
    for it in items:
        s = it.get("status", "draft")
        counts[s] = counts.get(s, 0) + 1
    base, active_status = _list_base(state), state["status"]
    issued_total = sum(counts.get(s, 0) for s in ("active", "paused", "cancelled"))
    # Draft first, then All Issued, then lifecycle statuses
    cards = [
        {"label": display_enum("draft", "subscription_status"),     "count": counts.get("draft", 0),     "total": None, "status": "draft",     "color": "gray",   "_url": f"{base}&status=draft"},
        {"label": t("status.all_issued"),                           "count": issued_total,               "total": None, "status": "",          "color": "blue",   "_url": base, "_active_key": ""},
        {"label": display_enum("active", "subscription_status"),    "count": counts.get("active", 0),    "total": None, "status": "active",    "color": "green",  "_url": f"{base}&status=active"},
        {"label": display_enum("paused", "subscription_status"),    "count": counts.get("paused", 0),    "total": None, "status": "paused",    "color": "yellow", "_url": f"{base}&status=paused"},
        {"label": display_enum("cancelled", "subscription_status"), "count": counts.get("cancelled", 0), "total": None, "status": "cancelled", "color": "gray",   "_url": f"{base}&status=cancelled"},
    ]
    return Div(status_cards(cards, base, active_status or None, currency=None, show_all_card=False), id="sub-status-cards")


def _data_table(*parts) -> FT:
    """A data table in the scroll box every data table has, so a wide row scrolls inside it
    instead of pushing the page wider than the screen."""
    return Div(Table(*parts, cls="data-table"), cls="table-scroll-wrap")


def _sentence_cell(*content, cls: str = "") -> FT:
    """A cell that can hold a sentence (what happened, why, what to do): it wraps in the room
    its table has (app.css cell--note) instead of pushing the columns after it out of view."""
    return Td(*content, cls=f"cell--note {cls}".rstrip())


def _sub_table(subs: list[dict], state: dict) -> FT:
    if not subs and state["review"]:
        return Div(empty_state_cta(t("subscriptions.review_empty"), t("subscriptions.review_all"),
                                   _list_base(state)), id="sub-table")
    if not subs:
        text = _LIST_TEXT[state["kind"]]
        return Div(empty_state_cta(t(text["empty"]), t(text["new"]), text.get("form") or _new_url(state),
                                   hx_post=not text.get("form")), id="sub-table")

    def _row(s: dict) -> FT:
        eid = s.get("id") or s.get("entity_id", "")
        name = template_label(s, eid)
        contact = s.get("contact_name") or s.get("contact_company_name") or "-"
        freq = display_enum(s["frequency"], "subscription_frequency") if s.get("frequency") else "-"
        # An agreement whose stored terms build no schedule says why where its next bill date would be.
        next_due = (Span(s["schedule_error"], cls="text-danger") if s.get("schedule_error")
                    else s.get("next_bill_date") or "-")
        status = s.get("status", "draft")
        return Tr(
            Td(A(name, href=f"/subscriptions/{eid}")),
            Td(contact),
            Td(freq),
            _sentence_cell(next_due),
            Td(_status_badge(status)),
        )

    return Div(
        _data_table(
            Thead(Tr(Th(t("th.name")), Th(t("th.contact")), Th(t("th.frequency")), Th(t("th.next_due")), Th(t("th.status"), style="text-align:center"))),
            Tbody(*[_row(s) for s in subs]),
        ),
        id="sub-table",
    )


_STANDING_CSS = {"current": "badge--active", "in_grace": "badge--paused", "lapsed": "badge--void",
                 "lifetime": "badge--active"}


def _members_table(rows: list[dict]) -> FT:
    """Members newest first: who, who pays, their standing and the bill or invoice it is read from."""
    if not rows:
        return empty_state_cta(t("subscriptions.members_empty"), t("page.new_membership"),
                               "/subscriptions/new?direction=sales&kind=membership", hx_post=True)

    def _row(m: dict) -> FT:
        source = (f"/subscriptions/{m['entity_id']}" if m.get("entity_id") else f"/docs/{m['doc_id']}")
        return Tr(
            Td(m.get("member_name") or EMPTY),
            Td(m.get("contact_name") or EMPTY),
            Td(Span(display_enum(m["standing"], "member_standing"),
                    cls=f"badge {_STANDING_CSS.get(m['standing'], 'badge--draft')}")),
            Td(m.get("due_date") or EMPTY),
            Td(A(m.get("ref") or source, href=source)),
        )

    return _data_table(
        Thead(Tr(*[Th(t(k)) for k in ("subscriptions.member", "subscriptions.paid_by", "subscriptions.standing",
                                       "th.due_date", "th.ref")])),
        Tbody(*[_row(m) for m in rows]),
    )


def _review_banner(items: list[dict], state: dict) -> FT | str:
    """Names how many agreements wait for history review, with the filter that lists them."""
    n = sum(1 for i in items if i.get("history_review"))
    if not n or state["review"]:
        return ""
    return Div(t("subscriptions.review_banner", n=n), " ",
               A(t("subscriptions.review_open"), href=safe_url(f"{_list_base(state)}&review=1")),
               cls="flash flash--warning")


# ---------------------------------------------------------------------------
# Subscription action bar (replaces standard doc Finalize/Send/Mark-as-Sent)
# ---------------------------------------------------------------------------

def _schedule_section(entity_id: str, sub: dict) -> FT:
    """When the agreement bills: its frequency, first bill date, timing, term and whether it
    stops at the end of the current period. Each change goes through the agreement's schedule,
    which refuses one that would move a period already billed or skipped; the answer draws
    every element showing the schedule again (_schedule_views)."""
    base = f"/subscriptions/{entity_id}/schedule"
    attrs = {**_SCHEDULE_CONTROL, "cls": "form-input form-input--inline"}
    freq = shown("frequency", sub.get("frequency"))
    interval = str(sub.get("custom_interval_days") or "")
    timing = shown("billing_timing", sub.get("billing_timing"))
    stops = sub.get("cancel_at_period_end") is not None

    def _date(field: str) -> FT:
        return Label(_label(field), Input(type="date", name="value", value=sub.get(field) or "",
                                     hx_patch=f"{base}/{field}", **attrs))

    return Div(
        H3(t("subscriptions.schedule_heading")),
        Div(
            # Choosing Custom shows the days box; the days entered choose it.
            Label(_label("frequency"),
                  Select(*[Option(_value_text("frequency", f), value=f, selected=(f == freq))
                           for f in _FREQUENCIES],
                         name="value", hx_patch=f"{base}/frequency", data_days=interval,
                         onchange="document.getElementById('sub-custom-days').style.display ="
                                  " this.value === 'custom' ? '' : 'none'",
                         **{**attrs, "hx_trigger": "change[this.value!=='custom' || this.dataset.days]"})),
            Label(_label("custom_interval_days"),
                  Input(type="number", name="value", value=interval, min="1", hx_patch=f"{base}/custom_interval_days",
                        style="width:80px", **attrs),
                  id="sub-custom-days", style="" if freq == "custom" else "display:none"),
            _date("anchor_date"),
            _date("trial_end"),
            Label(_label("billing_timing"),
                  Select(*[Option(_value_text("billing_timing", m), value=m, selected=(m == timing)) for m in _TIMINGS],
                         name="value", hx_patch=f"{base}/billing_timing", **attrs)),
            _date("term_start"),
            _date("term_end"),
            Label(_label("cancel_at_period_end"),
                  Select(Option(_yes_no(False), value="false", selected=not stops),
                         Option(_yes_no(True), value="true", selected=stops),
                         name="value", hx_patch=f"{base}/cancel_at_period_end", **attrs)),
            style="display:flex;gap:1rem;flex-wrap:wrap;align-items:center",
        ),
        _kind_row(base, sub, attrs),
        id="sub-schedule", cls="mt-lg",
    )


def _kind_row(base: str, sub: dict, attrs: dict) -> FT:
    """What the agreement is (subscription, retainer or membership), the member a membership is
    for and the days after a bill's due date a member stays in good standing. The member is
    picked from every contact by search; choosing none clears it."""
    kind = shown("kind", sub.get("kind"))
    member = sub.get("beneficiary_contact_id") or ""
    members = [("", EMPTY)] + ([(member, sub.get("beneficiary_name") or member)] if member else [])
    return Div(
        Label(_label("kind"),
              Select(*[Option(_value_text("kind", k), value=k, selected=(k == kind)) for k in _KINDS],
                     name="value", hx_patch=f"{base}/kind", **attrs)),
        Label(_label("beneficiary_contact_id"),
              searchable_select("value", members, value=member, aria_label=_label("beneficiary_contact_id"),
                                search_url="/contacts/search-options?contact_type=all&add_new=0",
                                hx_patch=f"{base}/beneficiary_contact_id", **_SCHEDULE_CONTROL)),
        Label(_label("grace_days"),
              Input(type="number", name="value", value=str(shown("grace_days", sub.get("grace_days"))), min="0",
                    hx_patch=f"{base}/grace_days", style="width:80px", **attrs)),
        style="display:flex;gap:1rem;flex-wrap:wrap;align-items:center", cls="mt-sm",
    )


async def _with_member(token: str, sub: dict) -> dict:
    """The agreement with its member's name for the page, else as it is (a member no longer
    found shows by its id)."""
    member = sub.get("beneficiary_contact_id")
    if not member:
        return sub
    try:
        return {**sub, "beneficiary_name": (await api.get_contact(token, member)).get("name")}
    except APIError:
        return sub


async def _with_collection(token: str, entity_id: str, sub: dict) -> dict:
    """A sales agreement with what Celerp Cloud offers for automatic collection and its payer's
    card on file (``collection_view``); as it is when the read fails, so nothing is offered."""
    if doc_kind(sub) != "invoice":
        return sub
    try:
        return {**sub, "collection_view": await api.get_subscription_collection(token, entity_id)}
    except APIError as e:
        api.raise_if_signed_out(e)
        return sub


def _bill_date_note(entity_id: str, sub: dict, next_bill: str | None) -> FT:
    """Shows the first bill date until a period is billed, then the next bill date; for an
    agreement that has ended, why it bills nothing, and on a void one a link to its document's
    page, where Unvoid is (every refusal on a void agreement names it)."""
    billed = any((e or {}).get("state") == "billed" for e in (sub.get("periods") or {}).values())
    attrs = {"id": "sub-bill-note", "cls": "text-muted", "style": "font-size:.85em"}
    if sub.get("ended"):
        unvoid = (" ", A(t("doc.unvoid"), href=f"/docs/{entity_id}")) if sub.get("status") == "void" else ()
        return Span(sub["ended"], *unvoid, **attrs)
    if not billed and (first := next_bill or sub.get("anchor_date")):
        return Span(sentence("subscriptions.first_bill_on", date=first), **attrs)
    if next_bill:
        return Span(sentence("subscriptions.next_bill_on", date=next_bill), **attrs)
    return Span(**attrs)


def _with_schedule(sub: dict, schedule: dict) -> dict:
    """The agreement as its page shows it: its own fields, its periods' next bill date, and why
    it bills nothing once it has ended."""
    return {**sub, "next_bill_date": schedule.get("next_bill_date"), "not_moved": schedule.get("not_moved"),
            "ended": schedule.get("ended")}


def _action_groups(entity_id: str, sub: dict, schedule: dict) -> tuple[FT, FT]:
    """The agreement's actions beside the document's own, each side in a box with its id."""
    left, right = _sub_action_controls(entity_id, sub, sub["next_bill_date"], schedule.get("oldest_open_index"))
    return (Span(*left, id="sub-actions-left", style="display:contents"),
            Span(*right, id="sub-actions-right", style="display:contents"))


def _defers(settings: dict, sub: dict) -> bool:
    """Whether the agreement's bills can recognize over their service period: a sales
    agreement in a company that keeps its books."""
    return doc_kind(sub) == "invoice" and module_active(settings, "celerp-accounting")


def _stored_schedule_views(entity_id: str, sub: dict, schedule: dict, defers: bool) -> str:
    """Every element showing the schedule, drawn from what the agreement stores, each swapped
    out of band by its id."""
    return to_markup(tuple(v(hx_swap_oob="true") for v in _schedule_views(entity_id, sub, schedule, defers) if v != ""))


def _schedule_views(entity_id: str, sub: dict, schedule: dict, defers: bool) -> list[FT]:
    """Every element of the agreement page that shows its schedule or billing settings: the
    actions with the bill date note and badge, the header's frequency and next bill date, the
    Schedule and Billing sections and the Periods table. The page draws each in its place and a
    schedule save answers with all of them, so none goes on showing the schedule as it was."""
    from ui.routes.documents import subscription_meta_cells

    sub = _with_schedule(sub, schedule)
    return [*_action_groups(entity_id, sub, schedule), *subscription_meta_cells(sub),
            _schedule_section(entity_id, sub) if sub.get("status") not in ENDED else "",
            _billing_section(entity_id, sub, defers), _periods_table(entity_id, schedule)]


def _pause_button(entity_id: str):
    return Button(t("btn.pause"), cls="btn btn--warning btn--sm", hx_post=f"/subscriptions/{entity_id}/pause", hx_swap="none")


def _cancel_button(entity_id: str):
    return Button(t("btn.cancel"), cls="btn btn--danger btn--sm", hx_post=f"/subscriptions/{entity_id}/cancel", hx_swap="none")


def _sub_action_controls(entity_id: str, sub: dict, next_bill: str | None = None,
                         period_index: int | None = None) -> tuple[list, list]:
    """Return (left_actions, right_actions) for the subscription detail page.

    Draft:   left=[Activate, note]                                        right=[]
    Active:  left=[badge, GenerateDue, BillEarly, note]                   right=[Pause, Cancel]
    Paused:  left=[badge, note]                                           right=[restart note, Resume, Cancel]
    Cancelled or void: left=[badge, why it bills nothing]                 right=[]
    Not on a schedule yet: left=[badge, why]                              right=[Pause, Cancel]

    The schedule itself is set in the Schedule section below the document. A draft's lines are
    still editable, so Activate saves them first, as Finalize does (persist_then_post).
    """
    status = sub.get("status", "draft")
    note = _bill_date_note(entity_id, sub, next_bill)
    if sub.get("not_moved") and status != "draft":
        # A template from an earlier release that is not on a schedule yet: say why. The
        # Schedule section's frequency and interval move it onto one.
        warning = Span(t("subscriptions.err_not_moved", ref=sub.get("ref_id") or entity_id, reason=sub["not_moved"]),
                       cls="flash flash--warning")
        stops = [_pause_button(entity_id)] if status == "active" else []
        if status not in ENDED:
            stops.append(_cancel_button(entity_id))
        return [_status_badge(status), warning], stops

    bill_fields = {"period_index": period_index} if period_index is not None else {}

    if status == "draft":
        return [
            A(t("subscriptions.set_schedule"), href="#sub-schedule", cls="btn btn--secondary"),
            Button(t("btn.activate"), cls="btn btn--primary", onclick=as_js(persist_then_post(f"/subscriptions/{entity_id}/activate")),
                   title=t("subscriptions.activate_title")),
            note,
        ], []

    left: list = [_status_badge(status), _auto_badge(sub)]
    right: list = []

    if status == "active":
        left.extend([
            Button(t("btn.generate_due"), cls="btn btn--secondary btn--sm", hx_post=f"/subscriptions/{entity_id}/generate-due",
                   hx_swap="none", hx_vals=operation_key_vals(**bill_fields)),
            Button(t("btn.bill_early"), cls="btn btn--secondary btn--sm", hx_post=f"/subscriptions/{entity_id}/bill-early",
                   hx_swap="none", hx_vals=operation_key_vals(**bill_fields)),
        ])
        right.append(_pause_button(entity_id))
    elif status == "paused":
        if sub.get("resume_restarts"):
            right.append(Span(t("subscriptions.resume_restarts_note"), cls="text-muted", style="font-size:.85em"))
        right.append(
            Button(t("btn.resume"), cls="btn btn--success btn--sm", hx_post=f"/subscriptions/{entity_id}/resume", hx_swap="none")
        )

    if status not in (*ENDED, "draft"):
        right.append(_cancel_button(entity_id))

    left.append(note)
    gen_count = sum(1 for e in (sub.get("periods") or {}).values() if (e or {}).get("state") == "billed")
    if gen_count:
        left.append(
            Span(t("subscriptions.docs_generated", n=gen_count), cls="text-muted", style="font-size:.85em")
        )

    return left, right


def _period_actions(entity_id: str, row: dict) -> FT:
    """Skip an open period with a reason; un-skip a skipped one; reopen a billed one whose
    document no longer bills it (void, or back in draft), or send its email when it has not
    gone. The server refuses any that does not apply and says why."""
    k, state = row["period_index"], row.get("state")
    base = f"/subscriptions/{entity_id}/periods/{k}"
    if state == "skipped":
        return Button(t("btn.unskip"), cls="btn btn--secondary btn--sm", hx_post=f"{base}/unskip", hx_swap="none")
    if state == "billed":
        reopen = Button(t("btn.reopen"), cls="btn btn--secondary btn--sm", hx_post=f"{base}/reopen", hx_swap="none",
                        title=t("subscriptions.reopen_title"))
        send = (_send_button(f"{base}/send", row["send"], bool(row.get("again")), row["attempt"])
                if row.get("send") in _SEND_BADGE else "")
        withdraw = (Button(t("subscriptions.history_withdraw"), cls="btn btn--secondary btn--sm", hx_swap="none",
                           hx_post=f"/subscriptions/{entity_id}/history/{row['doc_id']}/withdraw")
                    if row.get("history") else "")
        return Div(send, withdraw, reopen, style="display:flex;gap:.25rem")
    if state == "unresolved":
        return A(t("subscriptions.review_open"), href=f"/subscriptions/{entity_id}#history-review")
    return Form(
        Input(type="text", name="reason", placeholder=t("label.reason"), cls="form-input form-input--inline",
              style="width:160px", required=True),
        Button(t("btn.skip"), type="submit", cls="btn btn--secondary btn--sm"),
        hx_post=f"{base}/skip", hx_swap="none", style="display:flex;gap:.25rem",
    )


def _periods_table(entity_id: str, schedule: dict, *, historical_page: bool = False) -> FT:
    """The agreement's periods (``schedule`` is the API's period list), newest first: dates,
    status (with a reopened period and an email not sent, failed or unconfirmed marked), the document
    that billed it and any earlier ones it let go."""
    rows, oldest_due_row = schedule.get("rows") or [], schedule.get("oldest_due_row")
    older_before = schedule.get("older_before")
    attrs = {"id": "sub-periods", "cls": "mt-lg"}
    if not rows and oldest_due_row is None and not historical_page:
        return Div(**attrs)

    def _row(r: dict) -> FT:
        status = r["status"]
        text = t(f"subscriptions.period_{status}")
        label = Span(Span(text, cls=f"badge {_PERIOD_BADGE[status]}") if status in _PERIOD_BADGE else text)
        if r.get("reopened"):
            label = Span(label, " ", Span(t("subscriptions.period_reopened"), cls="badge badge--draft"))
        if r.get("send") in _SEND_BADGE:
            key, badge = _SEND_BADGE[r["send"]]
            label = Span(label, " ", Span(t(key), cls=f"badge {badge}"))
        doc = A(r["ref_id"], href=f"/docs/{r['doc_id']}") if r.get("doc_id") else (r.get("reason") or "--")
        if r.get("released"):
            earlier: list = []
            for d in r["released"]:
                earlier += [", "] if earlier else []
                earlier.append(A(d["ref_id"], href=f"/docs/{d['doc_id']}"))
            doc = Div(doc, Div(t("subscriptions.period_earlier_bills"), " ", *earlier, cls="text-muted"))
        return Tr(Td(f"{r['service_start']} - {r['service_end']}"), Td(r["bill_date"]), Td(label), _sentence_cell(doc),
                  Td(_period_actions(entity_id, r)))

    return Div(
        H3(t("subscriptions.periods_heading")),
        Div(P(t("th.next_due"), cls="text-muted"),
            _data_table(Tbody(_row(oldest_due_row)))) if oldest_due_row else "",
        _data_table(
            Thead(Tr(Th(t("th.period")), Th(t("th.bill_date")), Th(t("th.status")), Th(t("th.document")), Th(""))),
            Tbody(*[_row(r) for r in rows]),
        ),
        Div(*([A(t("btn.back"), href=f"/subscriptions/{entity_id}")]
               if historical_page else []),
            *([A(t("btn.next"),
                 href=f"/subscriptions/{entity_id}?period_before={older_before}")]
              if older_before is not None else []),
            cls="form-actions"),
        **attrs,
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
                             sent=counts.get("sent", 0), failed=_to_check(counts)), " ",
                           A(t("subscriptions.summary_open"), href="/subscriptions/summary"),
                           cls="flash flash--warning" if _to_check(counts) else "flash flash--info"))
    return banners


def _sub_amount(sub: dict) -> FT:
    return Td(fmt_money(float(sub.get("total") or 0), sub.get("currency")), cls="cell--number")


def _period_text(p: dict) -> str:
    return f"{p['service_start']} - {p['service_end']}"


# A bulk table's selection posts as ONE field, the ticked rows as a JSON list (the row boxes carry
# no name), so a page of any size stays under the form parser's 1000-field cap. Set as each request
# is built: the page's script policy runs no hx-vals expression.
_CHOSEN_ROWS_JS = """
(function(){
  if(window.__celerpChosenRows)return;window.__celerpChosenRows=true;
  document.addEventListener('htmx:configRequest',function(e){
    var f=e.detail.elt.closest('form[data-chosen-rows]');if(!f)return;
    e.detail.parameters.item=JSON.stringify(Array.prototype.map.call(
      f.querySelectorAll('.bulk-select:checked'),function(box){return box.value;}));});
})();
"""


_QUANTITIES_JS = """
(function(){
  if(window.__celerpQuantities)return;window.__celerpQuantities=true;
  document.addEventListener('htmx:configRequest',function(e){
    var f=e.detail.elt.closest('form[data-quantities]');if(!f)return;var q={};
    f.querySelectorAll('.amend-qty').forEach(function(box){q[box.dataset.line]=box.value;});
    e.detail.parameters.quantities=JSON.stringify(q);});
})();
"""


def _bulk_form(*children, **attrs) -> FT:
    """A form over a bulk table: its ticked rows post as the one ``item`` field."""
    return Form(*children, Script(_CHOSEN_ROWS_JS), data_chosen_rows=True, **attrs)


def _row_box(value: str, checked: bool = True) -> FT:
    """A row checkbox of a table that bills several agreements at once, ticked until a run has
    acted on the table, so the next click never bills rows nobody chose. Its _bulk_form posts
    the ticked ones."""
    return Td(Input(type="checkbox", value=value, checked=checked, cls="bulk-select"), cls="col-checkbox")


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
        return Div(P(A(sentence("subscriptions.due_empty", date=day, name=template_label(subs[eid], eid)),
                       href=f"/subscriptions/{eid}"), cls="hint"), cls="settings-card")
    items.sort(key=lambda i: i["periods"][0]["bill_date"])

    def _row(i: dict) -> FT:
        eid, first, more = i["entity_id"], i["periods"][0], len(i["periods"]) - 1
        sub = subs[eid]
        return Tr(
            _row_box(f"{eid}|{first['period_index']}"),
            Td(A(template_label(sub, eid), href=f"/subscriptions/{eid}")),
            Td(sub.get("contact_name") or "--"),
            Td(_period_text(first)),
            Td(first["bill_date"]),
            Td(str(more), cls="cell--number"),
            _sub_amount(sub),
            cls="data-row",
        )

    return _bulk_form(
        operation_key_input(),
        P(t("subscriptions.due_hint", n=len(items)), cls="text-muted"),
        _data_table(Thead(Tr(select_all_th(), Th(t("th.name")), Th(t("th.contact")), Th(t("th.period")), Th(t("th.bill_date")),
                       Th(t("subscriptions.th_more_due"), cls="cell--number"), Th(t("th.total"), cls="cell--number"))),
              Tbody(*[_row(i) for i in items])),
        Div(Button(t("btn.generate_due"), type="submit", cls="btn btn--primary btn--sm"),
            Button(t("btn.close"), type="button", cls="btn btn--ghost btn--sm",
                   onclick="document.getElementById('sub-due-panel').replaceChildren()"),
            cls="form-actions"),
        hx_post="/subscriptions/generate-due", hx_target="#sub-due-panel", hx_swap="innerHTML",
        cls="settings-card",
    )


def _billed_text(done: dict) -> str:
    """What billing a period did: the document and the dates it bills."""
    return sentence("subscriptions.done_already_billed" if done.get("already_billed") else "subscriptions.done_billed",
                    ref=done.get("ref_id") or done.get("doc_id") or "", start=done["service_start"], end=done["service_end"])


def _outcome_table(results: list[dict], subs: dict[str, dict], retry_key: str | None = None) -> FT:
    """What a bulk run did, one row per item: the agreement, then the document it billed or the
    period it skipped with the dates, or why it was refused, with the period's dates when the
    answer gives them. With *retry_key* (Generate all due) a refused row offers Retry."""
    def _row(r: dict) -> FT:
        eid = r["entity_id"]
        name = template_label(subs[eid], eid) if eid in subs else eid
        action = Td("")
        if not r.get("ok"):
            reason = refusal_text(r.get("error"))
            outcome = _sentence_cell(sentence("subscriptions.done_refused", start=r["service_start"], end=r["service_end"],
                                              reason=reason) if r.get("service_start") else reason, cls="text-danger")
            if retry_key is not None:
                action = Td(Button(t("btn.retry"), cls="btn btn--secondary btn--sm", hx_post="/subscriptions/generate-due",
                                   hx_vals={"item": json.dumps([f"{eid}|{r['period_index']}"]), FIELD: retry_key},
                                   hx_target="#sub-due-panel", hx_swap="innerHTML"))
        elif r.get("skipped"):
            outcome = _sentence_cell(sentence("subscriptions.done_skipped", start=r["service_start"], end=r["service_end"]))
        else:
            outcome = _sentence_cell(A(_billed_text(r), href=f"/docs/{r['doc_id']}"))
        return Tr(Td(A(name, href=f"/subscriptions/{eid}")), outcome, action)

    return _data_table(Thead(Tr(Th(t("th.name")), Th(t("th.status")), Th(""))), Tbody(*[_row(r) for r in results]))


def _due_results(result: dict, subs: dict[str, dict], request_key: str) -> FT:
    """Generate all due's answer: the count, then what it did for each agreement. The list
    stays until it is closed."""
    return Div(
        P(t("subscriptions.due_result", billed=result.get("billed", 0), refused=result.get("refused", 0))),
        P(t("subscriptions.due_already", n=result["already_billed"]), cls="text-muted") if result.get("already_billed") else "",
        _outcome_table(result.get("results") or [], subs, request_key),
        Div(Button(t("btn.close"), type="button", cls="btn btn--ghost btn--sm",
                   onclick="document.getElementById('sub-due-panel').replaceChildren()"), cls="form-actions"),
        cls="settings-card",
    )


def _catch_up_card(items: list[dict], subs: dict[str, dict], results: list[dict] | None = None) -> FT:
    """The periods automatic billing held back, each agreement's oldest first: they bill in
    that order, so the top row of an agreement is the one billed next. Bill the chosen
    periods, or skip them with a reason; a refused one says why and the rest go ahead."""
    notes = [_outcome_table(results, subs)] if results else []
    if not items:
        return Div(*notes, empty_state_cta(t("subscriptions.catchup_empty"), t("subscriptions.review_all"),
                                           "/subscriptions?direction=sales"), id="catchup-card")

    rows = []
    for i in items:
        eid = i["entity_id"]
        sub = subs.get(eid) or {}
        for p in i["periods"]:
            rows.append(Tr(
                _row_box(f"{eid}|{p['period_index']}", checked=results is None),
                Td(A(template_label(sub, eid), href=f"/subscriptions/{eid}")),
                Td(_period_text(p)),
                Td(p["bill_date"]),
                _sub_amount(sub),
                cls="data-row",
            ))
    return Div(
        *notes,
        P(t("subscriptions.catchup_hint"), cls="text-muted"),
        _bulk_form(
            operation_key_input(),
            _data_table(Thead(Tr(select_all_th(), Th(t("th.name")), Th(t("th.period")), Th(t("th.bill_date")),
                           Th(t("th.total"), cls="cell--number"))),
                  Tbody(*rows)),
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


# Send failures fixed on the Web Access page: no email service at all, or Celerp Cloud refusing it.
_FIXED_IN_WEB_ACCESS = ("email.err_no_transport", "email.err_relay_refused")


def _summary_table(rows: list[dict], subs: dict[str, dict]) -> FT:
    """What ran unattended, newest first, each row linked to its agreement and document;
    an email not sent offers Send, Retry or Send again beside what happened to it, and a failed
    bill Generate due, so the action is in view without scrolling. A failure the email settings
    fix links to that page."""
    def _row(r: dict) -> FT:
        eid = r["entity_id"]
        doc = A(r.get("ref_id") or r["doc_id"], href=f"/docs/{r['doc_id']}") if r.get("doc_id") else "--"
        retry = (_send_button(r["retry_url"], r.get("send"), bool(r.get("again")), r["attempt"], back="summary")
                 if r.get("retry_url") else "")
        if r["kind"] == "generation_failed":
            fields = {"period_index": r["period_index"]} if r.get("period_index") is not None else {}
            retry = Button(t("btn.generate_due"), cls="btn btn--secondary btn--sm", hx_swap="none",
                           hx_post=f"/subscriptions/{eid}/generate-due", hx_vals=operation_key_vals(**fields))
        fix = (A(t("doc.send_relay_error_link"), href="/settings/cloud")
               if r.get("error_key") in _FIXED_IN_WEB_ACCESS else "")
        return Tr(Td(format_timestamp(r.get("at") or "") or "--"),
                  Td(A(template_label(subs.get(eid) or {}, eid), href=f"/subscriptions/{eid}")),
                  _sentence_cell(r.get("message") or "", " " if fix else "", fix,
                                 Div(retry, cls="mt-xs") if retry else ""), Td(doc))

    return _data_table(Thead(Tr(Th(t("th.date")), Th(t("th.name")), Th(t("th.description")), Th(t("th.document")))),
                 Tbody(*[_row(r) for r in rows]))


def _auto_badge(sub: dict) -> FT:
    """The header badge naming an agreement that bills on its own; empty otherwise."""
    key = _MODE_BADGE.get(sub.get("generation_mode") or "")
    attrs = {"id": "sub-auto-badge"}
    return Span(t(key), cls="badge badge--active", **attrs) if key else Span(**attrs)


def _yes_no_control(base: str, field: str, on: bool, attrs: dict) -> FT:
    return Label(_label(field),
                 Select(Option(_yes_no(False), value="false", selected=not on),
                        Option(_yes_no(True), value="true", selected=on),
                        name="value", hx_patch=f"{base}/{field}", **attrs))


def _billing_section(entity_id: str, sub: dict, defers: bool) -> FT:
    """How the agreement bills: by hand or on its own, whether it sends what it bills, how
    many past periods automatic billing may still bill after a gap and, when its bills can
    defer revenue (_defers), whether they recognize over their service period. Each
    automatic mode names when it was authorized; setting it back to manual undoes it."""
    base = f"/subscriptions/{entity_id}/schedule"
    attrs = {**_SCHEDULE_CONTROL, "cls": "form-input form-input--inline"}
    mode = shown("generation_mode", sub.get("generation_mode"))
    consents = sub.get("consents") or {}
    offered = bool((sub.get("collection_view") or {}).get("offered"))
    # Collect is offered while Cloud offers it, and stays shown on an agreement using it so it can be set back.
    modes = (*_MODES, COLLECT_MODE) if offered or mode == COLLECT_MODE else _MODES
    return Div(
        H3(t("subscriptions.billing_heading")),
        Div(
            Label(_label("generation_mode"),
                  Select(*[Option(_value_text("generation_mode", m), value=m, selected=(m == mode)) for m in modes],
                         name="value", hx_patch=f"{base}/generation_mode", **attrs)),
            _yes_no_control(base, "auto_send", bool(sub.get("auto_send")), attrs),
            Label(_label("catchup_periods"),
                  Input(type="number", name="value", min="0", max="12", value=str(shown("catchup_periods", sub.get("catchup_periods"))),
                        hx_patch=f"{base}/catchup_periods", style="width:80px", **attrs),
                  title=t("subscriptions.catchup_window_title")),
            _yes_no_control(base, RECOGNIZE_FIELD, bool(sub.get(RECOGNIZE_FIELD)), attrs) if defers else "",
            style="display:flex;gap:1rem;flex-wrap:wrap;align-items:center",
        ),
        *[P(sentence(f"subscriptions.consent_{c}", date=format_timestamp(consents[c].get("at") or "")), cls="text-muted")
          for c in _CONSENTS if (consents.get(c) or {}).get("at")],
        _collection_card(entity_id, sub["collection_view"]) if offered else "",
        id="sub-billing", cls="mt-lg",
    )


def _card_text(binding: dict | None) -> str:
    """The payer's card on file as the agreement page reads it."""
    state = (binding or {}).get("state")
    if state == "active":
        brand = str(binding.get("card_brand") or "").replace("_", " ").title() or EMPTY
        return t("subscriptions.card_on_file", brand=brand, last4=binding.get("card_last4") or EMPTY)
    if state == "requires_action":
        return t("subscriptions.card_needs_action")
    if state == "access_removed":
        return t("subscriptions.card_access_removed")
    return t("subscriptions.no_card_on_file")


def _collection_button(entity_id: str, label: str, action: str, fields: dict, **attrs) -> FT:
    return Form(*[Input(type="hidden", name=k, value=v) for k, v in fields.items()],
                Button(label, type="submit", cls="btn btn--secondary btn--sm", **attrs),
                hx_post=f"/subscriptions/{entity_id}/collection/{action}", hx_target="#sub-collection",
                hx_swap="outerHTML", cls="inline-form")


def _setup_link_text(link: str) -> FT:
    """A setup link to send the payer, ready to copy."""
    return Div(P(t("subscriptions.setup_link_copy"), cls="text-muted"),
               Input(type="text", value=link, readonly=True, cls="form-input", aria_label=t("subscriptions.setup_link_copy")))


def _collection_card(entity_id: str, view: dict, link: str | None = None) -> FT:
    """The payer's card on file for automatic collection: what Cloud last said of it and, when its
    receipt was not emailed, so; Email or Copy a setup link where the payer puts a card on file
    (``link``: the one just made, to copy), Email receipt again and Remove card for one on file.
    A removed card comes back only by the payer putting a card on file again from a new link."""
    binding = view.get("binding") or {}
    active = binding.get("state") == "active"
    return Div(
        H4(t("subscriptions.collection_heading")),
        P(_card_text(binding)),
        P(t("subscriptions.receipt_not_sent"), cls="text-muted") if active and binding.get("receipt_sent") is False else "",
        Div(_collection_button(entity_id, t("subscriptions.btn_email_setup_link"), "setup-link", {"delivery": "email"}),
            _collection_button(entity_id, t("subscriptions.btn_copy_setup_link"), "setup-link", {"delivery": "copy"}),
            *([_collection_button(entity_id, t("subscriptions.btn_email_receipt"), "setup-link",
                                  {"delivery": "email", "resend_receipt": "true"}),
               _collection_button(entity_id, t("subscriptions.btn_remove_card"), "revoke", {},
                                  hx_confirm=t("subscriptions.remove_card_confirm"))] if active else []),
            style="display:flex;gap:0.5rem;flex-wrap:wrap"),
        _setup_link_text(link) if link else "",
        id="sub-collection", cls="mt-md",
    )


def _quantity_text(value) -> str:
    return EMPTY if value is None else f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _quantities_section(entity_id: str, sub: dict, today) -> FT | str:
    """Each line's quantity in force today, a box for its new quantity and the date the change
    takes effect (today unless chosen), and the changes made so far, newest first, each with the
    documents that settled the days already billed. The boxes post as the one ``quantities``
    field (a JSON object of line id to quantity), so a long agreement stays under the form's
    field limit. A change is undone by changing the quantity
    back from the same date."""
    lines = [li for li in lines_on(sub, today) if li.get("line_id")]
    if not lines:
        return ""
    by_id = {li["line_id"]: li.get("description") or EMPTY for li in lines}

    def _line_row(li: dict) -> FT:
        qty = _quantity_text(li.get("quantity"))
        return Tr(Td(li.get("description") or EMPTY), Td(qty, cls="cell--number"),
                  Td(Input(type="number", value=qty, min="0", step="any", data_line=li["line_id"],
                           cls="form-input form-input--inline amend-qty", style="width:90px"), cls="cell--number"))

    def _change_row(a: dict) -> FT:
        shown_lines = ", ".join(f"{by_id.get(li.get('line_id'), li.get('description') or EMPTY)}: "
                                f"{_quantity_text(li.get('quantity'))}" for li in a.get("line_items") or [])
        docs = [A(d.removeprefix("doc:"), href=f"/docs/{d}") for d in (a.get("credit_note_id"), a.get("invoice_id")) if d]
        return Tr(Td(sentence("subscriptions.amend_from", day=a.get("effective"))), Td(shown_lines or EMPTY),
                  _sentence_cell(*docs) if docs else Td(EMPTY))

    changes = list(reversed(amendments_of(sub)))
    return Div(
        H3(t("subscriptions.quantities_heading")),
        P(t("subscriptions.quantities_hint"), cls="text-muted"),
        Form(
            operation_key_input(),
            _data_table(Thead(Tr(Th(t("th.description")), Th(t("subscriptions.th_quantity_now"), cls="cell--number"),
                                 Th(t("subscriptions.th_quantity_new"), cls="cell--number"))),
                        Tbody(*[_line_row(li) for li in lines])),
            Div(Label(t("subscriptions.amend_effective"),
                      Input(type="date", name="effective", value=today.isoformat(), required=True,
                            cls="form-input form-input--inline")),
                Button(t("btn.change_quantities"), type="submit", cls="btn btn--primary btn--sm"),
                cls="form-actions"),
            Script(_QUANTITIES_JS),
            hx_post=f"/subscriptions/{entity_id}/amend", hx_swap="none", data_quantities=True,
        ),
        _data_table(Thead(Tr(Th(t("th.date")), Th(t("th.quantity")), Th(t("th.document")))),
                    Tbody(*[_change_row(a) for a in changes])) if changes else "",
        id="sub-quantities", cls="mt-lg",
    )


def _history_review(entity_id: str, review: list[dict], set_aside: list[dict]) -> FT | str:
    """Earlier documents that could not be matched to a period on their own: place each in
    the period it billed, or set it aside when it billed no period of its own. Placing one
    can be withdrawn from the Periods table; a set-aside one returns to review from here."""
    if not review and not set_aside:
        return ""

    def _doc(r: dict) -> FT:
        return A(r.get("ref_id") or r["doc_id"], href=f"/docs/{r['doc_id']}")

    def _action(label: str, doc_id: str, action: str, **attrs) -> FT:
        return Button(label, cls="btn btn--secondary btn--sm", hx_swap="none",
                      hx_post=f"/subscriptions/{entity_id}/history/{doc_id}/{action}", **attrs)

    def _row(r: dict) -> FT:
        doc_id = r["doc_id"]
        places = [_action(t("subscriptions.history_place", period=_period_text(c)), doc_id, "confirm",
                          hx_vals={"period_index": c["period_index"]})
                  for c in r.get("candidates") or []]
        aside = _action(t("subscriptions.history_set_aside"), doc_id, "set-aside",
                        title=t("subscriptions.history_set_aside_title"))
        note = [] if places else [Span(t("subscriptions.history_no_period"), cls="text-muted")]
        return Tr(Td(_doc(r)), _sentence_cell(*note, *places, aside))

    def _aside_row(r: dict) -> FT:
        return Tr(Td(_doc(r)), _sentence_cell(Span(t("subscriptions.history_set_aside_row"), cls="text-muted"),
                                              _action(t("subscriptions.history_return"), r["doc_id"], "withdraw")))

    return Div(
        H3(t("subscriptions.history_heading")),
        P(t("subscriptions.history_hint"), cls="text-muted"),
        _data_table(Thead(Tr(Th(t("th.document")), Th(t("th.period")))),
              Tbody(*[_row(r) for r in review], *[_aside_row(r) for r in set_aside])),
        id="history-review", cls="mt-lg",
    )


# ---------------------------------------------------------------------------
# Route setup
# ---------------------------------------------------------------------------

def _posted_list(raw) -> object:
    """A list posted as one JSON field (Mark as read's keys, a bulk table's chosen rows), so a
    page of any size is one field; decoded, or the raw value when it is not JSON."""
    try:
        return json.loads(raw or "")
    except ValueError:
        return raw


def _to_check(counts: dict) -> int:
    """Rows someone may need to act on (summary.NEEDS_YOU): a failed bill, a lapsed authorization,
    and a send that failed, was cut off, is with the mail server or still waits to go out."""
    return sum(counts.get(k, 0) for k in NEEDS_YOU)


async def _held_and_unread(token: str) -> tuple[list[dict], dict, list[str]]:
    """Optional banners never silently report an unavailable service as empty."""
    errors = []
    try:
        catch_up = (await api.subscriptions_catch_up(token)).get("items") or []
    except APIError as exc:
        api.raise_if_signed_out(exc)
        catch_up = []
        errors.append(error_message(exc))
    try:
        summary = await api.subscriptions_summary(token)
    except APIError as exc:
        api.raise_if_signed_out(exc)
        summary = {}
        errors.append(error_message(exc))
    return catch_up, summary, errors


async def _all_agreements(token: str, direction: str | None = None, *, review: bool = False,
                          kind: str = "") -> list[dict]:
    """Read every server page, without silently truncating large agreement sets."""
    items: list[dict] = []
    offset = 0
    while True:
        params = {"limit": 1000, "offset": offset, **({"direction": direction} if direction else {}),
                  **({"review": "true"} if review else {}), **({"kind": kind} if kind else {})}
        resp = await api.list_subscriptions(token, params)
        batch = resp if isinstance(resp, list) else resp.get("items", [])
        items.extend(batch)
        offset += len(batch)
        if not batch or len(batch) < 1000 or (isinstance(resp, dict) and offset >= resp.get("total", 0)):
            return items


# The pages that list agreements, by the kind they list ("" lists every kind), and their words.
# Members lists standing, not agreements, so it has its own page.
# Subscribers makes its agreements through its New subscriber form (_subscriber_form), so its New
# button and empty state lead there.
_LIST_PATHS = {"": "/subscriptions", "retainer": "/retainers", "subscription": "/subscribers"}
_LIST_TEXT = {"": {"new": "page.new_subscription", "empty": "label.no_subscription_templates_found"},
              "retainer": {"new": "page.new_retainer", "empty": "subscriptions.retainers_empty", "title": "nav.retainers"},
              "subscription": {"new": "page.new_subscriber", "empty": "subscriptions.subscribers_empty",
                               "title": "nav.subscribers", "form": "#new-subscriber"}}
# A SaaS plan is a service item in this category; a subscriber's seats are its line's quantity.
PLAN_CATEGORY = "saas_plan"


def _list_state(params, path: str = "/subscriptions") -> dict:
    """What a list view of agreements shows, from its path and query params: the list page's URL,
    or the page an action was taken from."""
    try:
        page = max(int(params.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    kind = next(k for k, p in _LIST_PATHS.items() if p == path)
    return {"direction": "sales" if kind else params.get("direction") or "sales", "kind": kind,
            "status": params.get("status", ""), "q": params.get("q", ""),
            "review": params.get("review", "") in {"1", "true"}, "page": page,
            "per_page": per_page_value(params.get("per_page"), _PER_PAGE)}


def _list_base(state: dict) -> str:
    """The list page's own URL, without its status, search or page."""
    return f"{_LIST_PATHS[state['kind']]}?direction={state['direction']}"


def _new_url(state: dict) -> str:
    """Where the list page's New button posts: a draft of the kind the page lists."""
    return f"/subscriptions/new?{urlencode({'direction': state['direction'], **({'kind': state['kind']} if state['kind'] else {})})}"


def _new_button(state: dict, cls: str) -> FT:
    """The list page's New action: a link to its form when it has one, else a blank draft."""
    text = _LIST_TEXT[state["kind"]]
    if text.get("form"):
        return A(t(text["new"]), href=safe_url(text["form"]), cls=cls)
    return Button(t(text["new"]), hx_post=_new_url(state), hx_swap="none", cls=cls)


async def _plans(token: str) -> list[dict]:
    """Every SaaS plan item, read page by page so none is left out."""
    items: list[dict] = []
    while True:
        resp = await api.list_items(token, {"category": PLAN_CATEGORY, "status": "all", "limit": 500,
                                            "offset": len(items)})
        batch = resp.get("items", [])
        items.extend(batch)
        if len(batch) < 500 or len(items) >= resp.get("total", 0):
            return items


def _subscriber_form(plans: list[dict], state: dict) -> FT:
    """New subscriber: the contact, the plan, its seats and an optional trial end, which is the
    first bill date."""
    label = lambda key, field: Label(t(key), field, cls="form-label")
    return Form(
        H3(t("page.new_subscriber")),
        Div(
            label("th.contact", searchable_select("contact_id", [], aria_label=t("th.contact"),
                                                  search_url="/contacts/search-options?contact_type=customer&add_new=0")),
            label("subscriptions.plan", searchable_select(
                "plan_id", [(p.get("id") or p.get("entity_id", ""), p.get("name") or p.get("sku") or "") for p in plans],
                aria_label=t("subscriptions.plan"))),
            label("subscriptions.seats", Input(type="number", name="seats", value="1", min="1", step="1",
                                               cls="form-input", style="width:80px")),
            label("subscriptions.trial_end", Input(type="date", name="trial_end", cls="form-input")),
            Button(t("page.new_subscriber"), type="submit", cls="btn btn--primary"),
            style="display:flex;gap:1rem;flex-wrap:wrap;align-items:flex-end",
        ),
        P(t("subscriptions.new_subscriber_hint"), cls="text-muted"),
        id="new-subscriber", cls="card mt-sm", hx_post=_new_url(state), hx_swap="none",
    )


async def _subscriber_draft(token: str, form) -> dict | str:
    """The draft New subscriber makes: the plan as its one line at the plan's price on the
    company's default list, the seats as its quantity, and the trial end as its first bill
    date. Why not, worded for the reader, when the seats are not a whole number of at least one
    or the item is not a SaaS plan."""
    seats = (form.get("seats") or "").strip()
    if not seats.isdigit() or int(seats) < 1:
        return t("subscriptions.err_seats", seats=seats or EMPTY)
    plan_id = form.get("plan_id") or ""
    plan = await api.get_item(token, plan_id) if plan_id else {}
    if plan.get("category") != PLAN_CATEGORY:
        return t("subscriptions.err_not_plan", name=plan.get("name") or plan_id or EMPTY)
    from celerp.services.pricing import resolve_price

    line = {"entity_id": plan_id, "sku": plan.get("sku"), "description": plan.get("name") or "",
            "quantity": int(seats), "unit": plan.get("sell_by"),
            "unit_price": resolve_price(plan, await api.get_default_price_list(token))}
    trial = (form.get("trial_end") or "").strip()
    return {"contact_id": form.get("contact_id") or None, "line_items": [line],
            **({"trial_end": trial, "anchor_date": trial} if trial else {})}


def _plans_table(plans: list[dict], counts: dict[str, int], price_list: str, currency: str | None) -> FT:
    from celerp.services.pricing import resolve_price

    def _row(p: dict) -> FT:
        pid = p.get("id") or p.get("entity_id", "")
        return Tr(Td(A(p.get("name") or p.get("sku") or pid, href=f"/inventory/{pid}")),
                  Td(fmt_money(resolve_price(p, price_list), currency), cls="cell--number"),
                  Td(str(counts.get(pid, 0)), cls="cell--number"), cls="data-row")

    return _data_table(Thead(Tr(Th(t("subscriptions.plan")), Th(price_list, cls="cell--number"),
                                Th(t("nav.subscribers"), cls="cell--number"))),
                       Tbody(*[_row(p) for p in plans]))


def _listed(items: list[dict], state: dict) -> list[dict]:
    """The agreements a list view shows: those of its status (the default, All Issued, leaves
    drafts out) that match its search."""
    status, ql = state["status"], state["q"].lower()
    items = [i for i in items if i.get("status") == status] if status else [i for i in items if i.get("status") != "draft"]
    return [i for i in items if not ql or any(ql in (v or "").lower() for v in (
        i.get("ref_id") or i.get("doc_number"), i.get("name"), i.get("contact_name"), i.get("contact_company_name")))]


def _list_views(all_items: list[dict], state: dict) -> tuple[FT, FT, int]:
    """The status cards and the table of one page of a list view, and how many rows it has in all."""
    listed = _listed(all_items, state)
    page, per_page = state["page"], state["per_page"]
    return (_sub_status_cards(all_items, state),
            _sub_table(listed[(page - 1) * per_page: page * per_page], state), len(listed))


async def _agreements(token: str, direction: str | None = None) -> dict[str, dict]:
    """Agreements by id, of one direction or both."""
    items = await _all_agreements(token, direction)
    return {(i.get("id") or i.get("entity_id")): i for i in items}


def _catch_up_items(form) -> list[dict]:
    """The chosen periods (the ``item`` field a _bulk_form posts), in the order the page lists
    them. Anything but a list chooses nothing."""
    chosen = _posted_list(form.get("item"))
    items = []
    for raw in chosen if isinstance(chosen, list) else []:
        eid, _, k = str(raw).rpartition("|")
        if eid and k.lstrip("-").isdigit():
            items.append({"entity_id": eid, "period_index": int(k)})
    return items


REVENUE_PAGE = "/revenue"
_MONTH_COLUMNS = (("subscriptions.th_mrr", "mrr"), ("subscriptions.th_arr", "arr"),
                  ("subscriptions.th_new", "new_mrr"), ("subscriptions.th_expansion", "expansion_mrr"),
                  ("subscriptions.th_contraction", "contraction_mrr"), ("subscriptions.th_churned", "churned_mrr"))
_DEFINITIONS = ("def_mrr", "def_arr", "def_active", "def_movement", "def_logo_churn", "def_revenue_churn", "def_currency")


def _percent(share: float | None) -> str:
    """A share as a percent with at most one decimal: 0.5 is 50%, None is the empty mark."""
    if share is None:
        return EMPTY
    return f"{share * 100:.1f}".rstrip("0").rstrip(".") + "%"


def _revenue_filter(params: QueryParams) -> FT:
    """The range and currency, sent in the URL."""
    return Form(
        Label(t("subscriptions.metrics_from"), Input(type="date", name="from", value=params.get("from", ""),
                                                      cls="date-input")),
        Label(t("subscriptions.metrics_to"), Input(type="date", name="to", value=params.get("to", ""),
                                                    cls="date-input")),
        Label(t("label.currency"), Input(type="text", name="currency", value=params.get("currency", ""),
                                         maxlength="3", placeholder=t("subscriptions.metrics_currency_all"))),
        Button(t("btn.apply"), type="submit", cls="btn btn--secondary btn--sm"),
        action=REVENUE_PAGE, method="get", cls="date-custom-form",
    )


def _months_table(data: dict) -> FT:
    """One row per month end, newest first."""
    cur = data["currency"]
    head = Tr(Th(t("subscriptions.th_month")), *[Th(t(k), cls="cell--number") for k, _ in _MONTH_COLUMNS[:2]],
              Th(t("subscriptions.th_active"), cls="cell--number"),
              *[Th(t(k), cls="cell--number") for k, _ in _MONTH_COLUMNS[2:]],
              Th(t("subscriptions.th_logo_churn"), cls="cell--number"),
              Th(t("subscriptions.th_revenue_churn"), cls="cell--number"))

    def _row(m: dict) -> FT:
        money = [Td(fmt_money(m[f], cur), cls="cell--number") for _, f in _MONTH_COLUMNS]
        return Tr(Td(m["month"]), *money[:2], Td(str(m["active"]), cls="cell--number"), *money[2:],
                  Td(_percent(m["logo_churn"]), cls="cell--number"),
                  Td(_percent(m["revenue_churn"]), cls="cell--number"), cls="data-row")

    return _data_table(Thead(head), Tbody(*[_row(m) for m in reversed(data["months"])]))


def _cohorts_table(data: dict) -> FT:
    """Each start month, newest first, with the share still active and their MRR at each month end."""
    cohorts, cur = data["cohorts"], data["currency"]
    width = max(len(c["active"]) for c in cohorts)
    head = Tr(Th(t("subscriptions.th_cohort")), Th(t("subscriptions.th_cohort_size"), cls="cell--number"),
              *[Th(t("subscriptions.th_month_n", n=i + 1), cls="cell--number") for i in range(width)])

    def _row(c: dict) -> FT:
        cells = [Td(Div(_percent(n / c["size"])), Div(fmt_money(mrr, cur), cls="text-muted"), cls="cell--number")
                 for n, mrr in zip(c["active"], c["mrr"])]
        cells += [Td("", cls="cell--number")] * (width - len(cells))
        return Tr(Td(c["month"]), Td(str(c["size"]), cls="cell--number"), *cells, cls="data-row")

    return _data_table(Thead(head), Tbody(*[_row(c) for c in reversed(cohorts)]))


def _not_converted(data: dict) -> list[FT]:
    return [P(t("subscriptions.metrics_not_converted", month=m["month"], count=len(m["not_converted"])),
              cls="flash flash--warning") for m in data["months"] if m["not_converted"]]


def _definitions() -> FT:
    return Div(H3(t("subscriptions.definitions_heading")),
               *[P(t(f"subscriptions.{k}"), cls="text-muted") for k in _DEFINITIONS])


def _revenue_body(data: dict, *, filtered: bool) -> list[FT]:
    """The tables, or the empty state when no subscription counted in any month of the range.
    Filtered to one currency, an empty range shows its zeros: other currencies may have revenue."""
    if not filtered and not any(m["active"] or m["not_converted"] for m in data["months"]):
        return [empty_state_cta(t("subscriptions.revenue_empty"), t("page.new_subscriber"),
                                "/subscribers#new-subscriber")]
    body = [*_not_converted(data), _months_table(data)]
    if data["cohorts"]:
        body += [H3(t("subscriptions.cohorts_heading")), P(t("subscriptions.cohorts_hint"), cls="text-muted"),
                 _cohorts_table(data)]
    return body


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
            return _refused(e)
        return _due_panel(preview, subs)

    @app.post("/subscriptions/generate-due")
    async def subscriptions_generate_all_due(request: Request):
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        items = _catch_up_items(form)
        if not items:
            return _action_error(t("label.select_rows_first"))
        try:
            key = required_operation_key(form, "generate-due")
            for item in items:
                item["request_key"] = f"{key}:{item['period_index']}"
            result = await api.generate_all_due(token, items)
        except APIError as e:
            return _refused(e)

        async def draw():
            answer = _due_results(result, await _agreements(token), str(form[FIELD]))
            # Run from the list: its cards and rows show what the run changed.
            page = urlsplit(request.headers.get("HX-Current-URL", ""))
            if page.path not in _LIST_PATHS.values():
                return answer
            state = _list_state(QueryParams(page.query), page.path)
            listed = await _all_agreements(token, state["direction"], review=state["review"], kind=state["kind"])
            cards, table, _ = _list_views(listed, state)
            return answer, cards(hx_swap_oob="true"), table(hx_swap_oob="true")
        return await _redrawn(draw, lambda: _due_results(result, {}, str(form[FIELD])))

    @app.get("/subscriptions/{entity_id}/doc-note/{doc_id}")
    async def subscription_doc_note(request: Request, entity_id: str, doc_id: str):
        """What a document's page says of the period it was made for, read from the agreement as
        stored: billed again on a newer document, reopened, or void while it still holds the
        period. Nothing while it bills the period, or when the agreement cannot be read."""
        token = _token(request)
        if not token:
            return ""
        try:
            sub = await api.get_subscription(token, entity_id)
            doc = await api.get_doc(token, doc_id)
            k = (sub.get("released_bills") or {}).get(doc_id)
            entry = ((sub.get("periods") or {}).get(str(k)) or {}) if k is not None else {}
            newer_id = entry.get("doc_id") if entry.get("state") == "billed" else None
            newer = await api.get_doc(token, newer_id) if newer_id and newer_id != doc_id else None
        except APIError as e:
            api.raise_if_signed_out(e)
            return ""
        if k is not None:
            text = (sentence("subscriptions.doc_period_rebilled", newer=newer.get("ref_id") or newer_id)
                    if bills(newer) else t("subscriptions.doc_period_reopened"))
        elif doc.get("status") == "void":
            text = t("subscriptions.doc_void_period")
        else:
            return ""
        return P(text, cls="text-muted")

    @app.get("/subscriptions/catch-up")
    async def subscriptions_catch_up_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "view_subscriptions", page_view=True)):
            return r
        items = (await api.subscriptions_catch_up(token)).get("items") or []
        subs = await _agreements(token)
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
        # Each batch or period answers for itself: what earlier ones did stays reported beside a
        # later failure. A run whose every batch was refused is a refusal.
        results, refusals = [], []
        if action == "run":
            try:
                key = required_operation_key(form, "catch-up")
            except APIError as e:
                return _refused(e)
            for item in chosen:
                item["request_key"] = f"{key}:{item['period_index']}"
            for offset in range(0, len(chosen), 50):
                batch = chosen[offset:offset + 50]
                try:
                    results.extend((await api.run_subscriptions_catch_up(token, batch)).get("results") or [])
                except APIError as e:
                    api.raise_if_signed_out(e)
                    refusals.append(e)
                    results.extend({**item, "ok": False, "error": error_message(e)} for item in batch)
        else:
            reason = str(form.get("reason", "")).strip()
            for item in chosen:
                try:
                    done = await api.subscription_period_action(token, item["entity_id"], item["period_index"],
                                                                "skip", {"reason": reason})
                    results.append({**item, "ok": True, "skipped": True, **done["period"]})
                except APIError as e:
                    api.raise_if_signed_out(e)
                    results.append({**item, "ok": False, "error": error_message(e)})
        if refusals and len(refusals) == len(range(0, len(chosen), 50)):
            return _refused(refusals[0])

        async def draw():
            items = (await api.subscriptions_catch_up(token)).get("items") or []
            return _catch_up_card(items, await _agreements(token), results)
        return await _redrawn(draw, lambda: Div(_outcome_table(results, {}), id="catchup-card"))

    @app.get("/subscriptions/summary")
    async def subscriptions_summary_page(request: Request, all: bool = False):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "view_subscriptions", page_view=True)):
            return r
        summary = await api.subscriptions_summary(token, everything=all)
        subs = await _agreements(token)
        rows = summary.get("rows") or []
        counts = summary.get("counts") or {}
        title = t("subscriptions.summary_title")
        toggle = (A(t("subscriptions.summary_unread"), href="/subscriptions/summary", cls="btn btn--secondary") if all
                  else A(t("subscriptions.summary_all"), href="/subscriptions/summary?all=1", cls="btn btn--secondary"))
        content = Div(
            page_header(title,
                        Button(t("subscriptions.summary_ack"), hx_post="/subscriptions/summary/ack", hx_swap="none",
                               hx_vals={"keys": json.dumps([r["key"] for r in rows if r.get("key")])},
                               cls="btn btn--primary") if rows and not all else "",
                        toggle),
            P(t("subscriptions.summary_counts", generated=counts.get("generated", 0), sent=counts.get("sent", 0),
                failed=_to_check(counts)), " ", t("subscriptions.summary_window", days=SUMMARY_WINDOW.days),
              cls="text-muted"),
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
            await api.acknowledge_subscriptions_summary(token, _posted_list((await request.form()).get("keys")))
        except APIError as e:
            return _refused(e)
        return _done("/subscriptions/summary", t("subscriptions.done_summary_read"))

    # --- List page ---

    async def list_page(request: Request, path: str):
        """An agreement list page: every agreement on one side (/subscriptions), or those of one kind
        (/retainers), with its own title, New button and empty state."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "view_subscriptions", page_view=True)):
            return r
        state = _list_state(request.query_params, path)
        direction, status, review, kind = state["direction"], state["status"], state["review"], state["kind"]
        q, page, per_page = state["q"], state["page"], state["per_page"]
        all_items = await _all_agreements(token, direction, review=review, kind=kind)
        cards, table, total = _list_views(all_items, state)
        side = "sales" if direction == "sales" else "purchasing"
        nav_key = f"subscriptions_{side}" if not kind else path.lstrip("/")
        text = _LIST_TEXT[kind]
        title = t(text.get("title") or f"nav.subscriptions_{side}")
        extra = urlencode({k: v for k, v in {"direction": direction, "status": status, "q": q,
                                             "review": "1" if review else ""}.items() if v})
        can_import = await _check_permission(request, "import_export_data") is None
        catch_up, summary, banner_errors = await _held_and_unread(token)
        search_url = "/subscriptions/search?" + urlencode({k: v for k, v in {
            "direction": direction, "status": status, "review": "1" if review else "", "kind": kind}.items() if v})

        content = Div(
            page_header(
                title,
                search_bar(placeholder=t("subscriptions.search_placeholder"), target="#sub-table",
                           url=search_url, label=t(f"subscriptions.search_{side}")),
                _new_button(state, "btn btn--primary"),
                Button(t("btn.generate_all_due"), hx_get=f"/subscriptions/due?direction={direction}",
                       hx_target="#sub-due-panel", hx_swap="innerHTML", cls="btn btn--secondary"),
                A(import_icon(), t("btn.import"), href="/subscriptions/import", cls="btn btn--secondary",
                  data_import_hint=True) if can_import else "",
            ),
            *_list_banners(catch_up, summary),
            *[Div(error, cls="flash flash--warning") for error in banner_errors],
            _review_banner(all_items, state),
            Div(id="sub-due-panel", data_esc_clear="1"),
            _PANEL_ESC_JS,
            _subscriber_form(await _plans(token), state) if text.get("form") else "",
            cards,
            table,
            pagination(page, total, per_page, path, extra),
            cls="page-content",
        )
        return await base_shell(content, title=title, request=request, nav_active=nav_key)

    @app.get("/subscriptions")
    async def subscriptions_list(request: Request):
        return await list_page(request, "/subscriptions")

    @app.get("/retainers")
    async def retainers_list(request: Request):
        return await list_page(request, "/retainers")

    @app.get("/subscribers")
    async def subscribers_list(request: Request):
        return await list_page(request, "/subscribers")

    @app.get("/plans")
    async def plans_page(request: Request):
        """SaaS plans: each plan item with its price on the default price list and how many
        active or paused agreements bill it."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "view_subscriptions", page_view=True)):
            return r
        title = t("nav.plans")
        try:
            plans = await _plans(token)
            price_list = await api.get_default_price_list(token)
            agreements = await _all_agreements(token, "sales", kind="subscription")
        except APIError as exc:
            api.raise_if_signed_out(exc)
            body = Div(error_message(exc), cls="flash flash--warning")
        else:
            counts: dict[str, int] = {}
            for a in agreements:
                if a.get("status") in ("active", "paused"):
                    for pid in {li.get("item_id") for li in a.get("line_items") or []}:
                        counts[pid] = counts.get(pid, 0) + 1
            currency = (await _get_company(token)).get("currency")
            body = (_plans_table(plans, counts, price_list, currency) if plans else
                    empty_state_cta(t("subscriptions.plans_empty"), t("page.new_plan"),
                                    "/inventory/create-blank?type=plan", hx_post=True))
        new_plan = Button(t("page.new_plan"), hx_post="/inventory/create-blank?type=plan", hx_swap="none",
                          cls="btn btn--primary")
        content = Div(page_header(title, new_plan), P(t("subscriptions.plans_hint"), cls="text-muted"), body,
                      cls="page-content")
        return await base_shell(content, title=title, request=request, nav_active="plans")

    @app.get(REVENUE_PAGE)
    async def revenue_page(request: Request):
        """MRR, ARR, churn and cohorts of the sales subscriptions by month, with the definitions."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "view_financial_reports", page_view=True)):
            return r
        title = t("nav.revenue")
        params = {k: v for k in ("from", "to", "currency") if (v := request.query_params.get(k))}
        try:
            body = _revenue_body(await api.subscriptions_metrics(token, params), filtered="currency" in params)
        except APIError as exc:
            api.raise_if_signed_out(exc)
            body = [Div(error_message(exc), cls="flash flash--warning")]
        content = Div(page_header(title), P(t("subscriptions.revenue_hint"), cls="text-muted"),
                      _revenue_filter(request.query_params), *body, _definitions(), cls="page-content")
        return await base_shell(content, title=title, request=request, nav_active="revenue")

    @app.get("/members")
    async def members_list(request: Request):
        """Members and their standing, read from each membership's latest bill."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "view_subscriptions", page_view=True)):
            return r
        rows = (await api.subscriptions_members(token)).get("items") or []
        title = t("nav.members")
        content = Div(
            page_header(title, Button(t("page.new_membership"),
                                      hx_post="/subscriptions/new?direction=sales&kind=membership",
                                      hx_swap="none", cls="btn btn--primary")),
            P(t("subscriptions.members_intro"), cls="text-muted"),
            _members_table(rows),
            cls="page-content",
        )
        return await base_shell(content, title=title, request=request, nav_active="members")

    # --- Search (HTMX) ---

    @app.get("/subscriptions/search")
    async def subscriptions_search(request: Request, kind: str = ""):
        token = _token(request)
        if not token:
            return Div(t("error.session_expired"), id="sub-table")
        state = _list_state(request.query_params, _LIST_PATHS.get(kind, "/subscriptions"))
        try:
            items = await _all_agreements(token, state["direction"], review=state["review"], kind=state["kind"])
        except APIError as exc:
            api.raise_if_signed_out(exc)
            return Div(error_message(exc), id="sub-table", cls="flash flash--warning")
        return _sub_table(_listed(items, state), state)

    # --- Create blank draft → redirect to detail ---

    @app.post("/subscriptions/new")
    async def create_subscription(request: Request, direction: str = "sales", kind: str = ""):
        """A blank draft agreement; the Retainers and Members pages name its kind (the API refuses
        one it does not know). Subscribers' New subscriber form names a subscription and sends
        its plan, seats and trial (_subscriber_draft)."""
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        doc_type = _DIRECTION_DOC_TYPE.get(direction, "subscription_invoice")
        draft: dict | str = {}
        try:
            if kind == "subscription":
                draft = await _subscriber_draft(token, await request.form())
                if isinstance(draft, str):
                    return _action_error(draft)
            result = await api.create_subscription(token, {"doc_type": doc_type, "frequency": "monthly",
                                                           **({"kind": kind} if kind else {}), **draft})
        except APIError as e:
            return _refused(e)
        doc_id = result.get("entity_id") or result.get("id") or ""
        return _done(f"/subscriptions/{doc_id}", t("subscriptions.done_created"))

    # --- Detail page ---

    @app.get("/subscriptions/{entity_id}")
    async def subscription_detail(request: Request, entity_id: str, period_before: int | None = None):
        from ui.routes.documents import _doc_detail, _merge_company_letterhead, doc_detail_connectors
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        try:
            doc = await api.get_subscription(token, entity_id)
        except APIError as exc:
            if exc.status == 404:
                return RedirectResponse("/subscriptions", status_code=302)
            raise

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

        schedule = await api.get_subscription_periods(token, entity_id, before=period_before)

        status = doc.get("status", "draft")
        ref = doc.get("ref_id") or doc.get("doc_number") or entity_id.split(":")[-1]
        type_label = t("subscriptions.type_sales") if direction == "sales" else t("subscriptions.type_purchasing")
        status_label = display_enum(status, "subscription_status")
        list_url = f"/subscriptions?direction={direction}"

        doc = _with_schedule(await _with_collection(token, entity_id, await _with_member(token, doc)), schedule)
        actions_left, actions_right = _action_groups(entity_id, doc, schedule)

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
                extra_left_actions=[actions_left],
                extra_right_actions=[actions_right],
                suppress_pdf=True,
            ),
            _schedule_section(entity_id, doc) if status not in ENDED else "",
            _quantities_section(entity_id, doc, business_today(tz)) if status in ("active", "paused") else "",
            _billing_section(entity_id, doc, _defers(co_settings, doc)),
            _history_review(entity_id, schedule.get("review") or [], schedule.get("set_aside") or []),
            _periods_table(entity_id, schedule, historical_page=period_before is not None),
            title=f"{type_label} {ref} - Celerp",
            nav_active="subscriptions_sales" if direction == "sales" else "subscriptions_purchasing",
            request=request,
        )

    # --- Lifecycle actions ---

    @app.post("/subscriptions/{entity_id}/amend")
    async def subscription_amend_ui(request: Request, entity_id: str):
        """The Quantities form: each line's new quantity from the effective date (billing.amend)."""
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        try:
            posted = json.loads(str(form.get("quantities", "")))
            quantities = {str(line): float(str(qty).strip()) for line, qty in posted.items()}
        except (ValueError, AttributeError):
            quantities = {"": math.nan}
        if not all(math.isfinite(q) for q in quantities.values()):
            return _action_error(t("subscriptions.err_amend_quantity"))
        body = {"effective": str(form.get("effective", "")).strip(), "quantities": quantities,
                "request_key": str(form.get(FIELD, "")).strip()}
        try:
            done = await api.amend_subscription(token, entity_id, body)
        except APIError as e:
            return _refused(e)
        said = (sentence("subscriptions.done_amended_settled", day=done["effective"],
                         credit_note=done["credit_note_ref"], document=done["invoice_ref"])
                if done.get("credit_note_id") else sentence("subscriptions.done_amended", day=done["effective"]))
        return _done(f"/subscriptions/{entity_id}", said)

    @app.post("/subscriptions/{entity_id}/collection/{action}")
    async def subscription_collection_ui(request: Request, entity_id: str, action: str):
        """The card on file section's actions (_collection_card): Email or Copy setup link and
        Email receipt again (setup-link), Remove card (revoke). A link not emailed is shown to copy."""
        if action not in _COLLECTION_ACTIONS:
            return Response(status_code=404)
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        back = f"/subscriptions/{entity_id}"
        if action == "revoke":
            try:
                await api.revoke_subscription_card(token, entity_id)
            except APIError as e:
                return _refused(e)
            return _done(back, t("subscriptions.done_card_removed"))
        form = await request.form()
        body = {"delivery": str(form.get("delivery", "")), "resend_receipt": form.get("resend_receipt") == "true"}
        try:
            done = await api.subscription_setup_link(token, entity_id, body)
        except APIError as e:
            return _refused(e)
        if body["resend_receipt"]:
            if done.get("receipt_sent"):
                return _done(back, t("subscriptions.done_receipt_sent"))
            return _done(back, t("subscriptions.receipt_not_resent"), "warning")
        if body["delivery"] == "email" and done.get("emailed"):
            return _done(back, t("subscriptions.done_setup_link_emailed"))
        link = done["link"]
        said = t("subscriptions.setup_link_not_emailed") if body["delivery"] == "email" else ""

        async def draw():
            card = _collection_card(entity_id, await api.get_subscription_collection(token, entity_id), link)
            return html_response(card, headers=toast_header(said, "warning") if said else None)
        return await _redrawn(draw, lambda: Div(H4(t("subscriptions.collection_heading")), _setup_link_text(link),
                                                id="sub-collection", cls="mt-md"), said=said)

    @app.post("/subscriptions/{entity_id}/{action}")
    async def subscription_action_ui(request: Request, entity_id: str, action: str):
        if action not in api.SUBSCRIPTION_ACTIONS:
            return Response(status_code=404)
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        key = submitted_operation_key(form)
        body = {"request_key": key[FIELD]} if action in _BILLING_ACTIONS and key else None
        if body is not None and "period_index" in form:
            try:
                body["period_index"] = int(str(form["period_index"]))
            except ValueError:
                return _action_error(t("subscriptions.err_early_not_next"))
        try:
            done = await api.subscription_action(token, entity_id, action, body)
        except APIError as e:
            return _refused(e)
        if action in _BILLING_ACTIONS:
            return _done(f"/subscriptions/{entity_id}", _billed_text(done))
        # Resume and Activate answer with what now runs automatically, read after the change.
        message = " ".join(m for m in (t(_ACTION_DONE[action]), done.get("message")) if m)
        return _done(f"/subscriptions/{entity_id}", message, "warning" if done.get("lapsed") else "success")

    @app.post("/subscriptions/{entity_id}/periods/{k}/{action}")
    async def subscription_period_ui(request: Request, entity_id: str, k: int, action: str):
        if action not in api.PERIOD_ACTIONS:
            return Response(status_code=404)
        token = _token(request)
        if not token:
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        reason = str(form.get("reason", "")).strip()
        body = ({"reason": reason} if action == "skip"
                else {"attempt": form.get("attempt"), **({"again": True} if form.get("again") else {})}
                if action == "send" else None)
        try:
            done = await api.subscription_period_action(token, entity_id, k, action, body)
        except APIError as e:
            return _refused(e)
        back = "/subscriptions/summary" if form.get("back") == "summary" else f"/subscriptions/{entity_id}"
        if action == "send":
            # The API reads the period's email after the attempt; the toast says what is stored.
            return _done(back, done.get("message") or "", _SEND_TOAST.get(done.get("send"), "warning"))
        period = done["period"]
        return _done(back, sentence(_PERIOD_DONE[action], start=period["service_start"], end=period["service_end"]))

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
            return _refused(e)
        return _done(f"/subscriptions/{entity_id}", t(_HISTORY_DONE[action]))

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
        except ValueError:
            return _action_error(t(_FIELD_ERRORS[field]))
        # The days box is the custom frequency: entering days chooses it (an empty box chooses nothing).
        changes = {field: value, **({"frequency": "custom"} if field == "custom_interval_days" and value is not None else {})}

        async def read() -> tuple[dict, str]:
            """The agreement as stored and every view of its schedule drawn from it."""
            sub = await _with_collection(token, entity_id, await _with_member(token, await api.get_subscription(token, entity_id)))
            settings = (await api.get_company(token)).get("settings") or {}
            return sub, _stored_schedule_views(entity_id, sub, await api.get_subscription_periods(token, entity_id),
                                               _defers(settings, sub))

        async def stored():
            return (await read())[1]
        try:
            saved = await api.update_subscription_schedule(token, entity_id, changes)
        except APIError as e:
            # The control already shows the refused value (Custom shows the days box).
            return await _refused_redrawn(e, stored)

        async def draw():
            sub, views = await read()
            # The version the save stored, for the page's next lines save, as a document's field saves hand it.
            version = {"celerpListVersion": {"version": sub.get("version")}}
            if saved.get("sending_off"):
                return html_response(views, headers=toast_header(t("subscriptions.sending_off_manual"), "success", **version))
            return html_response(views, headers={"HX-Trigger": json.dumps(version)})
        return await _redrawn(draw, said=t("subscriptions.sending_off_manual") if saved.get("sending_off") else "")
