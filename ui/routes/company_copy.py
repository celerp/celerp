# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Independent company copies: make one from a company, open one as a new company.

Making a copy (`/company-copy`) is one confirmation, then the file download. Opening
a copy runs in the two setup modes the migration wizard uses:

- bootstrap (`/setup/open-copy`): no user exists yet; opening the copy creates the
  first owner.
- company (`/setup/new-company/open-copy`): a signed-in owner adds the copy as
  another company.

Steps: upload, preview (company and firm shown before anything is written), open,
then the ready page. The upload token lives only in an HttpOnly cookie scoped to
the mode's base path.
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
    COMPANY,
    WizardMode,
    account_fields,
    back_link,
    gate,
    setup_code_field,
    setup_code_required,
    wizard_page,
)

MAKE = WizardMode("company", "/company-copy", "/")
OPEN_BOOTSTRAP = WizardMode("bootstrap", "/setup/open-copy", "/setup")
OPEN_COMPANY = WizardMode("company", "/setup/new-company/open-copy", "/setup/new-company")

UPLOAD_COOKIE = "celerp_company_copy_upload"
UPLOAD_TTL_SECONDS = 24 * 3600
_PREVIEW_FIELDS = ("company_name", "prepared_by", "created_at", "records", "attachments")


def _html(page, status_code: int = 200) -> HTMLResponse:
    return page if isinstance(page, HTMLResponse) else HTMLResponse(to_xml(page), status_code=status_code)


# ---------------------------------------------------------------------------
# Make a copy
# ---------------------------------------------------------------------------

async def _source(request: Request, run_id: str) -> tuple[dict | None, str | None]:
    """The company to copy: the migration run's company when a run is named, else the
    session's company. Returns ({"name", "prepared_by", "company_id"}, error)."""
    token = get_token(request)
    try:
        if run_id:
            run = await api.get_migration_run(token, run_id)
            return {"name": run.get("company_name", ""), "prepared_by": run.get("prepared_by") or "",
                    "company_id": str(run["company_id"])}, None
        company = await api.get_company(token)
        return {"name": company.get("name", ""), "prepared_by": "", "company_id": get_company_id(request)}, None
    except APIError as e:
        return None, str(e.detail)


