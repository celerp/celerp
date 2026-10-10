# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Web Access > Payments: connect a Stripe account so customers can pay invoices online.

Five states, five jobs:
  no relay       - upsell Web Access (payments cannot work without it)
  no Stripe      - a sales page: one benefit-led pitch, one CTA, fee in the fine print
  connected      - an operating page: status, deposit-account selector, disconnect
  disconnecting  - a status line: new payments are stopped while existing ones finish
  revoked        - Stripe access was withdrawn while payments were in progress: one
                   action, reconnect the same account so they can be checked

Below any of the last four, the payments received for a company or invoice that no
longer exists, and the refunds of online payments that could not be applied yet, when
there are any. A payment's invoice cell is click-to-edit: choosing an invoice of the
current company records the payment there.
"""

from __future__ import annotations

from fasthtml.common import *
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.cloud_gate import upgrade_banner
from ui.components.shell import base_shell, page_header, page_title, flash
from ui.components.table import (
    EMPTY, add_new_option, bank_account_options, display_cell, editable_cell, fmt_money,
)
from ui.i18n import t
from ui.routes.settings import _check_permission, _token
from ui.routes.settings_cloud import (
    _cloud_tabs, _commercial_state, _has_team_features, _relay_has_paid_access,
)


def _pitch() -> FT:
    """Pre-connect sales page: one message, one action. The deposit selector and
    other admin controls stay out of sight until there is something to admin."""
    return Div(
        H3(t("pay.pitch_head"), style="margin-bottom:12px;"),
        Ul(
            Li(t("pay.pitch_li1")),
            Li(t("pay.pitch_li2")),
            Li(t("pay.pitch_li3")),
            style="margin:0 0 20px;line-height:1.9;",
        ),
        Form(Button(t("pay.connect_with_stripe"), type="submit", cls="btn btn--primary"),
             method="post", action="/settings/payments/connect"),
        # Button microcopy: "Free to set up." tight under the CTA, where it
        # reads as part of the button's promise. The fine print is capped at
        # 400px so its lines stay readable; the Stripe line sits apart, one
        # step smaller and lighter, as a trust footer rather than more copy.
        P(t("pay.pitch_free"), cls="form-hint", style="margin-top:8px;"),
        P(t("pay.pitch_fineprint"), cls="form-hint",
          style="margin-top:14px;max-width:400px;line-height:1.6;"),
        P(t("pay.pitch_stripe_cred"),
          style="margin-top:12px;max-width:400px;font-size:0.7rem;color:#9aa0b5;"),
        cls="settings-card",
    )


def _connected_panel(deposit_account: str, bank_accounts: list[dict], saved: bool = False) -> FT:
    """Post-connect operating page: status line, deposit selector, disconnect."""
    _new_opt, _new_js = add_new_option(t("acct.add_bank_account"), "/settings/accounting/bank-accounts/new")
    deposit_select = Select(
        # Blank = the server-side default: payments book to the default deposit account.
        Option(t("pay.deposit_default_option"), value="", selected=not deposit_account),
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
             data_confirm=t("pay.disconnect_confirm"),
             onsubmit="return confirm(this.dataset.confirm)",
             style="margin-top:24px;"),
    )


def _revoked() -> FT:
    """Access withdrawn at Stripe with payments still in progress: reconnecting the
    same account is the one way to finish checking them."""
    return Div(
        P(t("pay.settings_revoked"), cls="form-hint"),
        Form(Button(t("pay.reconnect_stripe"), type="submit", cls="btn btn--primary"),
             method="post", action="/settings/payments/connect"),
        cls="settings-card",
    )


def _unmatched(unmatched: dict) -> FT | str:
    """Payments received that could not be recorded on an invoice, then the refunds of
    online payments that could not be applied yet, each newest first."""
    return Div(_unmatched_payments(unmatched.get("items", [])),
               _unmatched_refunds(unmatched.get("refunds", [])))


def _where(row: dict) -> tuple:
    """The company and document cells of an unmatched row: their names, or "--"
    marked deleted once they no longer exist here."""
    return tuple(Td(name) if name else Td(EMPTY, " ", Span(t("pay.unmatched_deleted"), cls="text-muted"))
                 for name in (row.get("company_name"), row.get("document_ref")))


def _invoice_url(reference: str) -> str:
    return f"/settings/payments/unmatched/{quote(reference, safe='')}/invoice"


def _invoice_cell(reference: str, document_ref: str | None) -> FT:
    """The invoice an unmatched payment was made for, "--" once it no longer exists:
    click-to-edit, to record the payment on an invoice of the current company."""
    return display_cell(reference, "invoice", document_ref, cell_type="select",
                        edit_url=f"{_invoice_url(reference)}/edit")


def _invoice_editor(reference: str, invoices: list[dict], value: str = "") -> FT:
    """The searchable list of the invoices the payment can be recorded on: Enter
    records it, Esc puts the cell back."""
    labels = {i["id"]: " - ".join(x for x in (i["ref"], i.get("contact_name"),
                                                fmt_money(i["outstanding"], i["currency"])) if x)
              for i in invoices}
    return editable_cell(reference, "invoice", value, cell_type="select", options=list(labels),
                         label_map=labels, patch_url=_invoice_url(reference), searchable=True,
                         aria_label=t("th.document"))


def _refusal(error: APIError) -> str:
    """Why the payment was not recorded, in the user's language."""
    reason = (error.data or {}).get("reason")
    key = f"pay.unmatched_refused_{reason}"
    return t(key) if reason and t(key) != key else str(error.detail)


