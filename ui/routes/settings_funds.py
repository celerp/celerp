# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings > Accounting > Funds: turn funds on or off, add funds, rename them, archive them,
and move money between them (transfer, release from restriction, reallocation, opening balances).

Every action answers with the whole tab drawn again from what is stored, headed by what the
action did or the reason it was refused, so the list never shows a value the books do not hold."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request

import ui.api_client as api
from celerp.services.business_time import business_today
from celerp.services.journal_accounts import funds_enabled
from celerp.services.peer_lines import UNRESTRICTED
from ui.api_client import APIError, error_message
from ui.components.encode import call, EVENT, THIS
from ui.components.funds import fund_options
from ui.components.operation_key import operation_key_input, required_operation_key
from ui.components.table import EMPTY, fmt_money, searchable_select
from ui.i18n import date_text, t
from ui.routes.settings import _token

FUNDS_URL = "/settings/accounting/funds"
_TAB_ID = "funds-content"
_OPENING_LINES_ID = "fund-opening-lines"
# The accounts a movement moves: balance-sheet accounts only (income and expense are refused).
_MOVED_TYPES = ("asset", "liability")
# Each movement: its API path segment, title key and the help under its title.
_MOVEMENTS = {"transfer": ("funds.transfer_title", "funds.transfer_help"),
              "release": ("funds.release_title", "funds.release_help"),
              "reallocate": ("funds.reallocate_title", "funds.reallocate_help"),
              "opening": ("funds.opening_title", "funds.opening_help")}


def movement_title(kind: str) -> str:
    """A movement's name: transfer, release, reallocate or opening."""
    return t(_MOVEMENTS[kind][0])


def _fund_url(fund_id: str, action: str = "") -> str:
    return f"{FUNDS_URL}/{quote(fund_id, safe='')}{action}"


def _swap(url: str) -> dict:
    """Post to ``url`` and draw the whole tab with the answer."""
    return {"hx_post": url, "hx_target": f"#{_TAB_ID}", "hx_swap": "outerHTML"}


def _row_get(fund_id: str, url: str) -> dict:
    """Get ``url`` and draw the fund's row with the answer."""
    return {"hx_get": url, "hx_target": f"#{_row_id(fund_id)}", "hx_swap": "outerHTML"}


def _row_id(fund_id: str) -> str:
    return "fund-" + "".join(c if c.isalnum() else "-" for c in fund_id)


def _setting_form(enabled: bool) -> FT:
    return Form(
        P(t("funds.on_help" if enabled else "funds.off_help"), cls="text-muted mb-md"),
        Input(type="hidden", name="enabled", value="0" if enabled else "1"),
        Button(t("funds.turn_off" if enabled else "funds.turn_on"), type="submit",
               cls="btn btn--outline" if enabled else "btn btn--primary"),
        **_swap(f"{FUNDS_URL}/setting"),
    )


def fund_row(fund: dict) -> FT:
    """One fund: its name, purpose and opening date click to edit, whether it is restricted,
    and its archive or unarchive action."""
    archived = fund.get("archived")
    opened = fund.get("opening_date")
    return Tr(
        Td(Span(fund["name"], cls="editable-cell", title=t("documents.click_to_edit"),
                **_row_get(fund["id"], _fund_url(fund["id"], "/edit")))),
        Td(t("funds.restricted") if fund.get("restricted") else t("label.unrestricted")),
        Td(Span(fund.get("purpose") or EMPTY, cls="editable-cell", title=t("documents.click_to_edit"),
                **_row_get(fund["id"], _fund_url(fund["id"], "/edit")))),
        Td(Span(date_text(date.fromisoformat(opened)) if opened else EMPTY, cls="editable-cell",
                title=t("documents.click_to_edit"), **_row_get(fund["id"], _fund_url(fund["id"], "/edit")))),
        Td(t("chip.archived") if archived else t("th.active")),
        Td(Button(t("funds.unarchive" if archived else "funds.archive"), type="button", cls="btn btn--sm btn--outline",
                  **_swap(_fund_url(fund["id"], "/unarchive" if archived else "/archive")))),
        id=_row_id(fund["id"]),
    )


