# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Accounting > Revenue recognition: deferred revenue recognized month by month.

The page heads with the status: which ended months still have runs outstanding, whether runs
post automatically (and whether that lapsed). Below it each ended month lists its runs, newest
first, with Post while a run is outstanding and Reverse while one stands posted. A month the
books lock is refused with the lock date and offered its first open day as an explicit choice.
Last comes the deferred revenue roll-forward for the chosen period, with the schedules whose
source document was voided and their posted runs, for the accountant to reverse.

Also here: the click-to-edit Yes/No cells for what decides which lines defer, an item's
"Recognize over the service period" (item page) and a category's "Gifts" (Settings > Inventory).
"""
from __future__ import annotations

from collections import defaultdict
from urllib.parse import quote, urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from celerp.accounting_roles import RECOGNIZE_FIELD
from celerp.services.peer_lines import UNRESTRICTED
from celerp.services.permissions import role_has_permission
from celerp.services.recognition import GIFT_CATEGORIES
from ui.api_client import APIError, error_message
from ui.components.encode import safe_url
from ui.components.shell import module_active, no_access_banner, page_header, page_title
from ui.components.table import display_cell, doc_href, editable_cell, empty_state_cta, fmt_money
from ui.config import get_token as _token
from ui.routes.settings import _get_role
from ui.i18n import get_lang, t
from ui.routes.reports import _date_filter_bar, _get_company, _page_or_fragment, _parse_dates

PAGE = "/accounting/recognition"
SETTING_URL = f"{PAGE}/setting"
_BODY_ID = "recognition-body"
_SETTING_ID = "recognition-setting"
_NUM = "cell--number"
FLAG_URL = f"{PAGE}/flag"
# kind -> (its label, the permission its save needs: the item PATCH, the category gift PUT)
FLAGS = {"item": ("item.recognize_over_service_period", "edit_inventory"),
         "category": ("settings_inventory.category_gift", "manage_accounting")}


# ── automatic posting ────────────────────────────────────────────────────────────────────

def setting_card(enabled: bool, lapsed: bool, banner: FT | str = "") -> FT:
    """Whether recognition runs post automatically, and the button that turns it the other way.
    Shown on Revenue recognition and in Settings > Accounting; every answer redraws it."""
    return Div(
        banner,
        P(t("recognition.summary_lapsed"), cls="warning-banner") if enabled and lapsed else "",
        P(t("recognition.auto_on" if enabled else "recognition.auto_off")),
        P(t("recognition.auto_hint"), cls="settings-hint"),
        Form(
            Input(type="hidden", name="enabled", value="0" if enabled else "1"),
            Button(t("recognition.turn_off" if enabled else "recognition.turn_on"), type="submit",
                   cls="btn btn--secondary btn--sm"),
            hx_post=SETTING_URL, hx_target=f"#{_SETTING_ID}", hx_swap="outerHTML",
        ),
        id=_SETTING_ID, cls="settings-card",
    )


async def setting_tab(token: str) -> FT:
    """The Revenue recognition tab of Settings > Accounting."""
    setting = await api.get_recognition_setting(token)
    return Div(H3(t("page.revenue_recognition"), cls="section-title"),
               setting_card(bool(setting.get("enabled")), bool(setting.get("lapsed"))),
               A(t("recognition.open_page"), href=safe_url(PAGE), cls="drilldown-link"))


# ── which lines defer: the item flag and gift categories ─────────────────────────────────

def _flag_url(action: str, kind: str, key: str) -> str:
    return f"{FLAG_URL}{'/' + action if action else ''}?{urlencode({'kind': kind, 'key': key})}"


def flag_view(kind: str, key: str, on: bool, settings: dict, role: str) -> FT | None:
    """The Yes/No cell of an item's flag or a category's gift mark, click to edit with the
    permission its save needs. None when the company keeps no accounts (nothing defers)."""
    if not module_active(settings, "celerp-accounting"):
        return None
    return display_cell(entity_id=key, field=f"recognition-{kind}", value=str(on).lower(), cell_type="bool",
                        editable=role_has_permission(settings, role, FLAGS[kind][1]),
                        edit_url=_flag_url("edit", kind, key))


def _flag_editor(kind: str, key: str, on: bool, error: str = "") -> FT:
    cell = editable_cell(entity_id=key, field=f"recognition-{kind}", value=str(on).lower(), cell_type="bool",
                         aria_label=t(FLAGS[kind][0]), patch_url=_flag_url("", kind, key),
                         restore_url=_flag_url("display", kind, key))
    if error:
        cell.attrs["class"] = f'{cell.attrs.get("class", "")} cell--error'.strip()
        cell = cell(P(error, cls="cell-error"))
    return cell


async def _flag_state(token: str, kind: str, key: str) -> tuple[bool, dict]:
    """(whether the item's flag or the category's gift mark is on, the company settings)."""
    settings = (await api.get_company(token)).get("settings") or {}
    if kind == "category":
        return key in (settings.get(GIFT_CATEGORIES) or ()), settings
    return bool((await api.get_item(token, key)).get(RECOGNIZE_FIELD)), settings


# ── runs ─────────────────────────────────────────────────────────────────────────────────

def _month_action(month: str, action: str, label: str, period: tuple[str, str], day: str = "") -> FT:
    """Post or Reverse on every run of ``month``; ``day`` is the first open day chosen for a locked month."""
    d_from, d_to = period
    return Form(
        Input(type="hidden", name="from", value=d_from), Input(type="hidden", name="to", value=d_to),
        Input(type="hidden", name="on", value=day) if day else "",
        Button(label, type="submit", cls="btn btn--primary btn--sm" if action == "post" else "btn btn--secondary btn--sm"),
        hx_post=f"{PAGE}/{quote(month, safe='')}/{action}", hx_target=f"#{_BODY_ID}", hx_swap="outerHTML",
        cls="inline-form",
    )


def _month_actions(month: str, runs: list[dict], period: tuple[str, str], locked: dict | None) -> list:
    """Post while a run of the month is outstanding, Reverse while one stands posted, and the
    dated choice the refusal of a locked month offered."""
    actions = []
    if any(r["status"] == "outstanding" for r in runs):
        actions.append(_month_action(month, "post", t("btn.post"), period))
    if any(r["status"] == "posted" for r in runs):
        actions.append(_month_action(month, "reverse", t("recognition.reverse"), period))
    if locked and locked["month"] == month:
        label = "recognition.post_dated" if locked["action"] == "post" else "recognition.reverse_dated"
        actions.append(_month_action(month, locked["action"], t(label, day=locked["day"]), period, locked["day"]))
    return actions


def _document(documents: dict, schedule_id: str) -> FT | str:
    named = documents.get(schedule_id) or {}
    if not named.get("doc_id"):
        return schedule_id
    return A(named.get("ref") or named["doc_id"], href=safe_url(doc_href(named["doc_id"])), cls="drilldown-link")


def runs_view(data: dict, currency: str | None, period: tuple[str, str], locked: dict | None) -> FT:
    """Each ended month's runs, newest month first, the month's Post and Reverse on its heading row."""
    by_month: dict[str, list[dict]] = defaultdict(list)
    for run in data.get("runs") or ():
        by_month[run["month"]].append(run)
    documents = data.get("documents") or {}
    rows = []
    for month in sorted(by_month, reverse=True):
        runs = by_month[month]
        rows.append(Tr(Td(Strong(month), colspan="3"),
                       Td(Div(*_month_actions(month, runs, period, locked), cls="flex-row gap-sm")),
                       cls="row--group"))
        rows += [Tr(Td(_document(documents, r["schedule_id"])),
                    Td(str((documents.get(r["schedule_id"]) or {}).get("line") or "")),
                    Td(fmt_money(r["amount"], currency), cls=_NUM),
                    Td(t(f"recognition.status_{r['status']}")))
                 for r in runs]
    return Table(Thead(Tr(Th(t("th.document")), Th(t("th.line")), Th(t("label.amount"), cls=_NUM),
                          Th(t("th.status")))),
                 Tbody(*rows), cls="data-table data-table--compact")


def status_view(data: dict) -> FT:
    """The status block: the summary's lapse, the outstanding months and automatic posting."""
    summary = data.get("summary") or {}
    months = summary.get("months") or []
    return Div(
        P(summary["message"], cls="warning-banner") if summary.get("message") else "",
        P(t("recognition.outstanding", n=summary.get("outstanding") or 0, months=", ".join(months))
          if months else t("recognition.none_outstanding")),
        setting_card(bool(data.get("enabled")), False),
        cls="report-section",
    )


