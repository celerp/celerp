# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Company backups: download the session company's backup, restore one as a new company.

Downloading (`/company-backup/download`) returns the file directly. From the migration
completion page, `from_run` names the migrated company, whose backup carries the
migration's provenance.

Restoring is one wizard on three entry points:

- Settings (`/settings/restore-backup`): a signed-in owner restores a backup beside the
  current company.
- Add company (`/setup/new-company/restore-backup`): a signed-in owner adds a company
  from a backup.
- Fresh installation (`/setup/restore-backup`): no user exists yet; the restore creates
  the first owner.

Steps: upload, preview (nothing written yet), restore, then the restored company. The
upload token lives only in an HttpOnly cookie scoped to the entry point's base path.
"""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, StreamingResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import flash
from ui.components.table import format_value
from ui.config import cookie_domain, get_company_id, get_token, session_cookie_secure, set_session_cookies
from ui.i18n import t
from ui.routes.auth import auth_header
from ui.routes.migrations import (
    WizardMode,
    account_error,
    account_fields,
    api_token,
    back_link,
    bind,
    gate,
    setup_code_field,
    setup_code_required,
    upload_again_page,
    wizard_page,
)

DOWNLOAD = "/company-backup/download"
BACKUP_TAB = "/settings/general?tab=backup"
_TITLE = "company_backup.restore_title"
_OWNER_ONLY = "company_backup.owner_only"
SETTINGS = WizardMode("settings", "/settings/restore-backup", BACKUP_TAB, _TITLE, _OWNER_ONLY)
NEW_COMPANY = WizardMode("new_company", "/setup/new-company/restore-backup", "/setup/new-company",
                         _TITLE, _OWNER_ONLY)
BOOTSTRAP = WizardMode("bootstrap", "/setup/restore-backup", "/setup", _TITLE, _OWNER_ONLY)

UPLOAD_COOKIE = "celerp_company_backup_upload"
UPLOAD_TTL_SECONDS = 24 * 3600
_PREVIEW_FIELDS = ("company_name", "created_at", "records", "attachments", "prepared_by")


def _html(page, status_code: int = 200) -> HTMLResponse:
    return page if isinstance(page, HTMLResponse) else HTMLResponse(to_xml(page), status_code=status_code)


def _message(e: APIError) -> str:
    """An API refusal as one plain sentence: field problems joined, anything else not shown raw."""
    if isinstance(e.detail, dict):
        return " ".join(str(v) for v in e.detail.values())
    return e.detail if isinstance(e.detail, str) else t("error.invalid_value")


def _status(e: APIError) -> int:
    return e.status if e.status >= 400 else 502


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

async def _download(request: Request):
    """Serve the backup file. A migration run's company may not be the session's
    company yet, so its backup is made with a token for that company; the session
    itself stays where it is."""
    token = get_token(request)
    if not token:
        return RedirectResponse("/login", status_code=302)
    run_id = request.query_params.get("from_run", "")
    try:
        if run_id:
            company_id = str((await api.get_migration_run(token, run_id))["company_id"])
            if company_id != get_company_id(request):
                token = (await api.switch_company(token, company_id))[0]
        chunks, headers = await api.company_backup_download(token, run_id or None)
    except APIError as e:
        back = f"/migrations/{run_id}/complete" if run_id else BACKUP_TAB
        return wizard_page(request, auth_header(t("company_backup.download")), flash(_message(e)),
                           back_link(back), status_code=_status(e), title="company_backup.download")
    return StreamingResponse(chunks, media_type="application/octet-stream",
                             headers={k.title(): v for k, v in headers.items() if k != "content-type"})


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _set_upload_cookie(resp, token: str, mode: WizardMode, request: Request) -> None:
    resp.set_cookie(UPLOAD_COOKIE, token, max_age=UPLOAD_TTL_SECONDS, path=mode.base, httponly=True,
                    samesite="strict", secure=session_cookie_secure(request), domain=cookie_domain(request))


def _clear_upload_cookie(resp, mode: WizardMode, request: Request) -> None:
    resp.delete_cookie(UPLOAD_COOKIE, path=mode.base, domain=cookie_domain(request))


async def _upload_page(request: Request, mode: WizardMode, error: str | None = None, status_code: int = 200):
    return wizard_page(
        request,
        auth_header(t(_TITLE), t("company_backup.upload_subtitle")),
        flash(error) if error else "",
        Form(
            Div(
                Label(t("company_backup.file_label"), For="file", cls="form-label"),
                Input(type="file", id="file", name="file", required=True, accept=".celerp-company",
                      cls="form-input"),
                cls="form-group",
            ),
            setup_code_field() if await setup_code_required(mode) else "",
            Button(t("btn.continue"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/read", enctype="multipart/form-data", cls="auth-form",
        ),
        back_link(mode.back),
        status_code=status_code,
        title=_TITLE,
    )


def _upload_again(request: Request, mode: WizardMode, message: str):
    resp = upload_again_page(request, mode, message)
    _clear_upload_cookie(resp, mode, request)
    return resp


async def _preview_page(request: Request, mode: WizardMode, preview: dict, *, values: dict | None = None,
                        error: str | None = None):
    values = values or {}
    rows = [
        (t("company_backup.backup_date"), format_value(preview.get("created_at"), "date")),
        (t("company_backup.records"), str(preview.get("records") or "--")),
        (t("company_backup.attachments"), str(preview.get("attachments") or "--")),
    ]
    if preview.get("prepared_by"):
        rows.append((t("migration.prepared_by"), preview["prepared_by"]))
    return wizard_page(
        request,
        auth_header(preview.get("company_name", ""), t("company_backup.preview_subtitle")),
        flash(error) if error else "",
        Table(Tbody(*[Tr(Td(k), Td(v)) for k, v in rows]), cls="data-table"),
        P(t("company_backup.separate_company"), cls="form-hint") if mode is SETTINGS else "",
        P(t("company_backup.nothing_written"), cls="form-hint"),
        Form(
            *[Input(type="hidden", name=f"preview_{k}", value=str(preview.get(k) or "")) for k in _PREVIEW_FIELDS],
            *(account_fields(values) if mode.bootstrap else []),
            setup_code_field() if await setup_code_required(mode) else "",
            Button(t("company_backup.restore"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/restore", cls="auth-form",
        ),
        P(A(t("company_backup.choose_other"), href=mode.base, cls="auth-link"), cls="auth-alt-action"),
        title=_TITLE,
    )


async def _start(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    return await _upload_page(request, mode)


async def _read(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    form = await request.form()
    upload = form.get("file")
    if not getattr(upload, "filename", ""):
        return await _upload_page(request, mode, t("migration.choose_file"))
    setup_code = str(form.get("setup_code", "")).strip() or None
    try:
        summary = await api.company_backup_read(api_token(request, mode), upload.filename, upload.file,
                                                setup_code=setup_code)
    except APIError as e:
        return await _upload_page(request, mode, _message(e), status_code=_status(e))
    preview = {**summary, "prepared_by": (summary.get("provenance") or {}).get("prepared_by")}
    resp = _html(await _preview_page(request, mode, preview))
    _set_upload_cookie(resp, summary["upload_token"], mode, request)
    return resp


async def _restore(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if not upload_token:
        return _upload_again(request, mode, t("company_backup.upload_expired"))
    form = await request.form()
    preview = {k: str(form.get(f"preview_{k}", "")) for k in _PREVIEW_FIELDS}
    values = {k: str(form.get(k, "")).strip() for k in ("name", "email")}
    values["password"] = str(form.get("password", ""))
    values["confirm_password"] = str(form.get("confirm_password", ""))
    setup_code = str(form.get("setup_code", "")).strip() or None
    try:
        if mode.bootstrap:
            if (problem := account_error(values)) is not None:
                return await _preview_page(request, mode, preview, values=values, error=problem)
            if await setup_code_required(mode) and not setup_code:
                return await _preview_page(request, mode, preview, values=values,
                                           error=t("auth.setup_code_required"))
            restored = await api.company_backup_bootstrap_restore(upload_token, values["name"], values["email"],
                                                                  values["password"], setup_code=setup_code)
        else:
            restored = await api.company_backup_restore(get_token(request), upload_token, mode.key)
    except APIError as e:
        # A refused backup or a gone upload starts over; account and setup code problems
        # are corrected on the preview.
        if e.status in (409, 422) and not isinstance(e.detail, dict):
            return _upload_again(request, mode, _message(e))
        return await _preview_page(request, mode, preview, values=values, error=_message(e))
    # The session moves to the restored company; the next page says so.
    resp = RedirectResponse(f"{mode.base}/done", status_code=303)
    set_session_cookies(resp, restored["access_token"], restored["refresh_token"], request)
    _clear_upload_cookie(resp, mode, request)
    return resp


async def _done(request: Request, mode: WizardMode):
    """The restored company, now the session's company, with the date of its backup."""
    token = get_token(request)
    if not token:
        return RedirectResponse("/login", status_code=302)
    try:
        company = await api.get_company(token)
    except APIError:
        company = {}
    restored = (company.get("settings") or {}).get("restored_backup") or {}
    return wizard_page(
        request,
        auth_header(t("company_backup.restored_title"), company.get("name", "")),
        P(t("company_backup.restored_notice", date=format_value(restored["created_at"], "date")),
          cls="flash flash--success") if restored.get("created_at") else "",
        A(t("migration.open_company"), href="/", cls="btn btn--primary btn--full"),
        title=_TITLE,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def company_backup_routes(app) -> None:
    app.get(DOWNLOAD)(_download)
    for mode in (SETTINGS, NEW_COMPANY, BOOTSTRAP):
        for method, suffix, handler in (("get", "", _start), ("post", "/read", _read),
                                        ("post", "/restore", _restore), ("get", "/done", _done)):
            getattr(app, method)(f"{mode.base}{suffix}")(bind(handler, mode, "company_backup"))