def _make_page(request: Request, source: dict, run_id: str, *, prepared_by: str, error: str | None = None):
    cancel = f"/migrations/{run_id}/complete" if run_id else "/"
    return wizard_page(
        request,
        auth_header(t("company_copy.make_title"), source["name"]),
        flash(error) if error else "",
        P(t("company_copy.only_this_company")),
        H3(t("company_copy.included")),
        Ul(Li(t("company_copy.included_books")), Li(t("company_copy.included_settings")),
           Li(t("company_copy.included_attachments"))),
        H3(t("company_copy.not_included")),
        Ul(Li(t("company_copy.not_included_users")), Li(t("company_copy.not_included_connections")),
           Li(t("company_copy.not_included_other"))),
        P(t("company_copy.snapshot"), cls="form-hint"),
        Form(
            Div(
                Label(t("migration.prepared_by"), For="prepared_by", cls="form-label"),
                Input(type="text", id="prepared_by", name="prepared_by", value=prepared_by, cls="form-input"),
                P(t("company_copy.prepared_by_hint"), cls="form-hint"),
                cls="form-group",
            ),
            Input(type="hidden", name="run_id", value=run_id),
            Button(t("company_copy.create"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=MAKE.base, cls="auth-form",
        ),
        P(A(t("btn.cancel"), href=cancel, cls="auth-link"), cls="auth-alt-action"),
    )


async def _make_confirm(request: Request):
    if (denied := await gate(request, MAKE)) is not None:
        return denied
    run_id = request.query_params.get("from_run", "")
    source, error = await _source(request, run_id)
    if source is None:
        return wizard_page(request, auth_header(t("company_copy.make_title")), flash(error), back_link("/"))
    return _make_page(request, source, run_id, prepared_by=source["prepared_by"])


async def _make(request: Request):
    """Create the copy. A copy named by a migration run is made of that run's company,
    so the session moves to it first; the ready page names the company."""
    if (denied := await gate(request, MAKE)) is not None:
        return denied
    form = await request.form()
    run_id = str(form.get("run_id", "")).strip()
    prepared_by = str(form.get("prepared_by", "")).strip()
    source, error = await _source(request, run_id)
    if source is None:
        return wizard_page(request, auth_header(t("company_copy.make_title")), flash(error), back_link("/"))
    token, tokens = get_token(request), None
    try:
        if source["company_id"] != get_company_id(request):
            tokens = await api.switch_company(token, source["company_id"])
            token = tokens[0]
        made = await api.company_copy_create(token, prepared_by or None)
    except APIError as e:
        return _make_page(request, source, run_id, prepared_by=prepared_by, error=str(e.detail))
    resp = _html(wizard_page(
        request,
        auth_header(t("company_copy.ready_title"), made["company_name"]),
        P(t("company_copy.file_only")),
        A(t("company_copy.download"), href=f"{MAKE.base}/{made['copy_id']}/download",
          cls="btn btn--primary btn--full"),
        A(t("migration.move_another"), href=COMPANY.base, cls="btn btn--secondary btn--full mt-sm"),
        P(A(t("migration.open_company"), href="/", cls="auth-link"), cls="auth-alt-action"),
    ))
    if tokens is not None:
        set_session_cookies(resp, tokens[0], tokens[1], request)
    return resp


async def _download(request: Request, copy_id: str):
    if (denied := await gate(request, MAKE)) is not None:
        return denied
    try:
        chunks, headers = await api.company_copy_download(get_token(request), copy_id)
    except APIError as e:
        return wizard_page(request, auth_header(t("company_copy.make_title")), flash(str(e.detail)),
                           A(t("company_copy.make_new"), href=MAKE.base, cls="btn btn--primary btn--full"),
                           back_link("/"), status_code=e.status if e.status >= 400 else 502)
    return StreamingResponse(chunks, media_type="application/octet-stream",
                             headers={k.title(): v for k, v in headers.items() if k != "content-type"})


# ---------------------------------------------------------------------------
# Open a copy
# ---------------------------------------------------------------------------

def _api_token(request: Request, mode: WizardMode) -> str | None:
    return None if mode.bootstrap else get_token(request)


def _set_upload_cookie(resp, token: str, mode: WizardMode, request: Request) -> None:
    resp.set_cookie(UPLOAD_COOKIE, token, max_age=UPLOAD_TTL_SECONDS, path=mode.base, httponly=True,
                    samesite="strict", secure=session_cookie_secure(request), domain=cookie_domain(request))


def _clear_upload_cookie(resp, mode: WizardMode, request: Request) -> None:
    resp.delete_cookie(UPLOAD_COOKIE, path=mode.base, domain=cookie_domain(request))


async def _upload_page(request: Request, mode: WizardMode, error: str | None = None, status_code: int = 200):
    return wizard_page(
        request,
        auth_header(t("company_copy.open_title"), t("company_copy.open_subtitle")),
        flash(error) if error else "",
        Form(
            Div(
                Label(t("company_copy.file_label"), For="file", cls="form-label"),
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
    )


def _upload_again(request: Request, mode: WizardMode, message: str):
    resp = _html(wizard_page(
        request, auth_header(t("company_copy.open_title")), flash(message),
        A(t("migration.upload_again"), href=mode.base, cls="btn btn--primary btn--full"),
        back_link(mode.back),
    ))
    _clear_upload_cookie(resp, mode, request)
    return resp


async def _preview_page(request: Request, mode: WizardMode, preview: dict, *, values: dict | None = None,
                        error: str | None = None):
    values = values or {}
    firm = preview.get("prepared_by")
    rows = [
        (t("company_copy.records"), str(preview.get("records", "--"))),
        (t("company_copy.attachments"), str(preview.get("attachments", "--"))),
        (t("company_copy.copy_date"), str(preview.get("created_at") or "--")[:10]),
    ]
    return wizard_page(
        request,
        auth_header(preview.get("company_name", ""),
                    t("company_copy.prepared_for_you", firm=firm) if firm else t("company_copy.independent_copy")),
        flash(error) if error else "",
        Table(Tbody(*[Tr(Td(k), Td(v, cls="cell--number")) for k, v in rows]), cls="data-table"),
        P(t("company_copy.nothing_written"), cls="form-hint"),
        Form(
            *[Input(type="hidden", name=f"preview_{k}", value=str(preview.get(k) or "")) for k in _PREVIEW_FIELDS],
            *(account_fields(values) if mode.bootstrap else []),
            setup_code_field() if await setup_code_required(mode) else "",
            Button(t("company_copy.open"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/open", cls="auth-form",
        ),
        P(A(t("company_copy.choose_other"), href=mode.base, cls="auth-link"), cls="auth-alt-action"),
    )


async def _open_start(request: Request, mode: WizardMode):
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
        preview = await api.company_copy_read(_api_token(request, mode), upload.filename, upload.file,
                                              setup_code=setup_code)
    except APIError as e:
        return await _upload_page(request, mode, str(e.detail), status_code=e.status if e.status >= 400 else 502)
    resp = _html(await _preview_page(request, mode, preview))
    _set_upload_cookie(resp, preview["upload_token"], mode, request)
    return resp


def _bootstrap_error(values: dict) -> str | None:
    from celerp.services.auth import MIN_PASSWORD_LENGTH
    if not all(values.get(k) for k in ("name", "email", "password")):
        return t("settings.all_fields_required")
    if values["password"] != values.get("confirm_password"):
        return t("settings.passwords_do_not_match")
    if len(values["password"]) < MIN_PASSWORD_LENGTH:
        return t("settings.password_min_length")
    return None


async def _open(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if not upload_token:
        return _upload_again(request, mode, t("company_copy.upload_expired"))
    form = await request.form()
    preview = {k: str(form.get(f"preview_{k}", "")) for k in _PREVIEW_FIELDS}
    values = {k: str(form.get(k, "")).strip() for k in ("name", "email")}
    values["password"] = str(form.get("password", ""))
    values["confirm_password"] = str(form.get("confirm_password", ""))
    setup_code = str(form.get("setup_code", "")).strip() or None
    try:
        if mode.bootstrap:
            if (problem := _bootstrap_error(values)) is not None:
                return await _preview_page(request, mode, preview, values=values, error=problem)
            if await setup_code_required(mode) and not setup_code:
                return await _preview_page(request, mode, preview, values=values,
                                           error=t("auth.setup_code_required"))
            opened = await api.company_copy_bootstrap_open(upload_token, values["name"], values["email"],
                                                           values["password"], setup_code=setup_code)
            tokens = opened["access_token"], opened["refresh_token"]
        else:
            opened = await api.company_copy_open(get_token(request), upload_token)
            tokens = await api.switch_company(get_token(request), opened["company_id"])
    except APIError as e:
        if e.status == 409:
            return _upload_again(request, mode, str(e.detail))
        detail = " ".join(str(v) for v in e.detail.values()) if isinstance(e.detail, dict) else str(e.detail)
        return await _preview_page(request, mode, preview, values=values, error=detail)
    # The new company is opened for the user: the ready page names it and its totals.
    resp = RedirectResponse(f"{mode.base}/done", status_code=303)
    set_session_cookies(resp, tokens[0], tokens[1], request)
    _clear_upload_cookie(resp, mode, request)
    return resp


async def _totals(token: str) -> list[tuple[str, str]]:
    """Trial balance, AR and AP totals of the session's company; `--` for any that fail."""
    async def fetch(getter, pick):
        try:
            return format_value(pick(await getter(token)), "money")
        except (APIError, KeyError, TypeError, ValueError):
            return "--"

    def aging_total(report: dict) -> float:
        return sum(float(line.get("total") or 0) for line in report.get("lines") or [])

    return [
        (t("company_copy.total_debits"), await fetch(api.get_trial_balance, lambda r: r["total_debit"])),
        (t("company_copy.total_credits"), await fetch(api.get_trial_balance, lambda r: r["total_credit"])),
        (t("company_copy.receivables"), await fetch(api.get_ar_aging, aging_total)),
        (t("company_copy.payables"), await fetch(api.get_ap_aging, aging_total)),
    ]


async def _done(request: Request, mode: WizardMode):
    token = get_token(request)
    if not token:
        return RedirectResponse("/login", status_code=302)
    try:
        name = (await api.get_company(token)).get("name", "")
    except APIError:
        name = ""
    return wizard_page(
        request,
        auth_header(t("migration.success_title"), name),
        Table(
            Thead(Tr(Th(t("migration.col_check")), Th(t("migration.col_celerp"), cls="cell--number"))),
            Tbody(*[Tr(Td(k), Td(v, cls="cell--number")) for k, v in await _totals(token)]),
            cls="data-table",
        ),
        P(t("company_copy.free_locally"), cls="form-hint"),
        A(t("migration.open_company"), href="/", cls="btn btn--primary btn--full"),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _bind(handler, mode: WizardMode):
    async def route(request: Request):
        return await handler(request, mode)
    route.__name__ = f"company_copy_{mode.key}{handler.__name__}"
    return route


def company_copy_routes(app) -> None:
    app.get(MAKE.base)(_make_confirm)
    app.post(MAKE.base)(_make)
    app.get(MAKE.base + "/{copy_id}/download")(_download)
    for mode in (OPEN_BOOTSTRAP, OPEN_COMPANY):
        for method, suffix, handler in (("get", "", _open_start), ("post", "/read", _read),
                                        ("post", "/open", _open), ("get", "/done", _done)):
            getattr(app, method)(f"{mode.base}{suffix}")(_bind(handler, mode))