# ── roll-forward ─────────────────────────────────────────────────────────────────────────

_FLOWS = ("opening", "deferred", "recognized", "adjusted", "closing")


def roll_forward_view(roll: dict, currency: str | None, period: tuple[str, str], fund_names: dict[str, str],
                      documents: dict) -> FT:
    """The roll-forward rows (all funds, then each fund when the books use funds), whether the
    closing agrees with the deferred revenue accounts, and the schedules of a void source."""
    def row(label: str, values: dict) -> FT:
        return Tr(Td(label), *[Td(fmt_money(values.get(k, 0), currency), cls=_NUM) for k in _FLOWS])

    rows = [row(t("recognition.all_funds") if roll.get("by_fund") else t("page.revenue_recognition"), roll)]
    rows += [row(fund_names.get(fund, fund), values) for fund, values in (roll.get("by_fund") or {}).items()]
    difference = round(float(roll.get("liability") or 0) - float(roll.get("closing") or 0), 6)
    agrees = (P(t("recognition.agrees"), cls="settings-hint") if not difference
              else P(t("recognition.differs", amount=fmt_money(abs(difference), currency)), cls="warning-banner"))
    void = roll.get("void_source") or []
    return Div(
        H3(t("recognition.roll_forward"), cls="report-section-title"),
        Table(Thead(Tr(Th(""), *[Th(t(f"recognition.col_{k}"), cls=_NUM) for k in _FLOWS])),
              Tbody(*rows), cls="data-table data-table--compact"),
        agrees,
        P(t("recognition.outstanding_in_period", n=len(roll.get("outstanding") or ())), cls="settings-hint"),
        Div(H3(t("recognition.void_source"), cls="report-section-title"),
            P(t("recognition.void_hint"), cls="settings-hint"),
            Table(Tbody(*[Tr(Td(_document(documents, v["schedule_id"])),
                             Td(fmt_money(r["amount"], currency), cls=_NUM), Td(r["month"]),
                             Td(_month_action(r["month"], "reverse", t("recognition.reverse"), period)))
                          for v in void for r in v.get("posted") or ()]),
                  cls="data-table data-table--compact"))
        if void else "",
        cls="report-section",
    )