def fund_row_editor(fund: dict, error: str = "") -> FT:
    """A fund's row as an inline edit: name, purpose and opening date fields, Save, and Cancel
    (or Esc) back to the row as stored."""
    cancel = _row_get(fund["id"], _fund_url(fund["id"], "/row"))
    return Tr(
        Td(Input(type="text", name="name", value=fund["name"], cls="form-input", autofocus=True),
           *([P(error, cls="error-banner mt-sm")] if error else [])),
        Td(t("funds.restricted") if fund.get("restricted") else t("label.unrestricted")),
        Td(Input(type="text", name="purpose", value=fund.get("purpose") or "", cls="form-input")),
        Td(Input(type="date", name="opening_date", value=fund.get("opening_date") or "", cls="form-input")),
        Td(t("chip.archived") if fund.get("archived") else t("th.active")),
        Td(Button(t("btn.save"), type="button", cls="btn btn--sm btn--primary",
                  hx_include="closest tr", hx_patch=_fund_url(fund["id"]), hx_target=f"#{_row_id(fund['id'])}",
                  hx_swap="outerHTML"),
           Button(t("btn.cancel"), type="button", cls="btn btn--sm btn--secondary ml-sm", **cancel)),
        id=_row_id(fund["id"]),
        onkeydown=call("celerpCellKey", THIS, EVENT, {"url": cancel["hx_get"], "target": cancel["hx_target"]}),
    )


def _add_form(values: dict) -> FT:
    return Form(
        operation_key_input(),
        Div(Label(t("label.name")), Input(type="text", name="name", value=values.get("name", ""), cls="form-input"),
            cls="form-field"),
        Div(Label(Input(type="checkbox", name="restricted", value="1", checked=bool(values.get("restricted"))),
                  " ", t("funds.restricted_check")), cls="form-field"),
        Div(Label(t("funds.purpose")), Input(type="text", name="purpose", value=values.get("purpose", ""),
                                             placeholder=t("funds.purpose_hint"), cls="form-input"),
            cls="form-field"),
        Div(Label(t("funds.opening_date")), Input(type="date", name="opening_date",
                                                  value=values.get("opening_date", ""), cls="form-input"),
            cls="form-field"),
        Div(Button(t("funds.add"), type="submit", cls="btn btn--primary"), cls="form-actions mt-md"),
        **_swap(FUNDS_URL),
    )


@dataclass(frozen=True)
class FundsBooks:
    """What the Funds tab is drawn from: the setting, the funds, and for the movement forms
    the accounts a movement may move, the purpose spending a release may name, the
    company currency and today in the company's timezone (the forms' date)."""
    enabled: bool
    funds: list[dict]
    accounts: list[dict] = field(default_factory=list)
    spending: list[dict] = field(default_factory=list)
    currency: str = ""
    today: str = ""


def _field(label: str, control) -> FT:
    return Div(Label(label), control, cls="form-field")


def _text(name: str, values: dict, **attrs) -> FT:
    return Input(type=attrs.pop("type", "text"), name=name, value=values.get(name, ""), cls="form-input", **attrs)


def _fund_select(name: str, books: FundsBooks, values: dict) -> FT:
    return searchable_select(name, fund_options(books.funds), value=values.get(name, ""), aria_label=t("th.fund"))


def _account_options(books: FundsBooks, blank: str) -> list[tuple[str, str]]:
    return [("", blank), *((a["code"], f"{a['code']} {a.get('name', '')}".strip()) for a in books.accounts)]


def _account_select(books: FundsBooks, values: dict) -> FT:
    return searchable_select("account", _account_options(books, t("funds.default_account")),
                             value=values.get("account", ""), aria_label=t("label.account"))


def opening_line(books: FundsBooks, line: dict | None = None) -> FT:
    """One opening assignment: an account, the fund it gives part of its balance to, the amount."""
    line = line or {}
    return Tr(
        Td(searchable_select("account", _account_options(books, EMPTY), value=line.get("account", ""),
                             aria_label=t("label.account"))),
        Td(searchable_select("fund", fund_options(books.funds), value=line.get("fund", ""), aria_label=t("th.fund"))),
        Td(_text("amount", line, type="number", step="0.01", min="0", aria_label=t("label.amount")),
           cls="cell--number"),
    )


