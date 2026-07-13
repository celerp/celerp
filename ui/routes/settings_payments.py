# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Web Access > Payments: connect a Stripe account so customers can pay invoices online.

Three states, three jobs:
  no relay   - upsell Web Access (payments cannot work without it)
  no Stripe  - a sales page: one benefit-led pitch, one CTA, fee in the fine print
  connected  - an operating page: status, deposit-account selector, disconnect
"""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.cloud_gate import upgrade_banner
from ui.components.shell import base_shell, page_header, flash
from ui.components.table import add_new_option, bank_account_options
from ui.i18n import t
from ui.routes.settings import _check_role, _token
from ui.routes.settings_cloud import _cloud_tabs, _has_team_features


def _pitch() -> FT:
    """Pre-connect sales page: one message, one action. The deposit selector and
    other admin controls stay out of sight until there is something to admin."""
    return Div(
        H3(t("pay.pitch_head"), style="margin-bottom:12px;"),
        Ul(
            Li(t("pay.pitch_li1")),
            Li(t("pay.pitch_li2")),
            Li(t("pay.pitch_li3")),
            style="text-align:left;display:inline-block;margin:0 auto 20px;line-height:1.9;",
        ),
        Form(Button(t("pay.connect_with_stripe"), type="submit", cls="btn btn--primary"),
             method="post", action="/settings/payments/connect"),
        P(t("pay.pitch_fineprint"), cls="form-hint", style="margin-top:14px;"),
        P(t("pay.pitch_stripe_cred"), cls="form-hint"),
        cls="settings-card",
        style="text-align:center;max-width:560px;margin:24px auto;padding:32px;",
    )


def _connected_panel(deposit_account: str, bank_accounts: list[dict], saved: bool = False) -> FT:
    """Post-connect operating page: status line, deposit selector, disconnect."""
    _new_opt, _new_js = add_new_option(t("acct.add_bank_account"), "/settings/accounting/bank-accounts/new")
    deposit_select = Select(
        # Blank = the server-side default: payments book to Cash (1110).
        Option(t("pay.deposit_cash_option"), value="", selected=not deposit_account),
        *bank_account_options(bank_accounts, default_code=deposit_account or None),
        _new_opt,
        name="stripe_deposit_account", cls="form-input", onchange=_new_js,
    )
    return Div(
        P(t("pay.settings_connected"), cls="form-hint"),
        P(t("pay.fee_note"), cls="form-hint"),
        flash(t("flash.saved")) if saved else "",
        Form(
            Div(Label(t("pay.deposit_label"), cls="form-label"),
                deposit_select,
                P(t("pay.deposit_hint"), cls="form-hint"),
                cls="form-group"),
            Button(t("btn.save"), type="submit", cls="btn btn--secondary"),
            method="post", action="/settings/payments", cls="settings-form",
        ),
        Form(Button(t("btn.disconnect"), type="submit", cls="btn btn--danger"),
             method="post", action="/settings/payments/disconnect",
             style="margin-top:24px;"),
    )


def _page(relay_ok: bool, enabled: bool, deposit_account: str,
          bank_accounts: list[dict], saved: bool = False) -> FT:
    if not relay_ok:
        body = upgrade_banner(t("nav.payments"), t("pay.upgrade_desc"), anchor="cloud")
    elif not enabled:
        body = _pitch()
    else:
        body = _connected_panel(deposit_account, bank_accounts, saved=saved)
    return Div(
        page_header(t("nav.payments")),
        _cloud_tabs("payments", has_team_features=_has_team_features()),
        body,
    )


async def _load(token: str) -> tuple[bool, bool, str, list[dict]]:
    relay_ok = False
    try:
        relay_ok = bool((await api.get_relay_status(token)).get("connected"))
    except APIError:
        pass
    status = await api.get_payments_status(token)
    enabled = bool(status.get("enabled"))
    company = await api.get_company(token)
    deposit = company.get("stripe_deposit_account") or ""
    banks: list[dict] = []
    if enabled:
        try:
            banks = (await api.get_bank_accounts(token)).get("items", [])
        except APIError:
            pass
    return relay_ok, enabled, deposit, banks


def setup_routes(app):
    @app.get("/settings/payments")
    async def payments_settings_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        redir = _check_role(request, "admin")
        if redir:
            return redir
        try:
            relay_ok, enabled, deposit, banks = await _load(token)
        except APIError as e:
            return base_shell(flash(str(e.detail)), nav_active="web-access", request=request)
        return base_shell(
            _page(relay_ok, enabled, deposit, banks,
                  saved=request.query_params.get("saved") == "1"),
            nav_active="web-access", request=request)

    @app.post("/settings/payments")
    async def payments_settings_save(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        redir = _check_role(request, "admin")
        if redir:
            return redir
        form = await request.form()
        try:
            await api.patch_company(token, {"stripe_deposit_account": str(form.get("stripe_deposit_account", "")).strip()})
        except APIError as e:
            relay_ok, enabled, deposit, banks = await _load(token)
            return base_shell(Div(flash(str(e.detail)), _page(relay_ok, enabled, deposit, banks)),
                              nav_active="web-access", request=request)
        return RedirectResponse("/settings/payments?saved=1", status_code=302)

    @app.post("/settings/payments/connect")
    async def payments_connect(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        redir = _check_role(request, "admin")
        if redir:
            return redir
        try:
            result = await api.start_payments_connect(token)
        except APIError as e:
            relay_ok, enabled, deposit, banks = await _load(token)
            return base_shell(Div(flash(str(e.detail)), _page(relay_ok, enabled, deposit, banks)),
                              nav_active="web-access", request=request)
        return RedirectResponse(result.get("url", "/settings/payments"), status_code=302)

    @app.post("/settings/payments/disconnect")
    async def payments_disconnect(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        redir = _check_role(request, "admin")
        if redir:
            return redir
        try:
            await api.disconnect_payments(token)
        except APIError:
            pass
        return RedirectResponse("/settings/payments", status_code=302)