# ── the page ─────────────────────────────────────────────────────────────────────────────

async def _body(token: str, company: dict, period: tuple[str, str], banner: FT | str = "",
                locked: dict | None = None) -> FT:
    """Everything below the date filter, read again after every action."""
    currency = company.get("currency")
    data = await api.get_recognition(token)
    if not data.get("schedules") and not data.get("runs"):
        return Div(banner, empty_state_cta(t("recognition.empty")), id=_BODY_ID)
    d_from, d_to = period
    roll = await api.get_recognition_roll_forward(token, {"date_from": d_from, "date_to": d_to})
    names = {}
    if roll.get("by_fund"):
        names = {UNRESTRICTED: t("label.unrestricted"),
                 **{f["id"]: f["name"] for f in (await api.list_funds(token)).get("items") or []}}
    return Div(banner, status_view(data), runs_view(data, currency, period, locked),
               roll_forward_view(roll, currency, period, names, data.get("documents") or {}), id=_BODY_ID)


def _locked_choice(e: APIError, month: str, action: str) -> dict | None:
    """The first open day a locked month's refusal offers, as the choice to show beside it."""
    data = e.data if isinstance(e.data, dict) else {}
    day = (data.get("params") or {}).get("first_open")
    if data.get("message_key") == "recognition.err_month_locked" and day:
        return {"month": month, "action": action, "day": day}
    return None