def _spending_table(books: FundsBooks, values: dict) -> FT:
    """The purpose spending a release may name, newest first: tick a line and say how much of it."""
    if not books.spending:
        return P(t("funds.release_spending_empty"), cls="empty-state")
    ticked = set(values.get("backing") or ())
    rows = []
    for item in books.spending:
        ref = f"{item['je_id']}|{item['line_index']}"
        rows.append(Tr(
            Td(Input(type="checkbox", name="backing", value=ref, checked=ref in ticked,
                     aria_label=t("funds.release_this"))),
            Td(item.get("date") or EMPTY),
            Td(item.get("memo") or item.get("description") or EMPTY),
            Td(item.get("account") or EMPTY),
            Td(fmt_money(item["left"], books.currency), cls="cell--number"),
            Td(Input(type="number", name=f"release:{ref}", step="0.01", min="0", cls="form-input",
                     value=values.get(f"release:{ref}", item["left"]), aria_label=t("funds.release_amount")),
               cls="cell--number"),
        ))
    return Table(
        Thead(Tr(Th(""), Th(t("th.date")), Th(t("th.memo")), Th(t("th.account")),
                 Th(t("funds.left_to_release"), cls="cell--number"), Th(t("funds.release_amount"), cls="cell--number"))),
        Tbody(*rows), cls="data-table")


def _movement_fields(kind: str, books: FundsBooks, values: dict) -> list:
    if kind == "opening":
        lines = values.get("lines") or [{}, {}, {}]
        return [Table(Thead(Tr(Th(t("label.account")), Th(t("th.fund")), Th(t("label.amount"), cls="cell--number"))),
                      Tbody(*[opening_line(books, line) for line in lines], id=_OPENING_LINES_ID), cls="data-table"),
                Button(t("btn.add_line"), type="button", cls="btn btn--sm btn--outline mt-sm",
                       hx_get=f"{FUNDS_URL}/opening-line", hx_target=f"#{_OPENING_LINES_ID}", hx_swap="beforeend")]
    giver = "fund" if kind == "release" else "from_fund"
    fields = [_field(t("funds.from_fund"), _fund_select(giver, books, values)),
              _field(t("funds.to_fund"), _fund_select("to_fund", books, values)),
              _field(t("label.account"), _account_select(books, values))]
    if kind == "release":
        return [*fields, P(t("funds.release_spending_help"), cls="text-muted"), _spending_table(books, values)]
    fields.append(_field(t("label.amount"), _text("amount", values, type="number", step="0.01", min="0")))
    if kind == "reallocate":
        fields.append(_field(t("funds.donor_authorization"), _text("donor_authorization", values)))
    return fields


def _movement_form(kind: str, books: FundsBooks, values: dict | None) -> FT:
    """One movement as a section that opens to its form; a refused one opens with what was typed."""
    title, help_key = _MOVEMENTS[kind]
    values = values or {}
    return Details(
        Summary(t(title)),
        Form(
            P(t(help_key), cls="text-muted mb-md"),
            operation_key_input(),
            _field(t("th.date"), _text("date", {"date": books.today, **values}, type="date")),
            *_movement_fields(kind, books, values),
            _field(t("label.reason"), _text("reason", values)),
            Div(Button(t("btn.post"), type="submit", cls="btn btn--primary"), cls="form-actions mt-md"),
            **_swap(f"{FUNDS_URL}/{kind}"),
        ),
        open=bool(values), cls="settings-section mt-md",
    )


def funds_card(books: FundsBooks, banner: FT | str = "", kept: tuple[str, dict] | None = None) -> FT:
    """The Funds tab: the setting, then (funds on) the list or its empty state, Add fund and,
    once there is a fund, the movements between funds. ``kept`` is the form a refusal
    keeps open with what was typed."""
    form, values = kept or ("", {})
    body: list = []
    if books.enabled:
        body.append(Table(
            Thead(Tr(Th(t("th.name")), Th(t("funds.restriction")), Th(t("funds.purpose")), Th(t("funds.opening_date")),
                     Th(t("th.status")), Th(""))),
            Tbody(*[fund_row(f) for f in books.funds]), cls="data-table mt-md") if books.funds
            else P(t("funds.empty"), cls="empty-state mt-md"))
        body += [H4(t("funds.add"), cls="section-title mt-lg"), _add_form(values if form == "add" else {})]
        if books.funds:
            body += [H4(t("funds.move_title"), cls="section-title mt-lg"), P(t("funds.move_help"), cls="text-muted"),
                     *[_movement_form(kind, books, values if form == kind else None) for kind in _MOVEMENTS]]
    return Div(
        banner,
        H3(t("funds.title"), cls="section-title"),
        P(t("funds.help"), cls="text-muted mb-md"),
        _setting_form(books.enabled),
        *body,
        id=_TAB_ID,
        cls="settings-card",
    )