def _unmatched_payments(payments: list[dict]) -> FT | str:
    if not payments:
        return ""
    return Div(
        H3(t("pay.unmatched_head"), cls="section-title"),
        P(t("pay.unmatched_hint"), cls="form-hint"),
        Div(Table(
            Thead(Tr(Th(t("pay.unmatched_received")), Th(t("pay.unmatched_paid_on")), Th(t("label.reference")),
                     Th(t("label.amount"), cls="cell--number"), Th(t("th.company")), Th(t("th.document")))),
            Tbody(*[Tr(Td(p.get("received_on") or EMPTY), Td(p.get("paid_on") or EMPTY), Td(p["reference"]),
                       Td(fmt_money(p["amount"], p["currency"]), cls="cell--money"),
                       _where(p)[0], _invoice_cell(p["reference"], p.get("document_ref")))
                    for p in payments]),
            cls="data-table",
        ), cls="table-scroll-wrap"),
        cls="settings-card", style="margin-top:24px;",
    )


def _unmatched_refunds(refunds: list[dict]) -> FT | str:
    if not refunds:
        return ""
    return Div(
        H3(t("pay.unmatched_refunds_head"), cls="section-title"),
        P(t("pay.unmatched_refunds_hint"), cls="form-hint"),
        Div(Table(
            Thead(Tr(Th(t("pay.unmatched_received")), Th(t("pay.unmatched_refunded_on")), Th(t("label.reference")),
                     Th(t("label.amount"), cls="cell--number"), Th(t("th.type")), Th(t("th.company")),
                     Th(t("th.document")))),
            Tbody(*[Tr(Td(r.get("received_on") or EMPTY), Td(r.get("refunded_on") or EMPTY), Td(r["reference"]),
                       Td(fmt_money(r["amount"], r["currency"]), cls="cell--money"),
                       Td(t(f"pay.refund_{r['transition']}")), *_where(r))
                    for r in refunds]),
            cls="data-table",
        ), cls="table-scroll-wrap"),
        cls="settings-card", style="margin-top:24px;",
    )


def _page(relay_ok: bool, enabled: bool, deposit_account: str,
          bank_accounts: list[dict], has_team_features: bool, saved: bool = False,
          state: str | None = None, unmatched: dict | None = None) -> FT:
    if not relay_ok:
        body = upgrade_banner(t("nav.payments"), t("pay.upgrade_desc"), plan="cloud")
    elif state == "revoked":
        body = _revoked()
    elif state == "disconnecting":
        body = Div(P(t("pay.settings_disconnecting"), cls="form-hint"), cls="settings-card")
    elif not enabled:
        body = _pitch()
    else:
        body = _connected_panel(deposit_account, bank_accounts, saved=saved)
    return Div(
        page_header(t("nav.payments")),
        _cloud_tabs("payments", has_team_features=has_team_features),
        body,
        _unmatched(unmatched or {}) if relay_ok else "",
    )


async def _load(token: str) -> tuple[bool, bool, str, list[dict], str | None, dict]:
    relay_ok = False
    try:
        relay_ok = _relay_has_paid_access(await api.get_relay_status(token))
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
    unmatched: dict = {}
    try:
        unmatched = await api.get_unmatched_payments(token)
    except APIError:
        pass  # only the installation owner sees them
    return relay_ok, enabled, deposit, banks, status.get("state"), unmatched


