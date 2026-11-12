# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings > Accounting > Funds: turn funds on or off, add funds, rename them, archive them.

Every action answers with the whole tab drawn again from what is stored, headed by what the
action did or the reason it was refused, so the list never shows a value the books do not hold."""

from __future__ import annotations

from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request

import ui.api_client as api
from celerp.services.journal_accounts import funds_enabled
from ui.api_client import APIError, error_message
from ui.components.operation_key import operation_key_input, required_operation_key
from ui.components.table import EMPTY
from ui.i18n import t
from ui.routes.settings import _token

FUNDS_URL = "/settings/accounting/funds"
_TAB_ID = "funds-content"


def _fund_url(fund_id: str, action: str = "") -> str:
    return f"{FUNDS_URL}/{quote(fund_id, safe='')}{action}"


def _swap(url: str, method: str = "post") -> dict:
    return {f"hx_{method}": url, "hx_target": f"#{_TAB_ID}", "hx_swap": "outerHTML"}


def _row_swap(fund_id: str, url: str, method: str) -> dict:
    return {f"hx_{method}": url, "hx_target": f"#{_row_id(fund_id)}", "hx_swap": "outerHTML"}


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
    """One fund: its name and purpose click to edit, whether it is restricted, and its
    archive or unarchive action."""
    archived = fund.get("archived")
    return Tr(
        Td(Span(fund["name"], cls="editable-cell", title=t("documents.click_to_edit"),
                **_row_swap(fund["id"], _fund_url(fund["id"], "/edit"), "get"))),
        Td(t("funds.restricted") if fund.get("restricted") else t("label.unrestricted")),
        Td(Span(fund.get("purpose") or EMPTY, cls="editable-cell", title=t("documents.click_to_edit"),
                **_row_swap(fund["id"], _fund_url(fund["id"], "/edit"), "get"))),
        Td(t("chip.archived") if archived else t("th.active")),
        Td(Button(t("funds.unarchive" if archived else "funds.archive"), type="button", cls="btn btn--sm btn--outline",
                  **_swap(_fund_url(fund["id"], "/unarchive" if archived else "/archive")))),
        id=_row_id(fund["id"]),
    )


def fund_row_editor(fund: dict, error: str = "") -> FT:
    """A fund's row as an inline edit: name and purpose fields, Save, and Cancel (or Esc)
    back to the row as stored."""
    cancel = _row_swap(fund["id"], _fund_url(fund["id"], "/row"), "get")
    return Tr(
        Td(Input(type="text", name="name", value=fund["name"], cls="form-input", autofocus=True),
           *([P(error, cls="error-banner mt-sm")] if error else [])),
        Td(t("funds.restricted") if fund.get("restricted") else t("label.unrestricted")),
        Td(Input(type="text", name="purpose", value=fund.get("purpose") or "", cls="form-input")),
        Td(t("chip.archived") if fund.get("archived") else t("th.active")),
        Td(Button(t("btn.save"), type="button", cls="btn btn--sm btn--primary",
                  hx_include="closest tr", **_row_swap(fund["id"], _fund_url(fund["id"]), "patch")),
           Button(t("btn.cancel"), type="button", cls="btn btn--sm btn--secondary ml-sm", **cancel)),
        id=_row_id(fund["id"]),
        hx_trigger="keyup[key=='Escape']", **cancel,
    )


def _add_form(values: dict | None = None) -> FT:
    values = values or {}
    return Form(
        operation_key_input(),
        Div(Label(t("label.name")), Input(type="text", name="name", value=values.get("name", ""), cls="form-input"),
            cls="form-field"),
        Div(Label(Input(type="checkbox", name="restricted", value="1", checked=bool(values.get("restricted"))),
                  " ", t("funds.restricted_check")), cls="form-field"),
        Div(Label(t("funds.purpose")), Input(type="text", name="purpose", value=values.get("purpose", ""),
                                             placeholder=t("funds.purpose_hint"), cls="form-input"),
            cls="form-field"),
        Div(Button(t("funds.add"), type="submit", cls="btn btn--primary"), cls="form-actions mt-md"),
        **_swap(FUNDS_URL),
    )


def funds_card(enabled: bool, funds: list[dict], banner: FT | str = "", values: dict | None = None) -> FT:
    """The Funds tab: the setting, then (funds on) the list or its empty state and Add fund."""
    body: list = []
    if enabled:
        body.append(Table(
            Thead(Tr(Th(t("th.name")), Th(t("funds.restriction")), Th(t("funds.purpose")), Th(t("th.status")), Th(""))),
            Tbody(*[fund_row(f) for f in funds]), cls="data-table mt-md") if funds
            else P(t("funds.empty"), cls="empty-state mt-md"))
        body += [H4(t("funds.add"), cls="section-title mt-lg"), _add_form(values)]
    return Div(
        banner,
        H3(t("funds.title"), cls="section-title"),
        P(t("funds.help"), cls="text-muted mb-md"),
        _setting_form(enabled),
        *body,
        id=_TAB_ID,
        cls="settings-card",
    )


async def funds_tab(token: str, banner: FT | str = "", values: dict | None = None) -> FT:
    """The Funds tab read from the books now."""
    enabled = funds_enabled((await api.get_company(token)).get("settings") or {})
    funds = (await api.list_funds(token)).get("items") or [] if enabled else []
    return funds_card(enabled, funds, banner, values)


async def _answer(token: str, said: str = "", refused: APIError | None = None, values: dict | None = None) -> FT:
    """The tab after an action: what it did, or why it was refused, above the tab as stored.
    When the tab cannot be read, the action's own answer alone."""
    if refused is not None:
        api.raise_if_signed_out(refused)
    banner = (P(error_message(refused), cls="error-banner mb-md") if refused is not None
              else P(said, cls="success-banner mb-md") if said else "")
    try:
        return await funds_tab(token, banner, values)
    except APIError as e:
        api.raise_if_signed_out(e)
        return Div(banner, P(error_message(e), cls="error-banner"), id=_TAB_ID, cls="settings-card")


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
            return await _answer(token, refused=e)
        return await _answer(token, t("funds.turned_on" if enabled else "funds.turned_off"))

    @app.post(FUNDS_URL)
    async def add_fund(request: Request):
        token = _token(request)
        form = await request.form()
        values = {"name": str(form.get("name") or "").strip(), "restricted": form.get("restricted") == "1",
                  "purpose": str(form.get("purpose") or "").strip()}
        try:
            fund = await api.create_fund(token, {**values, "purpose": values["purpose"] or None,
                                                 "request_key": required_operation_key(form)})
        except APIError as e:
            return await _answer(token, refused=e, values=values)
        return await _answer(token, t("funds.added", name=fund["name"]))

    @app.post(FUNDS_URL + "/{fund_id}/archive")
    async def archive_fund(request: Request, fund_id: str):
        token = _token(request)
        try:
            fund = await api.archive_fund(token, fund_id)
        except APIError as e:
            return await _answer(token, refused=e)
        return await _answer(token, t("funds.archived", name=fund["name"]))

    @app.post(FUNDS_URL + "/{fund_id}/unarchive")
    async def unarchive_fund(request: Request, fund_id: str):
        token = _token(request)
        try:
            fund = await api.unarchive_fund(token, fund_id)
        except APIError as e:
            return await _answer(token, refused=e)
        return await _answer(token, t("funds.unarchived", name=fund["name"]))

    async def _row(request: Request, fund_id: str, draw):
        token = _token(request)
        try:
            fund = await _fund(token, fund_id)
        except APIError as e:
            return await _answer(token, refused=e)
        return draw(fund) if fund is not None else await _answer(token)

    @app.get(FUNDS_URL + "/{fund_id}/edit")
    async def edit_fund(request: Request, fund_id: str):
        return await _row(request, fund_id, fund_row_editor)

    @app.get(FUNDS_URL + "/{fund_id}/row")
    async def show_fund(request: Request, fund_id: str):
        return await _row(request, fund_id, fund_row)

    @app.patch(FUNDS_URL + "/{fund_id}")
    async def rename_fund(request: Request, fund_id: str):
        """Save the name and purpose and show the row as stored, or keep the edit with the reason."""
        token = _token(request)
        form = await request.form()
        name, purpose = str(form.get("name") or ""), str(form.get("purpose") or "")
        try:
            return fund_row(await api.update_fund(token, fund_id, {"name": name, "purpose": purpose}))
        except APIError as e:
            api.raise_if_signed_out(e)
            try:
                fund = await _fund(token, fund_id)
            except APIError:
                fund = None
            if fund is None:
                return await _answer(token, refused=e)
            return fund_row_editor({**fund, "name": name, "purpose": purpose}, error_message(e))
