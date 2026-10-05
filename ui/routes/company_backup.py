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
- Start company (`/setup/start-company/restore-backup`): a login left with no company,
  after its last company was reset, restores one with its email and password.

Steps: upload, preview (nothing written yet), restore, then the restored company. The
preview names the new company, editable, and says what the backup brings and what stays
behind. Modules the backup needs are imported or turned on from the preview, restarting
Celerp when that is needed, and the same backup is shown again afterwards. A backup
already restored here opens that company instead, giving its missing team access where
the preview said so; one restored as a company since deactivated is reactivated instead
of copied. A preview that no longer holds is shown again, updated. Choosing another file
deletes the upload. When the restore's answer is lost, the page says the result is not
confirmed and sending it again finishes it or opens the company it made.

The upload token, and what the restore did for the page after it, live only in HttpOnly
cookies scoped to the entry point's base path.
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
from ui.routes.auth import START_COMPANY as START_COMPANY_PAGE
from ui.routes.auth import START_COMPANY_RESTORE, auth_header
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
    sign_in_fields,
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
START_COMPANY = WizardMode("start_company", START_COMPANY_RESTORE, START_COMPANY_PAGE, _TITLE, _OWNER_ONLY)

USERS_AND_ROLES = "/settings/general?tab=users"

UPLOAD_COOKIE = "celerp_company_backup_upload"
UPLOAD_TTL_SECONDS = 24 * 3600
OUTCOME_COOKIE = "celerp_company_backup_outcome"
OUTCOME_TTL_SECONDS = 300
_CREATED, _OPENED, _TEAM_ADDED, _REACTIVATED = "created", "opened_existing", "opened_existing_team_added", "reactivated"
_OUTCOMES = frozenset({_CREATED, _OPENED, _TEAM_ADDED, _REACTIVATED})


def _html(page, status_code: int = 200) -> HTMLResponse:
    return page if isinstance(page, HTMLResponse) else HTMLResponse(to_xml(page), status_code=status_code)


def _message(e: APIError) -> str:
    """An API refusal as one plain sentence: field problems joined, anything else not shown raw."""
    if isinstance(e.detail, dict):
        return " ".join(str(v) for v in e.detail.values())
    return e.detail if isinstance(e.detail, str) else t("error.invalid_value")


def _status(e: APIError) -> int:
    return e.status if e.status >= 400 else 502


def _count(value) -> int:
    """A count carried in a form or query value; 0 when it is not a count."""
    value = str(value or "")
    return int(value) if value.isdigit() else 0


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

async def _gate(request: Request, mode: WizardMode):
    """A login with no company has no session: the API checks its email and password at
    every step instead."""
    return None if mode is START_COMPANY else await gate(request, mode)


def _sign_in(form) -> tuple[str, str]:
    return str(form.get("email", "")).strip(), str(form.get("password", ""))


def _start_company_fields(mode: WizardMode, values: dict) -> list:
    """The login's email and password again, at the step that restores for a login with no company."""
    if mode is not START_COMPANY:
        return []
    return [P(t("company_backup.confirm_with_password"), cls="form-hint"), *sign_in_fields(values.get("email", ""))]


def _set_upload_cookie(resp, token: str, mode: WizardMode, request: Request) -> None:
    resp.set_cookie(UPLOAD_COOKIE, token, max_age=UPLOAD_TTL_SECONDS, path=mode.base, httponly=True,
                    samesite="strict", secure=session_cookie_secure(request), domain=cookie_domain(request))


def _clear_upload_cookie(resp, mode: WizardMode, request: Request) -> None:
    resp.delete_cookie(UPLOAD_COOKIE, path=mode.base, domain=cookie_domain(request))


