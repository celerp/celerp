# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Send, review and revoke portable accountant-prepared company copies."""
from __future__ import annotations

import uuid

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.config import get_company_id, get_token
from ui.routes.auth import auth_header
from ui.routes.migrations import wizard_page, back_link
from ui.components.shell import flash


async def _token(request: Request, run_id: str) -> str | None:
    token = get_token(request)
    if not token:
        return None
    if run_id:
        run = await api.get_migration_run(token, run_id)
        company_id = str(run["company_id"])
        if company_id != get_company_id(request):
            token = (await api.switch_company(token, company_id))[0]
    return token


def _page(request: Request, *, run_id: str = "", items: list[dict] | None = None,
          error: str = "", sent: dict | None = None):
    action = "/accountant-copies/send" + (f"?from_run={run_id}" if run_id else "")
    return wizard_page(
        request,
        auth_header("Send company copy", "Give the customer their own independent Celerp company."),
        flash(error) if error else "",
        P("This copy includes company records, attachments and settings. It does not include your "
          "users, passwords, bank connections or other installation credentials.", cls="form-hint"),
        Div(
            H3("Company copy sent"),
            P("The recipient can open the email on a computer. You can also share this link directly."),
            Input(type="text", value=sent["link"], readonly=True, cls="form-control",
                  aria_label="Company copy link"),
            cls="mb-md",
        ) if sent else "",
        Form(
            Label("Recipient email", Input(name="recipient", type="email", required=True)),
            Label("Your firm name", Input(name="firm_name", required=True, maxlength="160")),
            Label("Verified Connect email (for replies)",
                  Input(name="reply_to", type="email", required=True)),
            Label("Optional note", Textarea(name="note", rows=3, maxlength="1500")),
            Button("Send company copy", type="submit", cls="btn btn--primary btn--full mt-md"),
            method="post", action=action, cls="auth-form",
        ),
        H3("Copies sent from this company"),
        Ul(*[
            Li(f"{row.get('firm_name', '')}: {row.get('status', '')}",
               f" | opened: {'yes' if row.get('opened') else 'no'}",
               f" | downloaded: {'yes' if row.get('downloaded') else 'no'}",
               Form(
                   Button("Revoke", type="submit", cls="btn btn--secondary"),
                   method="post",
                   action=f"/accountant-copies/{row['id']}/revoke"
                          + (f"?from_run={run_id}" if run_id else ""),
               ) if row.get("status") in ("sent", "pending") else "")
            for row in items or []
        ]) if items else P("No copies sent yet."),
        P(A("Invite a user to this company instead", href="/settings/general?tab=users")),
        back_link(f"/migrations/{run_id}/complete" if run_id else "/settings/general?tab=backup"),
        title="Send company copy",
    )


async def copies_page(request: Request):
    run_id = request.query_params.get("from_run", "")
    try:
        token = await _token(request, run_id)
        if token is None:
            return RedirectResponse("/login", status_code=302)
        items = await api.accountant_copy_list(token)
        return _page(request, run_id=run_id, items=items)
    except APIError as exc:
        return _page(request, run_id=run_id, error=str(exc.detail))


async def send_copy(request: Request):
    run_id = request.query_params.get("from_run", "")
    try:
        token = await _token(request, run_id)
        if token is None:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        result = await api.accountant_copy_send(token, {
            "recipient": str(form.get("recipient") or "").strip(),
            "firm_name": str(form.get("firm_name") or "").strip(),
            "reply_to": str(form.get("reply_to") or "").strip(),
            "note": str(form.get("note") or ""),
        })
        items = await api.accountant_copy_list(token)
        return _page(request, run_id=run_id, items=items, sent=result)
    except APIError as exc:
        return _page(request, run_id=run_id, error=str(exc.detail))


async def revoke_copy(request: Request, handoff_id: str):
    run_id = request.query_params.get("from_run", "")
    try:
        uuid.UUID(handoff_id)
        token = await _token(request, run_id)
        if token is None:
            return RedirectResponse("/login", status_code=302)
        await api.accountant_copy_revoke(token, handoff_id)
    except (ValueError, APIError) as exc:
        return _page(request, run_id=run_id, error=str(getattr(exc, "detail", exc)))
    return RedirectResponse(
        "/accountant-copies" + (f"?from_run={run_id}" if run_id else ""),
        status_code=303,
    )


def accountant_copy_routes(app):
    app.get("/accountant-copies")(copies_page)
    app.post("/accountant-copies/send")(send_copy)
    app.post("/accountant-copies/{handoff_id}/revoke")(revoke_copy)