def setup_routes(app):

    def _flag_unusable(token: str | None, kind: str):
        if kind not in FLAGS:
            return Response(status_code=404)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        return None

    @app.get(f"{FLAG_URL}/edit")
    async def flag_edit(request: Request, kind: str, key: str):
        token = _token(request)
        if (unusable := _flag_unusable(token, kind)) is not None:
            return unusable
        try:
            on, _ = await _flag_state(token, kind, key)
        except APIError as e:
            api.raise_if_signed_out(e)
            return P(error_message(e), cls="cell-error")
        return _flag_editor(kind, key, on)

    @app.get(f"{FLAG_URL}/display")
    async def flag_display(request: Request, kind: str, key: str):
        token = _token(request)
        if (unusable := _flag_unusable(token, kind)) is not None:
            return unusable
        try:
            on, settings = await _flag_state(token, kind, key)
        except APIError as e:
            api.raise_if_signed_out(e)
            return P(error_message(e), cls="cell-error")
        cell = flag_view(kind, key, on, settings, _get_role(request))
        return cell if cell is not None else Response(status_code=404)

    @app.patch(FLAG_URL)
    async def flag_save(request: Request, kind: str, key: str):
        """Turn the flag or gift mark on or off and show the cell, or keep the Yes/No open with
        the API's reason when it refuses."""
        token = _token(request)
        if (unusable := _flag_unusable(token, kind)) is not None:
            return unusable
        on = str((await request.form()).get("value", "")).strip().lower() == "true"
        try:
            if kind == "item":
                await api.patch_item(token, key, {RECOGNIZE_FIELD: on})
            else:
                await api.set_category_gift(token, key, on)
        except APIError as e:
            api.raise_if_signed_out(e)
            return _flag_editor(kind, key, on, error=error_message(e))
        return await flag_display(request, kind, key)

    @app.get(PAGE)
    async def recognition_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        try:
            company = await _get_company(token)
            d_from, d_to, preset = _parse_dates(request, company)
            content = Div(
                P(t("recognition.intro"), cls="settings-hint"),
                _date_filter_bar(PAGE, d_from, d_to, preset, settings_link="/settings/general?tab=company", lang=lang),
                await _body(token, company, (d_from, d_to)),
            )
        except APIError as e:
            api.raise_if_signed_out(e)
            content = no_access_banner(e) if e.status == 403 else Div(error_message(e), cls="error-banner")
        return await _page_or_fragment(request, page_header(t("page.revenue_recognition", lang)), content,
                                       title=page_title("page.revenue_recognition"), nav_active="accounting")

    async def _acted(request: Request, month: str, action: str):
        """Post or reverse the runs of ``month`` and redraw the page body headed by what it did,
        or by the refusal and, for a locked month, its first open day to choose."""
        token = _token(request)
        form = await request.form()
        period = (str(form.get("from") or ""), str(form.get("to") or ""))
        day = str(form.get("on") or "").strip() or None
        locked = None
        try:
            if action == "post":
                await api.post_recognition_month(token, month, day)
            else:
                await api.reverse_recognition_month(token, month, day)
            done = "recognition.posted_month" if action == "post" else "recognition.reversed_month"
            banner = P(t(done, month=month), cls="success-banner mb-md")
        except APIError as e:
            api.raise_if_signed_out(e)
            banner, locked = P(error_message(e), cls="error-banner mb-md"), _locked_choice(e, month, action)
        try:
            return await _body(token, await _get_company(token), period, banner, locked)
        except APIError as e:
            api.raise_if_signed_out(e)
            return Div(banner, P(error_message(e), cls="error-banner"), id=_BODY_ID)

    @app.post(f"{PAGE}/{{month}}/post")
    async def post_month(request: Request, month: str):
        return await _acted(request, month, "post")

    @app.post(f"{PAGE}/{{month}}/reverse")
    async def reverse_month(request: Request, month: str):
        return await _acted(request, month, "reverse")

    @app.post(SETTING_URL)
    async def set_setting(request: Request):
        """Turn automatic posting on or off and redraw the card from what is stored."""
        token = _token(request)
        enabled = str((await request.form()).get("enabled") or "") == "1"
        try:
            await api.set_recognition_setting(token, enabled)
            banner = P(t("recognition.turned_on" if enabled else "recognition.turned_off"), cls="success-banner")
        except APIError as e:
            api.raise_if_signed_out(e)
            banner = P(error_message(e), cls="error-banner")
        try:
            setting = await api.get_recognition_setting(token)
        except APIError as e:
            api.raise_if_signed_out(e)
            return Div(banner, P(error_message(e), cls="error-banner"), id=_SETTING_ID, cls="settings-card")
        return setting_card(bool(setting.get("enabled")), bool(setting.get("lapsed")), banner)