def _set_outcome_cookie(resp, mode: WizardMode, request: Request, done: dict) -> None:
    """What the API said the restore did, for the next page to state. Set by this server,
    never read from the address, and gone once shown."""
    value = ":".join(str(v) for v in (done.get("outcome") or "", _count(done.get("team_members")),
                                      len(done.get("connectors_to_reconnect") or []),
                                      done.get("role_permissions") or ""))
    resp.set_cookie(OUTCOME_COOKIE, value, max_age=OUTCOME_TTL_SECONDS, path=mode.base, httponly=True,
                    samesite="strict", secure=session_cookie_secure(request), domain=cookie_domain(request))


def _outcome(request: Request) -> dict:
    parts = (request.cookies.get(OUTCOME_COOKIE) or "").split(":")
    if len(parts) != 4 or parts[0] not in _OUTCOMES:
        return {}
    return {"outcome": parts[0], "team": _count(parts[1]), "reconnect": _count(parts[2]), "policy": parts[3]}


async def _upload_page(request: Request, mode: WizardMode, error: str | None = None, status_code: int = 200,
                       email: str = ""):
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
            *(sign_in_fields(email) if mode is START_COMPANY else []),
            setup_code_field() if await setup_code_required(mode) else "",
            Button(t("btn.continue"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/read", enctype="multipart/form-data", cls="auth-form",
        ),
        # First run offers restore once, here; a backup of a whole installation
        # (not one company) is recovered from its own page.
        P(A(t("setup.recover_installation"), href="/setup/import-backup", cls="auth-link"),
          cls="auth-alt-action") if mode is BOOTSTRAP else "",
        back_link(mode.back),
        status_code=status_code,
        title=_TITLE,
    )


def _upload_again(request: Request, mode: WizardMode, message: str):
    resp = upload_again_page(request, mode, message)
    _clear_upload_cookie(resp, mode, request)
    return resp


def _file_error(name: str, e: APIError) -> str:
    """A refusal of a file, naming the file the owner chose."""
    return t("company_backup.file_failed", name=name, reason=_message(e)) if name else _message(e)


_CREATE, _RETURN, _ADD_TEAM, _REACTIVATE = "create", "return_existing", "return_existing_and_add_team", "offer_reactivate"
_BUTTONS = {_CREATE: "company_backup.restore", _RETURN: "company_backup.open_existing",
            _ADD_TEAM: "company_backup.add_team_button", _REACTIVATE: "company_backup.reactivate_button"}
_MISSING, _INCOMPATIBLE, _ENABLE = "missing", "incompatible", "enable_required"


def _plan_lines(mode: WizardMode, preview: dict) -> list:
    """What restoring does, as the preview states it: a new company, or the company the
    backup was already restored as, the team members it gives access, and whose role
    permissions they work under."""
    action = preview.get("action") or _CREATE
    name = preview.get("destination_name") or preview.get("company_name") or ""
    team = _count(preview.get("team_members")) if mode is SETTINGS else 0
    blocked = _count(preview.get("team_blocked"))
    policy = ("company_backup.source_policy" if _policy(preview) == "source"
              else "company_backup.destination_policy")
    if action == _REACTIVATE:
        return [P(t("company_backup.deactivated_exists", name=name), cls="flash flash--warning")]
    if action == _CREATE:
        lines = [P(t("company_backup.separate_company"), cls="form-hint")] if mode is SETTINGS else []
        if team:
            lines += [P(t("company_backup.team_keeps_access", count=team), cls="flash flash--warning"),
                      P(t(policy, name=name), cls="form-hint")]
        return lines
    lines = [P(t("company_backup.already_restored", name=name), cls="form-hint")]
    if action == _ADD_TEAM and team:
        lines += [P(t("company_backup.team_to_add", name=name, count=team), cls="flash flash--warning"),
                  P(t(policy, name=name), cls="form-hint")]
    if blocked:
        lines.append(P(t("company_backup.team_blocked", name=name, count=blocked), cls="flash flash--warning"))
    return lines


def _policy(preview: dict) -> str:
    return (preview.get("scope") or {}).get("role_permissions") or ""


def _parts(names) -> str:
    return ", ".join(t(f"company_backup.part.{n}") for n in names or ())