async def _page_with_error(request: Request, token: str, error: APIError):
    relay_ok, enabled, deposit, banks, state, unmatched = await _load(token)
    has_team = _has_team_features(await _commercial_state(request))
    return await base_shell(
        Div(flash(str(error.detail)),
            _page(relay_ok, enabled, deposit, banks, has_team, state=state,
                  unmatched=unmatched)),
        title=page_title("nav.payments"), nav_active="web-access", request=request)


async def _payment_row(token: str, reference: str) -> dict | None:
    """The unmatched payment *reference* as the list shows it, or None once it left."""
    return next((p for p in (await api.get_unmatched_payments(token)).get("items", [])
                 if p["reference"] == reference), None)


def setup_routes(app):
    @app.get("/settings/payments/unmatched/{reference}/invoice/edit")
    async def unmatched_invoice_edit(request: Request, reference: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            invoices = (await api.get_unmatched_invoices(token, reference)).get("items", [])
        except APIError as e:
            row = await _payment_row(token, reference) if e.status == 409 else None
            return _invoice_cell(reference, (row or {}).get("document_ref"))(P(_refusal(e), cls="cell-error"))
        return _invoice_editor(reference, invoices)

    @app.patch("/settings/payments/unmatched/{reference}/invoice")
    async def unmatched_invoice_record(request: Request, reference: str):
        """Record the payment on the chosen invoice; the row leaves the list. Refused,
        the editor stays open with the reason and the row stays (GDR 2e)."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        entity_id = str((await request.form()).get("value", "")).strip()
        if entity_id:
            try:
                await api.record_unmatched_payment(token, reference, entity_id)
            except APIError as e:
                try:
                    invoices = (await api.get_unmatched_invoices(token, reference)).get("items", [])
                except APIError:
                    invoices = []
                return _invoice_editor(reference, invoices, entity_id)(P(_refusal(e), cls="cell-error"))
            return _R("", headers={"HX-Retarget": "closest tr", "HX-Reswap": "delete"})
        row = await _payment_row(token, reference)
        return _invoice_cell(reference, (row or {}).get("document_ref"))

    @app.get("/settings/payments/unmatched/{reference}/invoice/display")
    async def unmatched_invoice_display(request: Request, reference: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            row = await _payment_row(token, reference)
        except APIError as e:
            return P(e.detail, cls="cell-error")
        return _invoice_cell(reference, (row or {}).get("document_ref"))


    @app.get("/settings/payments")
    async def payments_settings_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        redir = await _check_permission(request, "manage_integrations")
        if redir:
            return redir
        try:
            relay_ok, enabled, deposit, banks, state, unmatched = await _load(token)
        except APIError as e:
            return await base_shell(flash(str(e.detail)), title=page_title("nav.payments"),
                                    nav_active="web-access", request=request)
        has_team = _has_team_features(await _commercial_state(request))
        return await base_shell(
            _page(relay_ok, enabled, deposit, banks, has_team,
                  saved=request.query_params.get("saved") == "1", state=state,
                  unmatched=unmatched),
            title=page_title("nav.payments"), nav_active="web-access", request=request)

    @app.post("/settings/payments")
    async def payments_settings_save(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        redir = await _check_permission(request, "manage_integrations")
        if redir:
            return redir
        form = await request.form()
        try:
            await api.patch_company(token, {"stripe_deposit_account": str(form.get("stripe_deposit_account", "")).strip()})
        except APIError as e:
            return await _page_with_error(request, token, e)
        return RedirectResponse("/settings/payments?saved=1", status_code=302)

    @app.post("/settings/payments/connect")
    async def payments_connect(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        redir = await _check_permission(request, "manage_integrations")
        if redir:
            return redir
        try:
            result = await api.start_payments_connect(token)
        except APIError as e:
            return await _page_with_error(request, token, e)
        return RedirectResponse(result.get("url", "/settings/payments"), status_code=302)

    @app.post("/settings/payments/disconnect")
    async def payments_disconnect(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        redir = await _check_permission(request, "manage_integrations")
        if redir:
            return redir
        try:
            await api.disconnect_payments(token)
        except APIError as e:
            return await _page_with_error(request, token, e)
        return RedirectResponse("/settings/payments", status_code=302)