async def funds_books(token: str) -> FundsBooks:
    """The Funds tab's books read now."""
    company = await api.get_company(token)
    if not funds_enabled(company.get("settings") or {}):
        return FundsBooks(False, [])
    funds = (await api.list_funds(token)).get("items") or []
    if not funds:
        return FundsBooks(True, [])
    accounts = [a for a in (await api.get_chart(token)).get("items") or ()
                if a.get("account_type") in _MOVED_TYPES and a.get("is_active", True)]
    spending = (await api.list_release_spending(token)).get("items") or []
    return FundsBooks(True, funds, accounts, spending, company.get("currency") or "",
                      business_today(company.get("timezone")).isoformat())


async def funds_tab(token: str, banner: FT | str = "", kept: tuple[str, dict] | None = None) -> FT:
    """The Funds tab read from the books now."""
    return funds_card(await funds_books(token), banner, kept)


async def _answer(token: str, said: str = "", refused: str = "",
                  kept: tuple[str, dict] | None = None) -> FT:
    """The tab after an action: what it did, or why it was refused (the refusal's sentence,
    once the caller has let an ended session through to sign-in), above the tab as stored.
    When the tab cannot be read, the action's own answer alone."""
    banner = (P(refused, cls="error-banner mb-md") if refused
              else P(said, cls="success-banner mb-md") if said else "")
    try:
        return await funds_tab(token, banner, kept)
    except APIError as e:
        api.raise_if_signed_out(e)
        return Div(banner, P(error_message(e), cls="error-banner"), id=_TAB_ID, cls="settings-card")


def _fund_value(raw) -> str | None:
    """A fund chosen on a form: None for Unrestricted (or none chosen)."""
    value = str(raw or "").strip()
    return None if value in ("", UNRESTRICTED) else value


def _movement_body(kind: str, form) -> tuple[dict, dict]:
    """What a movement form sends the API, and what it typed (kept on a refusal)."""
    typed = {k: str(form.get(k) or "").strip() for k in
             ("date", "reason", "from_fund", "to_fund", "fund", "account", "amount", "donor_authorization")}
    body = {"date": typed["date"], "reason": typed["reason"]}
    if kind == "opening":
        lines = [{"account": str(a).strip(), "fund": str(f).strip(), "amount": str(m).strip()}
                 for a, f, m in zip(form.getlist("account"), form.getlist("fund"), form.getlist("amount"))]
        lines = [line for line in lines if any(line.values())]
        body["assignments"] = [{**line, "fund": _fund_value(line["fund"])} for line in lines]
        return body, {"date": typed["date"], "reason": typed["reason"], "lines": lines or None}
    body.update({"to_fund": _fund_value(typed["to_fund"]), "account": typed["account"] or None})
    if kind == "release":
        ticked = form.getlist("backing")
        body["fund"] = _fund_value(typed["fund"])
        body["backing"] = [{"je_id": ref.rpartition("|")[0], "line_index": int(ref.rpartition("|")[2]),
                            "amount": str(form.get(f"release:{ref}") or "").strip()}
                           for ref in ticked if ref.rpartition("|")[2].isdigit()]
        typed.update({"backing": ticked, **{f"release:{ref}": str(form.get(f"release:{ref}") or "")
                                             for ref in ticked}})
    else:
        body.update({"from_fund": _fund_value(typed["from_fund"]), "amount": typed["amount"]})
    if kind == "reallocate":
        body["donor_authorization"] = typed["donor_authorization"]
    return body, typed


async def _fund(token: str, fund_id: str) -> dict | None:
    return next((f for f in (await api.list_funds(token)).get("items") or () if f["id"] == fund_id), None)