def _scope_lines(preview: dict) -> list:
    """What the backup brings and what stays behind, and that role permissions follow
    this installation when they are not copied from the backup's company."""
    scope = preview.get("scope") or {}
    if not scope:
        return []
    lines = [P(t("company_backup.scope_included", items=_parts(scope.get("included"))), cls="form-hint"),
             P(t("company_backup.scope_excluded", items=_parts(scope.get("excluded"))), cls="form-hint")]
    if _policy(preview) == "destination" and (preview.get("action") or _CREATE) == _CREATE:
        lines.append(P(t("company_backup.roles_reset"), cls="form-hint"))
    return lines


def _module_name(module: dict) -> str:
    """The module's label; a missing one also shows its package name, which is how
    the user finds the file to import."""
    label, name = module.get("label") or module.get("name", ""), module.get("name", "")
    return f"{label} ({name})" if module.get("status") == _MISSING and name and name != label else label


async def _module_section(mode: WizardMode, preview: dict) -> list:
    """The modules the backup needs that are not ready, and the one step that readies
    them: import a missing module, or turn the installed ones on and restart."""
    modules = [m for m in preview.get("modules") or [] if m.get("status") != "ready"]
    statuses = {m.get("status") for m in modules}
    code = setup_code_field() if await setup_code_required(mode) else ""
    out = [P(t("company_backup.modules_needed"), cls="flash flash--warning"),
           Table(Tbody(*[Tr(Td(_module_name(m)), Td(t(f"company_backup.module_status.{m.get('status')}")))
                         for m in modules]),
                 cls="data-table")]
    if mode is START_COMPANY:
        out.append(P(t("company_backup.modules_need_company"), cls="form-hint"))
    elif _MISSING in statuses:
        out.append(Form(
            Div(Label(t("company_backup.module_file_label"), For="module", cls="form-label"),
                Input(type="file", id="module", name="module", required=True, accept=".zip", cls="form-input"),
                cls="form-group"),
            code,
            Button(t("company_backup.import_module"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/import-module", enctype="multipart/form-data", cls="auth-form"))
    elif _INCOMPATIBLE not in statuses:
        consent = [Label(Input(type="checkbox", name="consent", value=m["name"]),
                         " " + t("company_backup.module_consent", name=m.get("label") or m["name"]),
                         cls="form-check")
                   for m in modules if m.get("status") == _ENABLE and not m.get("first_party")]
        out.append(Form(
            *consent,
            P(t("company_backup.prepare_hint"), cls="form-hint"),
            code,
            Button(t("company_backup.prepare_modules"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/prepare", cls="auth-form"))
    return out


def _cancel(mode: WizardMode):
    """Choosing another file deletes this upload. A login with no company has no session to
    delete it with; its upload expires on its own."""
    if mode is START_COMPANY:
        return P(A(t("company_backup.choose_other"), href=mode.base, cls="auth-link"), cls="auth-alt-action")
    return Form(Button(t("company_backup.choose_other"), type="submit", cls="btn btn--secondary btn--full"),
                method="post", action=f"{mode.base}/discard", cls="auth-alt-action")


async def _preview_page(request: Request, mode: WizardMode, preview: dict, *, values: dict | None = None,
                        error: str | None = None, status_code: int = 200):
    values = values or {}
    action = preview.get("action") or _CREATE
    rows = [
        (t("company_backup.backup_date"), format_value(preview.get("created_at"), "date")),
        (t("company_backup.records"), str(preview.get("records") or "--")),
        (t("company_backup.attachments"), str(preview.get("attachments") or "--")),
    ]
    if preview.get("prepared_by"):
        rows.append((t("migration.prepared_by"), preview["prepared_by"]))
    if preview.get("modules_ready", True):
        target = "reactivate" if action == _REACTIVATE else "restore"
        name = values.get("company_name") or preview.get("destination_name") or preview.get("company_name") or ""
        step = [Form(
            Input(type="hidden", name="plan_fingerprint", value=str(preview.get("plan_fingerprint") or "")),
            Div(Label(t("company_backup.destination_name"), For="company_name", cls="form-label"),
                Input(type="text", id="company_name", name="company_name", value=name, required=True,
                      cls="form-input"),
                cls="form-group") if action == _CREATE else "",
            *(account_fields(values) if mode.bootstrap else []),
            *_start_company_fields(mode, values),
            setup_code_field() if await setup_code_required(mode) else "",
            Button(t(_BUTTONS.get(action, _BUTTONS[_CREATE])), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/{target}", cls="auth-form",
        )]
    else:
        step = await _module_section(mode, preview)
    return wizard_page(
        request,
        auth_header(preview.get("company_name", ""), t("company_backup.preview_subtitle")),
        flash(error) if error else "",
        Table(Tbody(*[Tr(Td(k), Td(v)) for k, v in rows]), cls="data-table"),
        *_plan_lines(mode, preview),
        *_scope_lines(preview),
        P(t("company_backup.nothing_written"), cls="form-hint"),
        *step,
        _cancel(mode),
        status_code=status_code,
        title=_TITLE,
    )


def _with_cookie(resp, mode: WizardMode, request: Request, upload_token: str):
    resp = _html(resp)
    _set_upload_cookie(resp, upload_token, mode, request)
    return resp


def _preview_of(summary: dict) -> dict:
    return {**summary, "prepared_by": (summary.get("provenance") or {}).get("prepared_by")}


async def _start(request: Request, mode: WizardMode):
    if (denied := await _gate(request, mode)) is not None:
        return denied
    return await _upload_page(request, mode)


def _mode_key(mode: WizardMode) -> str | None:
    return None if mode.bootstrap else mode.key


def _setup_code(form) -> str | None:
    return str(form.get("setup_code", "")).strip() or None


async def _read(request: Request, mode: WizardMode):
    if (denied := await _gate(request, mode)) is not None:
        return denied
    form = await request.form()
    email, password = _sign_in(form)
    upload = form.get("file")
    if not getattr(upload, "filename", ""):
        return await _upload_page(request, mode, t("migration.choose_file"), email=email)
    try:
        if mode is START_COMPANY:
            summary = await api.company_backup_start_read(email, password, upload.filename, upload.file)
        else:
            summary = await api.company_backup_read(api_token(request, mode), upload.filename, upload.file,
                                                    setup_code=_setup_code(form), mode=_mode_key(mode))
    except APIError as e:
        return await _upload_page(request, mode, _file_error(upload.filename, e), status_code=_status(e),
                                  email=email)
    return _with_cookie(await _preview_page(request, mode, _preview_of(summary), values={"email": email}), mode,
                        request, summary["upload_token"])


async def _again(request: Request, mode: WizardMode, upload_token: str, *, values: dict | None = None,
                 error: str | None = None, status_code: int = 200):
    """The staged upload's preview, checked again; when the upload is gone, the reason
    and the way to choose the file again. A login with no company has no session to
    check it with, so it chooses the file again."""
    if mode is START_COMPANY:
        return _upload_again(request, mode, error or t("company_backup.upload_expired"))
    try:
        preview = _preview_of(await api.company_backup_staged(api_token(request, mode), upload_token,
                                                              _mode_key(mode)))
    except APIError as e:
        return _upload_again(request, mode, error or _message(e))
    page = _html(await _preview_page(request, mode, preview, values=values, error=error, status_code=status_code))
    page.headers["X-Celerp-Staged"] = "1"
    return page


async def _staged(request: Request, mode: WizardMode):
    """The staged upload's preview, after its modules were prepared or Celerp restarted."""
    if (denied := await gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if not upload_token:
        return _upload_again(request, mode, t("company_backup.upload_expired"))
    try:
        preview = _preview_of(await api.company_backup_staged(api_token(request, mode), upload_token,
                                                              _mode_key(mode)))
    except APIError as e:
        if e.status >= 500:
            return await _restarting_page(request, mode, _message(e), status_code=_status(e))
        return _upload_again(request, mode, _message(e))
    page = _html(await _preview_page(request, mode, preview))
    page.headers["X-Celerp-Staged"] = "1"
    return page


async def _restarting_page(request: Request, mode: WizardMode, message: str | None = None, status_code: int = 200):
    """Celerp is restarting to load modules. The page asks for the staged backup until
    Celerp answers with it, then shows it."""
    staged = f"{mode.base}/staged"
    return wizard_page(
        request,
        auth_header(t(_TITLE)),
        P(message or t("company_backup.restarting"), cls="form-hint"),
        P(t("company_backup.restart_slow"), cls="flash flash--warning", id="restart-timeout", style="display:none;"),
        A(t("company_backup.check_again"), href=staged, cls="btn btn--secondary btn--full"),
        Script(f"""
        (function () {{
          var tries = 0, MAX = 40;
          setTimeout(function poll() {{
            if (tries++ >= MAX) {{ document.getElementById('restart-timeout').style.display = ''; return; }}
            fetch({staged!r}, {{cache: 'no-store'}}).then(function (r) {{
              if (r.ok && r.headers.get('X-Celerp-Staged')) {{ window.location = {staged!r}; }}
              else {{ setTimeout(poll, 1500); }}
            }}).catch(function () {{ setTimeout(poll, 1500); }});
          }}, 2500);
        }})();
        """),
        _cancel(mode),
        status_code=status_code,
        title=_TITLE,
    )


async def _restarting(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    return await _restarting_page(request, mode)


async def _prepare(request: Request, mode: WizardMode):
    """Turn on the modules the staged backup needs; when Celerp restarts to load them,
    wait for it and come back to the same backup."""
    if (denied := await gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if not upload_token:
        return _upload_again(request, mode, t("company_backup.upload_expired"))
    form = await request.form()
    try:
        done = await api.company_backup_prepare(api_token(request, mode), upload_token,
                                                [str(v) for v in form.getlist("consent")],
                                                setup_code=_setup_code(form))
    except APIError as e:
        return await _again(request, mode, upload_token, error=_message(e), status_code=_status(e))
    if done.get("restart") and not done.get("restarting"):
        return await _again(request, mode, upload_token, error=t("company_backup.restart_needed"))
    return RedirectResponse(f"{mode.base}/restarting" if done.get("restart") else f"{mode.base}/staged",
                            status_code=303)


async def _import_module(request: Request, mode: WizardMode):
    """Install a module file the staged backup needs, then show the backup again."""
    if (denied := await gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if not upload_token:
        return _upload_again(request, mode, t("company_backup.upload_expired"))
    form = await request.form()
    upload = form.get("module")
    if not getattr(upload, "filename", ""):
        return await _again(request, mode, upload_token, error=t("migration.choose_file"))
    try:
        preview = await api.company_backup_import_module(api_token(request, mode), upload_token, upload.filename,
                                                         upload.file, mode=_mode_key(mode),
                                                         setup_code=_setup_code(form))
    except APIError as e:
        return await _again(request, mode, upload_token, error=_file_error(upload.filename, e),
                            status_code=_status(e))
    return _html(await _preview_page(request, mode, _preview_of(preview)))


async def _discard(request: Request, mode: WizardMode):
    """Delete the staged upload and go back to choosing a file."""
    if (denied := await gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if upload_token:
        try:
            await api.company_backup_discard(api_token(request, mode), upload_token)
        except APIError:
            # Left for the expiry sweep; the owner's choice to start over still stands.
            pass
    resp = RedirectResponse(mode.base, status_code=303)
    _clear_upload_cookie(resp, mode, request)
    return resp


def _unconfirmed(e: APIError) -> bool:
    """The request may not have reached the API, or its answer was lost: the result is unknown."""
    return e.status in (502, 503, 504) or (isinstance(e.data, dict) and e.data.get("code") == api.NO_RESPONSE)


async def _resolving_page(request: Request, mode: WizardMode, e: APIError, target: str, form, values: dict):
    """The restore was sent but its result is not known. Nothing is claimed either way;
    sending it again finishes it or opens the company it already made."""
    lost = isinstance(e.data, dict) and e.data.get("code") == api.NO_RESPONSE
    return wizard_page(
        request,
        auth_header(t(_TITLE)),
        flash(t("company_backup.not_confirmed") if lost else _message(e)),
        P(t("company_backup.check_again_hint"), cls="form-hint"),
        Form(
            Input(type="hidden", name="plan_fingerprint", value=str(form.get("plan_fingerprint", ""))),
            Input(type="hidden", name="company_name", value=str(form.get("company_name", ""))),
            *(account_fields(values) if mode.bootstrap else []),
            *_start_company_fields(mode, values),
            setup_code_field() if await setup_code_required(mode) else "",
            Button(t("company_backup.check_again"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/{target}", cls="auth-form",
        ),
        status_code=_status(e),
        title=_TITLE,
    )


async def _password_again_page(request: Request, mode: WizardMode, e: APIError, target: str, form, values: dict):
    """The login's email or password was wrong: the upload is kept and asked for again."""
    return wizard_page(
        request,
        auth_header(t(_TITLE)),
        flash(_message(e)),
        Form(
            Input(type="hidden", name="plan_fingerprint", value=str(form.get("plan_fingerprint", ""))),
            Input(type="hidden", name="company_name", value=str(form.get("company_name", ""))),
            *_start_company_fields(mode, values),
            Button(t(_BUTTONS[_CREATE]), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/{target}", cls="auth-form",
        ),
        _cancel(mode),
        title=_TITLE,
    )


async def _refused(request: Request, mode: WizardMode, upload_token: str, e: APIError, target: str, form,
                   values: dict | None = None):
    """An unknown result is resolved, never assumed. Otherwise the staged backup is shown
    again, checked afresh, with the reason; a refused backup or a gone upload starts over."""
    if _unconfirmed(e):
        return await _resolving_page(request, mode, e, target, form, values or {})
    if mode is START_COMPANY and e.status == 401:
        return await _password_again_page(request, mode, e, target, form, values or {})
    stale = isinstance(e.data, dict) and e.data.get("code") == "stale_preview"
    return await _again(request, mode, upload_token, values=values, error=_message(e),
                        status_code=409 if stale else 200)


def _signed_in(request: Request, mode: WizardMode, done: dict):
    resp = RedirectResponse(f"{mode.base}/done", status_code=303)
    set_session_cookies(resp, done["access_token"], done["refresh_token"], request)
    _set_outcome_cookie(resp, mode, request, done)
    _clear_upload_cookie(resp, mode, request)
    return resp


async def _reactivate(request: Request, mode: WizardMode):
    """Reactivate the deactivated company the backup was already restored as, then open it."""
    if (denied := await gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if not upload_token:
        return _upload_again(request, mode, t("company_backup.upload_expired"))
    form = await request.form()
    try:
        done = await api.company_backup_reactivate(get_token(request), upload_token, mode.key,
                                                   str(form.get("plan_fingerprint", "")))
    except APIError as e:
        return await _refused(request, mode, upload_token, e, "reactivate", form)
    return _signed_in(request, mode, done)


async def _restore(request: Request, mode: WizardMode):
    if (denied := await _gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if not upload_token:
        return _upload_again(request, mode, t("company_backup.upload_expired"))
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in ("name", "email", "company_name")}
    values["password"] = str(form.get("password", ""))
    values["confirm_password"] = str(form.get("confirm_password", ""))
    setup_code = _setup_code(form)
    company_name = values["company_name"] or None
    try:
        if mode.bootstrap:
            if (problem := account_error(values)) is not None:
                return await _again(request, mode, upload_token, values=values, error=problem)
            if await setup_code_required(mode) and not setup_code:
                return await _again(request, mode, upload_token, values=values, error=t("auth.setup_code_required"))
            done = await api.company_backup_bootstrap_restore(upload_token, values["name"], values["email"],
                                                              values["password"], setup_code=setup_code,
                                                              company_name=company_name)
        elif mode is START_COMPANY:
            done = await api.company_backup_start_restore(values["email"], values["password"], upload_token,
                                                          str(form.get("plan_fingerprint", "")),
                                                          company_name=company_name)
        else:
            done = await api.company_backup_restore(get_token(request), upload_token, mode.key,
                                                    str(form.get("plan_fingerprint", "")), company_name=company_name)
    except APIError as e:
        return await _refused(request, mode, upload_token, e, "restore", form, values)
    return _signed_in(request, mode, done)


async def _done(request: Request, mode: WizardMode):
    """The company the restore opened, now the session's company, saying what the restore
    did as the API reported it."""
    token = get_token(request)
    if not token:
        return RedirectResponse("/login", status_code=302)
    try:
        company = await api.get_company(token)
    except APIError:
        company = {}
    name = company.get("name", "")
    done = _outcome(request)
    outcome = done.get("outcome")
    open_company = A(t("migration.open_company"), href="/", cls="btn btn--primary btn--full")
    if outcome == _REACTIVATED:
        page = wizard_page(
            request,
            auth_header(t("company_backup.reactivated_title"), name),
            P(t("company_backup.reactivated_notice", name=name), cls="flash flash--success"),
            P(t("company_backup.reconnect_connectors", count=done["reconnect"]), cls="flash flash--warning")
            if done["reconnect"] else "",
            open_company,
            title=_TITLE,
        )
    elif outcome in (_OPENED, _TEAM_ADDED):
        page = wizard_page(
            request,
            auth_header(t("company_backup.opened_title"), name),
            P(t("company_backup.opened_existing"), cls="flash flash--success"),
            P(t("company_backup.team_given_access", count=done["team"]), cls="flash flash--success")
            if outcome == _TEAM_ADDED and done["team"] else "",
            open_company,
            title=_TITLE,
        )
    else:
        restored = (company.get("settings") or {}).get("restored_backup") or {}
        created = outcome == _CREATED
        page = wizard_page(
            request,
            auth_header(t("company_backup.restored_title"), name),
            P(t("company_backup.restored_notice", date=format_value(restored["created_at"], "date")),
              cls="flash flash--success") if restored.get("created_at") else "",
            P(t("company_backup.team_given_access", count=done["team"]), cls="flash flash--success")
            if created and done["team"] else "",
            P(t("company_backup.integrations_disconnected"), cls="flash flash--warning") if created else "",
            P(t("company_backup.roles_reset_done"), " ",
              A(t("company_backup.review_roles"), href=USERS_AND_ROLES, cls="auth-link"), cls="form-hint")
            if created and done["policy"] == "destination" else "",
            open_company,
            title=_TITLE,
        )
    resp = _html(page)
    resp.delete_cookie(OUTCOME_COOKIE, path=mode.base, domain=cookie_domain(request))
    return resp


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def company_backup_routes(app) -> None:
    app.get(DOWNLOAD)(_download)
    for mode in (SETTINGS, NEW_COMPANY, BOOTSTRAP, START_COMPANY):
        for method, suffix, handler in (("get", "", _start), ("post", "/read", _read),
                                        ("post", "/restore", _restore), ("get", "/done", _done)):
            getattr(app, method)(f"{mode.base}{suffix}")(bind(handler, mode, "company_backup"))
    for mode in (SETTINGS, NEW_COMPANY, BOOTSTRAP):
        for method, suffix, handler in (("get", "/staged", _staged), ("post", "/prepare", _prepare),
                                        ("get", "/restarting", _restarting), ("post", "/import-module", _import_module),
                                        ("post", "/discard", _discard)):
            getattr(app, method)(f"{mode.base}{suffix}")(bind(handler, mode, "company_backup"))
    for mode in (SETTINGS, NEW_COMPANY):
        app.post(f"{mode.base}/reactivate")(bind(_reactivate, mode, "company_backup"))
