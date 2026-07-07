# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings → Payments: connect a Stripe account so customers can pay invoices online."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, flash
from ui.routes.settings import _check_role, _token


def _panel(enabled: bool, deposit_account: str, saved: bool = False) -> FT:
    connect_block = (
        Div(
            P("✓ Connected. The Pay button now appears on shared invoices and in the "
              "send email, so customers can pay you online.", cls="form-hint"),
            Form(Button("Disconnect", type="submit", cls="btn btn--danger"),
                 method="post", action="/settings/payments/disconnect"),
        )
        if enabled else
        Div(
            P("Let customers pay invoices online by card. Connect a Stripe account — "
              "or create one in the same step — and the Pay button turns on automatically.",
              cls="form-hint"),
            Form(Button("Connect with Stripe", type="submit", cls="btn btn--primary"),
                 method="post", action="/settings/payments/connect"),
        )
    )
    return Div(
        page_header("Payments"),
        flash("Saved.") if saved else "",
        connect_block,
        Form(
            Div(Label("Deposit online payments to (account code)", cls="form-label"),
                Input(type="text", name="stripe_deposit_account", value=deposit_account or "",
                      placeholder="e.g. 1110", cls="form-input"),
                P("The cash/bank account online payments are recorded against. "
                  "Defaults to Cash if left blank.", cls="form-hint"),
                cls="form-group"),
            Button("Save", type="submit", cls="btn btn--secondary"),
            method="post", action="/settings/payments", cls="settings-form",
        ),
    )


async def _load(token: str) -> tuple[bool, str]:
    status = await api.get_payments_status(token)
    company = await api.get_company(token)
    return bool(status.get("enabled")), company.get("stripe_deposit_account") or ""


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
            enabled, deposit = await _load(token)
        except APIError as e:
            return base_shell(flash(str(e.detail)), nav_active="settings", request=request)
        return base_shell(_panel(enabled, deposit, saved=request.query_params.get("saved") == "1"),
                          nav_active="settings", request=request)

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
            enabled, deposit = await _load(token)
            return base_shell(Div(flash(str(e.detail)), _panel(enabled, deposit)),
                              nav_active="settings", request=request)
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
            enabled, deposit = await _load(token)
            return base_shell(Div(flash(str(e.detail)), _panel(enabled, deposit)),
                              nav_active="settings", request=request)
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