def setup_routes(app):

    @app.post(f"{FUNDS_URL}/setting")
    async def funds_setting(request: Request):
        token = _token(request)
        enabled = (await request.form()).get("enabled") == "1"
        try:
            await api.set_funds_setting(token, enabled)
        except APIError as e:
            api.raise_if_signed_out(e)
            return await _answer(token, refused=error_message(e))
        return await _answer(token, t("funds.turned_on" if enabled else "funds.turned_off"))

    @app.post(FUNDS_URL)
    async def add_fund(request: Request):
        token = _token(request)
        form = await request.form()
        values = {"name": str(form.get("name") or "").strip(), "restricted": form.get("restricted") == "1",
                  "purpose": str(form.get("purpose") or "").strip(),
                  "opening_date": str(form.get("opening_date") or "").strip()}
        try:
            fund = await api.create_fund(token, {**values, "purpose": values["purpose"] or None,
                                                 "opening_date": values["opening_date"] or None,
                                                 "request_key": required_operation_key(form)})
        except APIError as e:
            api.raise_if_signed_out(e)
            return await _answer(token, refused=error_message(e), kept=("add", values))
        return await _answer(token, t("funds.added", name=fund["name"]))

    @app.get(f"{FUNDS_URL}/opening-line")
    async def add_opening_line(request: Request):
        try:
            return opening_line(await funds_books(_token(request)))
        except APIError as e:
            api.raise_if_signed_out(e)
            return await _answer(_token(request), refused=error_message(e))

    def _movement_route(kind: str):
        async def post_movement(request: Request):
            """Post the movement and head the tab with it, or with the reason it was refused."""
            token = _token(request)
            form = await request.form()
            body, typed = _movement_body(kind, form)
            try:
                await api.move_funds(token, kind, {"request_key": required_operation_key(form), **body})
            except APIError as e:
                api.raise_if_signed_out(e)
                return await _answer(token, refused=error_message(e), kept=(kind, typed))
            return await _answer(token, t("funds.movement_posted", movement=movement_title(kind)))
        app.post(f"{FUNDS_URL}/{kind}")(post_movement)

    for kind in _MOVEMENTS:
        _movement_route(kind)

    @app.post(FUNDS_URL + "/{fund_id}/archive")
    async def archive_fund(request: Request, fund_id: str):
        token = _token(request)
        try:
            fund = await api.archive_fund(token, fund_id)
        except APIError as e:
            api.raise_if_signed_out(e)
            return await _answer(token, refused=error_message(e))
        return await _answer(token, t("funds.archived", name=fund["name"]))

    @app.post(FUNDS_URL + "/{fund_id}/unarchive")
    async def unarchive_fund(request: Request, fund_id: str):
        token = _token(request)
        try:
            fund = await api.unarchive_fund(token, fund_id)
        except APIError as e:
            api.raise_if_signed_out(e)
            return await _answer(token, refused=error_message(e))
        return await _answer(token, t("funds.unarchived", name=fund["name"]))

    async def _row(request: Request, fund_id: str, draw):
        token = _token(request)
        try:
            fund = await _fund(token, fund_id)
        except APIError as e:
            api.raise_if_signed_out(e)
            return await _answer(token, refused=error_message(e))
        return draw(fund) if fund is not None else await _answer(token)

    @app.get(FUNDS_URL + "/{fund_id}/edit")
    async def edit_fund(request: Request, fund_id: str):
        return await _row(request, fund_id, fund_row_editor)

    @app.get(FUNDS_URL + "/{fund_id}/row")
    async def show_fund(request: Request, fund_id: str):
        return await _row(request, fund_id, fund_row)

    @app.patch(FUNDS_URL + "/{fund_id}")
    async def rename_fund(request: Request, fund_id: str):
        """Save the name, purpose and opening date and show the row as stored, or keep the edit
        with the reason."""
        token = _token(request)
        form = await request.form()
        typed = {k: str(form.get(k) or "") for k in ("name", "purpose", "opening_date")}
        try:
            return fund_row(await api.update_fund(token, fund_id, typed))
        except APIError as e:
            api.raise_if_signed_out(e)
            try:
                fund = await _fund(token, fund_id)
            except APIError:
                fund = None
            if fund is None:
                return await _answer(token, refused=error_message(e))
            return fund_row_editor({**fund, **typed}, error_message(e))
