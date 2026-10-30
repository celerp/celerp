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

import json
import tempfile
import secrets
import uuid
import base64
from pathlib import Path
from urllib.parse import urlencode
from cryptography.exceptions import InvalidTag

from celerp.services.company_handoff_links import parse_handoff_link
from celerp.services.company_handoff_transfer import HandoffDownloadError, download_company_handoff

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

import ui.api_client as api
import ui.handoff_opening as opening
from ui.api_client import APIError
from ui.components.files import fmt_size
from ui.components.shell import flash, secret_input, sign_in_fields
from ui.components.table import fmt_money, format_value, row_menu
from ui.config import cookie_domain, get_company_id, get_token, session_cookie_secure, set_session_cookies
from ui.i18n import get_lang, t
from ui.routes.account import account_panel
from ui.routes.auth import START_COMPANY as START_COMPANY_PAGE
from ui.routes.auth import START_COMPANY_RESTORE, auth_header, finish_sign_in
from ui.routes.migrations import (
    COMPANY,
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
    wizard_table,
)
from ui.streaming import ClosingStreamingResponse

DOWNLOAD = "/company-backup/download"
SEND = "/settings/company-handoff"
HANDOFFS = "/settings/company-handoffs"
SAVED = "/settings/saved-copies"
BACKUP_TAB = "/settings/general?tab=backup"
_TITLE = "company_backup.restore_title"
_OWNER_ONLY = "company_backup.owner_only"
SETTINGS = WizardMode("settings", "/settings/restore-backup", BACKUP_TAB, _TITLE, _OWNER_ONLY)
NEW_COMPANY = WizardMode("new_company", "/setup/new-company/restore-backup", "/setup/new-company",
                         _TITLE, _OWNER_ONLY)
BOOTSTRAP = WizardMode("bootstrap", "/setup/restore-backup", "/setup", _TITLE, _OWNER_ONLY)
START_COMPANY = WizardMode("start_company", START_COMPANY_RESTORE, START_COMPANY_PAGE, _TITLE, _OWNER_ONLY)
# Sending, listing and saved copies refuse a non-owner under a heading of their own.
COPIES = WizardMode("settings", SETTINGS.base, BACKUP_TAB, "company_backup.handoffs_title", _OWNER_ONLY)
SAVED_COPIES = WizardMode("settings", SETTINGS.base, BACKUP_TAB, "company_backup.saved_title", _OWNER_ONLY)

USERS_AND_ROLES = "/settings/general?tab=users"

UPLOAD_COOKIE = "celerp_company_backup_upload"
UPLOAD_TTL_SECONDS = 24 * 3600
OPENING_COOKIE = "celerp_handoff_opening"
OUTCOME_COOKIE = "celerp_company_backup_outcome"
OUTCOME_TTL_SECONDS = 300
_CREATED, _OPENED, _TEAM_ADDED, _REACTIVATED = "created", "opened_existing", "opened_existing_team_added", "reactivated"
_REPLACED, _PUT_BACK = "replaced", "put_back"
_OUTCOMES = frozenset({_CREATED, _OPENED, _TEAM_ADDED, _REACTIVATED, _REPLACED, _PUT_BACK})


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
    return ClosingStreamingResponse(chunks, media_type="application/octet-stream",
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
                                      done.get("role_permissions") or "", done.get("kept_backup_id") or ""))
    resp.set_cookie(OUTCOME_COOKIE, value, max_age=OUTCOME_TTL_SECONDS, path=mode.base, httponly=True,
                    samesite="strict", secure=session_cookie_secure(request), domain=cookie_domain(request))


def _outcome(request: Request) -> dict:
    parts = (request.cookies.get(OUTCOME_COOKIE) or "").split(":")
    if len(parts) != 5 or parts[0] not in _OUTCOMES:
        return {}
    kept = parts[4] if len(parts[4]) == 32 and all(c in "0123456789abcdef" for c in parts[4]) else ""
    return {"outcome": parts[0], "team": _count(parts[1]), "reconnect": _count(parts[2]), "policy": parts[3],
            "kept": kept}


async def _upload_page(request: Request, mode: WizardMode, error: str | None = None, status_code: int = 200,
                       email: str = "", link: str = ""):
    return wizard_page(
        request,
        auth_header(t(_TITLE), t("company_backup.upload_subtitle")),
        flash(error) if error else "",
        Form(
            Div(
                Label(t("company_backup.file_label"), For="file", cls="form-label"),
                Input(type="file", id="file", name="file", accept=".celerp-company",
                      cls="form-input"),
                cls="form-group",
            ),
            Div(
                Label(t("company_backup.handoff_link_label"), For="handoff_link", cls="form-label"),
                secret_input("handoff_link", link, get_lang(request)),
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


def _separate_line(mode: WizardMode) -> list:
    return [P(t("company_backup.separate_company"), cls="form-hint")] if mode is SETTINGS else []


def _plan_lines(mode: WizardMode, preview: dict, replace: bool) -> list:
    """What restoring does, as the preview states it: a new company, or the company the
    backup was already restored as, the team members it gives access, and whose role
    permissions they work under. With a replace on offer, that a restore is a separate
    company is said under that choice instead."""
    action = preview.get("action") or _CREATE
    name = preview.get("destination_name") or preview.get("company_name") or ""
    team = _count(preview.get("team_members")) if mode is SETTINGS else 0
    blocked = _count(preview.get("team_blocked"))
    policy = ("company_backup.source_policy" if _policy(preview) == "source"
              else "company_backup.destination_policy")
    if action == _REACTIVATE:
        return [P(t("company_backup.deactivated_exists", name=name), cls="flash flash--warning")]
    if action == _CREATE:
        lines = [] if replace else _separate_line(mode)
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


def _parts(heading: str, names) -> list:
    return [Strong(t(heading)), Ul(*(Li(t(f"company_backup.part.{n}")) for n in names or ()), cls="form-hint")]


def _scope_lines(preview: dict) -> list:
    """What the backup brings and what stays behind, and that role permissions follow
    this installation when they are not copied from the backup's company."""
    scope = preview.get("scope") or {}
    if not scope:
        return []
    lines = [*_parts("company_backup.scope_included", scope.get("included")),
             *_parts("company_backup.scope_excluded", scope.get("excluded")),
             P(t("company_backup.scope_excluded_note"), cls="form-hint")]
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
        # A first run creates its owner account with the opening, and a password is never
        # kept across a restart: there the step turns the modules on and shows the copy again.
        opens = not mode.bootstrap
        out.append(Form(
            *consent,
            *(_open_fields(preview) if opens else []),
            P(t("company_backup.enable_open_hint" if opens else "company_backup.prepare_hint"), cls="form-hint"),
            code,
            Button(t("company_backup.enable_and_open" if opens else "company_backup.prepare_modules"),
                   type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/prepare", cls="auth-form"))
        out.append(Form(Button(t("company_backup.decline_modules"), type="submit", cls="btn btn--secondary btn--full"),
                        method="post", action=f"{mode.base}/decline", cls="auth-alt-action"))
    return out


def _open_fields(preview: dict, values: dict | None = None) -> list:
    """What opening the copy confirms: the preview as shown and, for a new company, its name."""
    name = (values or {}).get("company_name") or preview.get("destination_name") or preview.get("company_name") or ""
    return [Input(type="hidden", name="plan_fingerprint", value=str(preview.get("plan_fingerprint") or "")),
            Div(Label(t("company_backup.destination_name"), For="company_name", cls="form-label"),
                Input(type="text", id="company_name", name="company_name", value=name, required=True,
                      cls="form-input"),
                cls="form-group") if (preview.get("action") or _CREATE) == _CREATE else ""]


def _declines(preview: dict) -> bool:
    """The module step offers to turn modules on, so declining it is the way out."""
    statuses = {m.get("status") for m in preview.get("modules") or [] if m.get("status") != "ready"}
    return not preview.get("modules_ready", True) and not statuses & {_MISSING, _INCOMPATIBLE}


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
        step = [Form(
            *_open_fields(preview, values),
            *(account_fields(values) if mode.bootstrap else []),
            *_start_company_fields(mode, values),
            setup_code_field() if await setup_code_required(mode) else "",
            Button(t(_BUTTONS.get(action, _BUTTONS[_CREATE])), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/{target}", cls="auth-form",
        )]
    else:
        step = await _module_section(mode, preview)
    replace = preview.get("replace") if action == _CREATE and preview.get("modules_ready", True) else None
    return wizard_page(
        request,
        auth_header(preview.get("company_name", ""), t("company_backup.preview_subtitle")),
        flash(error) if error else "",
        wizard_table(Tbody(*[Tr(Td(k), Td(v)) for k, v in rows])),
        *_plan_lines(mode, preview, replace=bool(replace)),
        *_scope_lines(preview),
        P(t("company_backup.nothing_written"), cls="form-hint"),
        *(_replace_section(mode, preview, replace) if replace else []),
        *step,
        "" if mode is not START_COMPANY and _declines(preview) else _cancel(mode),
        status_code=status_code,
        title=_TITLE,
    )


def _typed_name(company: str, label: str, field_id: str = "confirm_company_name") -> FT:
    """The field the owner types the company's name into before a replace or put-back."""
    return Div(Label(t(label, company=company), For=field_id, cls="form-label"),
               Input(type="text", id=field_id, name="company_name", value="", required=True,
                     autocomplete="off", cls="form-input"), cls="form-group")


def _version_rows(replace: dict) -> FT:
    """The company here and the copy, side by side."""
    here, copy = replace["current"], replace["incoming"]

    def number(facts: dict, key: str) -> FT:
        return Td(str(facts.get(key, 0)), cls="cell--number")

    # On the same day only the time tells which is older.
    same_day = len({format_value(f.get("last_changed"), "date") for f in (here, copy)}) == 1

    def when(facts: dict) -> FT:
        v = facts.get("last_changed")
        shown = f"{v[:10]} {v[11:16]} UTC" if same_day and v else format_value(v, "date")
        return Td(shown if v else "--", cls="cell--number")
    return wizard_table(
        Thead(Tr(Th(""), Th(t("company_backup.replace_here_now"), cls="cell--number"),
                 Th(t("company_backup.replace_this_copy"), cls="cell--number"))),
        Tbody(*[Tr(Td(t(label)), number(here, key), number(copy, key))
                for key, label in (("documents", "company_backup.replace_documents"),
                                   ("journals", "company_backup.replace_journals"),
                                   ("items", "company_backup.replace_items"))],
              Tr(Td(t("company_backup.replace_last_changed")), when(here), when(copy))))


def _replace_section(mode: WizardMode, preview: dict, replace: dict) -> list:
    """The company this copy is of is already here: what replacing it would lose, and the
    replace itself for its owner, who types its name. Opening the copy as a separate
    company stays below."""
    company = replace["company_name"]
    older = replace.get("older")
    lines = [
        H2(t("company_backup.replace_title", company=company), cls="mt-md"),
        P(t("company_backup.replace_intro", company=company), cls="form-hint"),
        _version_rows(replace),
    ]
    if older:
        lines.append(P(t("company_backup.replace_older", company=company,
                         date=format_value(older["since"], "date")),
                       cls="flash flash--warning"))
    if changed := _count(replace.get("changed_after_copy")):
        key = "company_backup.replace_changed_after_one" if changed == 1 else "company_backup.replace_changed_after"
        lines.append(P(t(key, company=company, count=changed), cls="flash flash--warning"))
    if replace.get("can_replace"):
        lines.append(Form(
            Input(type="hidden", name="plan_fingerprint", value=str(preview.get("plan_fingerprint") or "")),
            _typed_name(company, "company_backup.replace_type_name"),
            P(t("company_backup.replace_saved_first", company=company), cls="form-hint"),
            Button(t("company_backup.replace_button", company=company), type="submit",
                   cls="btn btn--danger btn--full"),
            method="post", action=f"{mode.base}/replace", cls="auth-form"))
    else:
        lines.append(P(t("company_backup.err_replace_owner_only", company=company), cls="flash flash--warning"))
    lines.append(H2(t("company_backup.replace_or_separate"), cls="mt-md"))
    return lines + _separate_line(mode)


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
    link = str(form.get("handoff_link", "")).strip()
    has_file = bool(getattr(upload, "filename", ""))
    if has_file == bool(link):
        return await _upload_page(request, mode, t("company_backup.choose_file_or_link"), email=email)

    token, setup_code = api_token(request, mode), _setup_code(form)

    async def stage(filename, content, handoff=None):
        if mode is START_COMPANY:
            return await api.company_backup_start_read(email, password, filename, content,
                                                       **({"handoff": handoff} if handoff else {}))
        return await api.company_backup_read(token, filename, content, setup_code=setup_code,
                                             mode=_mode_key(mode), **({"handoff": handoff} if handoff else {}))

    if link:
        try:
            validated = parse_handoff_link(link)
        except ValueError:
            return await _upload_page(request, mode, t("company_backup.handoff_unavailable"), email=email,
                                      link=link)
        unavailable = t("company_backup.handoff_unavailable")

        async def work(job: opening.Opening) -> dict:
            # Nothing is imported until the existing preview and restore steps succeed.
            try:
                with tempfile.TemporaryDirectory(prefix="celerp-handoff-") as folder:
                    backup = Path(folder) / "incoming.celerp-company"
                    await download_company_handoff(link, backup, job.report)
                    job.report("checking")
                    with backup.open("rb") as content:
                        summary = await stage(backup.name, content,
                                              handoff=(str(validated.copy_id), validated.proof_hash))
            except (HandoffDownloadError, ValueError, InvalidTag):
                raise opening.OpeningFailed(unavailable) from None
            except APIError as e:
                raise opening.OpeningFailed(_file_error("", e)) from None
            return {"summary": summary, "email": email}

        resp = RedirectResponse(f"{mode.base}/opening", status_code=303)
        resp.set_cookie(OPENING_COOKIE, opening.start(mode.key, link, work, unavailable), max_age=opening.KEEP_SECONDS,
                        path=mode.base, httponly=True, samesite="strict", secure=session_cookie_secure(request),
                        domain=cookie_domain(request))
        return resp
    try:
        summary = await stage(upload.filename, upload.file)
    except APIError as e:
        return await _upload_page(request, mode, _file_error(upload.filename, e),
                                  status_code=_status(e), email=email)
    return _with_cookie(await _preview_page(request, mode, _preview_of(summary), values={"email": email}), mode,
                        request, summary["upload_token"])


def _clear_opening_cookie(resp, mode: WizardMode, request: Request) -> None:
    resp.delete_cookie(OPENING_COOKIE, path=mode.base, domain=cookie_domain(request))


_STAGES = {"requesting": "company_backup.opening_requesting", "downloading": "company_backup.opening_downloading",
           "decrypting": "company_backup.opening_decrypting", "checking": "company_backup.opening_checking"}


def _stage_text(job: opening.Opening) -> str:
    if job.stage == "downloading" and job.percent is None:
        return t("company_backup.opening_downloading_unsized")
    return t(_STAGES.get(job.stage, _STAGES["requesting"]), percent=job.percent)


def _opening_status(job: opening.Opening) -> dict:
    return {"state": job.state, "stage": job.stage, "percent": job.percent, "text": _stage_text(job),
            "message": job.message}


async def _opening_page(request: Request, mode: WizardMode):
    """What the background download is doing and how far it has got. The page asks every
    second and moves on to the preview, or to the reason it could not open, by itself."""
    if (denied := await _gate(request, mode)) is not None:
        return denied
    job = opening.find(request.cookies.get(OPENING_COOKIE), mode.key)
    if job is None:
        return _upload_again(request, mode, t("company_backup.upload_expired"))
    if job.state != opening.RUNNING:
        return RedirectResponse(f"{mode.base}/opened", status_code=303)
    status, opened = f"{mode.base}/opening/status", f"{mode.base}/opened"
    return wizard_page(
        request,
        auth_header(t(_TITLE), t("company_backup.opening_subtitle")),
        P(_stage_text(job), id="opening-stage", aria_live="polite"),
        Div(Div(style=f"width:{job.percent or 0}%", cls="import-progress-fill", id="opening-bar"),
            cls="import-progress"),
        P(t("company_backup.opening_nothing_imported"), cls="form-hint"),
        Noscript(A(t("company_backup.check_again"), href=f"{mode.base}/opening", cls="btn btn--secondary btn--full")),
        Form(Button(t("btn.cancel"), type="submit", cls="btn btn--secondary btn--full"),
             method="post", action=f"{mode.base}/opening/cancel"),
        Script(f"""
        (function () {{
          var stage = document.getElementById('opening-stage'), bar = document.getElementById('opening-bar');
          setTimeout(function poll() {{
            fetch({status!r}, {{cache: 'no-store'}}).then(function (r) {{
              if (r.status === 404) {{ window.location = {opened!r}; return null; }}
              return r.ok ? r.json() : null;
            }}).then(function (s) {{
              if (!s) {{ setTimeout(poll, 2000); return; }}
              if (s.state !== 'running') {{ window.location = {opened!r}; return; }}
              stage.textContent = s.text;
              bar.style.width = (s.percent || 0) + '%';
              setTimeout(poll, 1000);
            }}).catch(function () {{ setTimeout(poll, 2000); }});
          }}, 1000);
        }})();
        """),
        status_code=200,
        title=_TITLE,
    )


async def _opening_state(request: Request, mode: WizardMode):
    if (denied := await _gate(request, mode)) is not None:
        return denied
    job = opening.find(request.cookies.get(OPENING_COOKIE), mode.key)
    if job is None:
        return JSONResponse({"state": "gone"}, status_code=404)
    return JSONResponse(_opening_status(job))


async def _opening_cancel(request: Request, mode: WizardMode):
    """Stop the download; nothing was imported, so choosing the file or link again is all
    there is to undo."""
    if (denied := await _gate(request, mode)) is not None:
        return denied
    opening.forget(request.cookies.get(OPENING_COOKIE), mode.key)
    resp = RedirectResponse(mode.base, status_code=303)
    _clear_opening_cookie(resp, mode, request)
    return resp


async def _opened(request: Request, mode: WizardMode):
    """The finished opening: the copy's preview, or why it could not be opened with the
    way to try again."""
    if (denied := await _gate(request, mode)) is not None:
        return denied
    key = request.cookies.get(OPENING_COOKIE)
    job = opening.find(key, mode.key)
    if job is None:
        return _upload_again(request, mode, t("company_backup.upload_expired"))
    if job.state == opening.RUNNING:
        return RedirectResponse(f"{mode.base}/opening", status_code=303)
    opening.forget(key, mode.key)
    if job.state == opening.FAILED:
        resp = _html(await _upload_page(request, mode, job.message, link=job.link))
    else:
        result = job.summary
        resp = _with_cookie(await _preview_page(request, mode, _preview_of(result["summary"]),
                                                values={"email": result["email"]}),
                            mode, request, result["summary"]["upload_token"])
    _clear_opening_cookie(resp, mode, request)
    return resp


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


async def _staged_preview(request: Request, mode: WizardMode):
    """The staged upload's preview, or the page that says why there is none."""
    if (denied := await gate(request, mode)) is not None:
        return None, denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if not upload_token:
        return None, _upload_again(request, mode, t("company_backup.upload_expired"))
    try:
        return _preview_of(await api.company_backup_staged(api_token(request, mode), upload_token,
                                                           _mode_key(mode))), None
    except APIError as e:
        if e.status >= 500:
            return None, await _restarting_page(request, mode, _message(e), status_code=_status(e))
        return None, _upload_again(request, mode, _message(e))


def _opens_now(mode: WizardMode, preview: dict) -> bool:
    return preview.get("open_now") is not None and not mode.bootstrap


async def _staged_page(request: Request, mode: WizardMode, preview: dict):
    if _opens_now(mode, preview):
        page = _html(_opening_chosen_page(request, mode, preview))
    else:
        error = t("company_backup.open_held") if preview.get("open_held") else None
        page = _html(await _preview_page(request, mode, preview, error=error))
    page.headers["X-Celerp-Staged"] = "1"
    return page


async def _staged(request: Request, mode: WizardMode):
    """The staged upload's preview, after its modules were prepared or Celerp restarted.
    It only reads: a copy the owner chose Enable and open for is opened by the POST its page sends."""
    preview, page = await _staged_preview(request, mode)
    return page if page is not None else await _staged_page(request, mode, preview)


async def _open_staged(request: Request, mode: WizardMode):
    """Open the copy the owner chose Enable and open for; anything else shows its preview."""
    preview, page = await _staged_preview(request, mode)
    if page is not None:
        return page
    if not _opens_now(mode, preview):
        return await _staged_page(request, mode, preview)
    return await _open_now(request, mode, request.cookies[UPLOAD_COOKIE], preview)


def _opening_chosen_page(request: Request, mode: WizardMode, preview: dict):
    """The modules are on: the page opens the copy the owner chose Enable and open for,
    and says so; the button sends the same request when scripts are off."""
    return wizard_page(
        request,
        auth_header(t(_TITLE)),
        P(t("company_backup.opening_chosen", name=preview["open_now"].get("company_name") or ""), cls="form-hint"),
        Form(Button(t("company_backup.open_chosen"), type="submit", cls="btn btn--primary btn--full"),
             method="post", action=f"{mode.base}/staged", id="open-chosen", cls="auth-form"),
        Script("document.getElementById('open-chosen').submit();"),
        _cancel(mode),
        title=_TITLE,
    )


async def _open_now(request: Request, mode: WizardMode, upload_token: str, preview: dict):
    """Open the copy the owner chose Enable and open for, now that its modules run, as the
    preview they confirmed showed it."""
    fingerprint = str(preview.get("plan_fingerprint") or "")
    form = {"plan_fingerprint": fingerprint, "company_name": preview["open_now"].get("company_name") or ""}
    target = "reactivate" if preview.get("action") == _REACTIVATE else "restore"
    try:
        if target == "reactivate":
            done = await api.company_backup_reactivate(get_token(request), upload_token, mode.key, fingerprint)
        else:
            done = await api.company_backup_restore(get_token(request), upload_token, mode.key, fingerprint,
                                                    company_name=form["company_name"] or None)
    except APIError as e:
        return await _refused(request, mode, upload_token, e, target, form, {"company_name": form["company_name"]})
    return await _signed_in(request, mode, done)


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
    opens = request.query_params.get("open") == "1"
    return await _restarting_page(request, mode, t("company_backup.restarting_open") if opens else None)


async def _prepare(request: Request, mode: WizardMode):
    """Turn on the modules the staged backup needs; when Celerp restarts to load them,
    wait for it and come back to the same backup."""
    if (denied := await gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if not upload_token:
        return _upload_again(request, mode, t("company_backup.upload_expired"))
    form = await request.form()
    opens = form.get("open") == "1" and not mode.bootstrap
    open_with = {"plan_fingerprint": str(form.get("plan_fingerprint", "")),
                 "company_name": str(form.get("company_name", "")).strip()} if opens else None
    try:
        done = await api.company_backup_prepare(api_token(request, mode), upload_token,
                                                [str(v) for v in form.getlist("consent")],
                                                setup_code=_setup_code(form), mode=_mode_key(mode),
                                                open_with=open_with)
    except APIError as e:
        return await _again(request, mode, upload_token, values=open_with, error=_message(e), status_code=_status(e))
    if done.get("restart") and not done.get("restarting"):
        return await _again(request, mode, upload_token, error=t("company_backup.restart_needed"))
    restarting = f"{mode.base}/restarting" + ("?open=1" if opens else "")
    return RedirectResponse(restarting if done.get("restart") else f"{mode.base}/staged", status_code=303)


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


async def _decline(request: Request, mode: WizardMode):
    """Decline the modules the staged copy needs: the upload is deleted, nothing is opened,
    and the page names the modules the copy needs."""
    if (denied := await gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    labels = ""
    if upload_token:
        token = api_token(request, mode)
        try:
            preview = await api.company_backup_staged(token, upload_token, _mode_key(mode))
            labels = ", ".join(_module_name(m) for m in preview.get("modules") or [] if m.get("status") != "ready")
        except APIError:
            pass
        try:
            await api.company_backup_discard(token, upload_token)
        except APIError:
            # Left for the expiry sweep; nothing was opened either way.
            pass
    return _upload_again(request, mode, t("company_backup.declined", labels=labels) if labels
                         else t("company_backup.declined_unnamed"))


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
    if isinstance(e.data, dict) and e.data.get("code") == "replaced_unfinished":
        # The company was replaced: the session moves to the new one, where sending it again
        # finishes the follow-up.
        resp = await _resolving_page(request, mode, e, target, form, values or {})
        set_session_cookies(resp, e.data["access_token"], e.data["refresh_token"], request)
        return resp
    if mode is START_COMPANY and e.status == 401:
        return await _password_again_page(request, mode, e, target, form, values or {})
    stale = isinstance(e.data, dict) and e.data.get("code") == "stale_preview"
    return await _again(request, mode, upload_token, values=values, error=_message(e),
                        status_code=409 if stale else 200)


async def _signed_in(request: Request, mode: WizardMode, done: dict):
    resp = await finish_sign_in(request, done["access_token"], done["refresh_token"], f"{mode.base}/done", 303)
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
    return await _signed_in(request, mode, done)


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
    return await _signed_in(request, mode, done)


async def _replace(request: Request, mode: WizardMode):
    """Replace the company this copy is of, as the preview offered, once its name is typed."""
    if (denied := await gate(request, mode)) is not None:
        return denied
    upload_token = request.cookies.get(UPLOAD_COOKIE)
    if not upload_token:
        return _upload_again(request, mode, t("company_backup.upload_expired"))
    form = await request.form()
    try:
        done = await api.company_backup_replace(get_token(request), upload_token, mode.key,
                                                str(form.get("plan_fingerprint", "")),
                                                str(form.get("company_name", "")))
    except APIError as e:
        return await _refused(request, mode, upload_token, e, "replace", form)
    return await _signed_in(request, mode, done)


async def _key_balances(token: str, currency: str | None) -> FT | str:
    """The opened company's own trial balance, receivables and payables, as its reports
    compute them. A report the company cannot show (its module is off) is left out."""
    rows = []
    for label, read, figure in (
        ("company_backup.tb_total_debits", api.get_trial_balance, lambda body: body["total_debit"]),
        ("company_backup.owed_to_you", api.get_ar_aging, lambda body: sum(body["buckets"].values())),
        ("company_backup.you_owe", api.get_ap_aging, lambda body: sum(body["buckets"].values())),
    ):
        try:
            rows.append(Tr(Td(t(label)), Td(fmt_money(figure(await read(token)), currency), cls="cell--number")))
        except APIError:
            continue
    if not rows:
        return ""
    return wizard_table(Thead(Tr(Th(t("label.balance")), Th(t("label.amount"), cls="cell--number"))), Tbody(*rows))


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
    elif outcome in (_REPLACED, _PUT_BACK):
        restored = (company.get("settings") or {}).get("restored_backup") or {}
        date = format_value(restored["created_at"], "date") if restored.get("created_at") else "--"
        notice = "company_backup.replaced_notice" if outcome == _REPLACED else "company_backup.put_back_notice"
        page = wizard_page(
            request,
            auth_header(t("company_backup.replaced_title"), name),
            P(t(notice, company=name, date=date), cls="flash flash--success"),
            _undo_replace(name, done["kept"]) if done.get("kept") else "",
            await _key_balances(token, company.get("currency")),
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
        firm = (restored.get("provenance") or {}).get("prepared_by")
        page = wizard_page(
            request,
            auth_header(t("migration.success_title") if firm else t("company_backup.restored_title"), name),
            P(t("company_backup.prepared_for_you", firm=firm), cls="flash flash--success") if firm else "",
            P(t("company_backup.restored_notice", date=format_value(restored["created_at"], "date")),
              cls="flash flash--success") if restored.get("created_at") else "",
            P(t("company_backup.team_given_access", count=done["team"]), cls="flash flash--success")
            if created and done["team"] else "",
            P(t("company_backup.integrations_disconnected"), " ",
              A(t("doc.send_relay_error_link"), href="/settings/cloud", cls="auth-link"),
              cls="flash flash--warning") if created else "",
            P(t("company_backup.roles_reset_done"), " ",
              A(t("company_backup.review_roles"), href=USERS_AND_ROLES, cls="auth-link"), cls="form-hint")
            if created and done["policy"] == "destination" else "",
            await _key_balances(token, company.get("currency")),
            open_company,
            title=_TITLE,
        )
    resp = _html(page)
    resp.delete_cookie(OUTCOME_COOKIE, path=mode.base, domain=cookie_domain(request))
    return resp


def _undo_replace(company: str, kept_id: str) -> FT:
    """The version a replace put aside: download it, or put it back once the company's name
    is typed. Putting it back saves the version it replaces in turn."""
    path = f"{SAVED}/{kept_id}"
    return Div(
        P(t("company_backup.replaced_saved", company=company), " ",
          A(t("company_backup.saved_download"), href=f"{path}/download", cls="auth-link"), cls="form-hint"),
        Form(_typed_name(company, "company_backup.undo_type_name"),
             Button(t("company_backup.undo_replace"), type="submit", cls="btn btn--secondary btn--full"),
             method="post", action=f"{path}/put-back", cls="auth-form"),
    )


def _handoff_status(item: dict) -> str:
    if item["status"] in ("revoked", "expired"):
        return t("company_backup.handoff_status_" + item["status"])
    if item["company_opened"]:
        return t("company_backup.handoff_status_company_opened")
    if item["downloaded"]:
        return t("company_backup.handoff_status_downloaded")
    if item["opened"]:
        return t("company_backup.handoff_status_opened")
    return t("company_backup.handoff_status_" + (
        "sent" if item["status"] in ("sent", "imported") else "pending"))


def _copy_actions(item: dict) -> FT:
    """The copy's actions behind one row menu; every action swaps the table in place."""
    path = f"{HANDOFFS}/{item['id']}"
    swap = {"hx_target": "#copies-list", "hx_swap": "outerHTML"}
    actions = []
    if item.get("link"):
        actions.append(Button(t("company_backup.ready_copy_link"), type="button", cls="row-menu-item",
                              **{"data-copy": "", "data-copy-text": item["link"]}))
    if item.get("renewable"):
        actions.append(Button(t("company_backup.copies_new_link"), type="button", hx_post=f"{path}/new-link",
                              hx_vals=json.dumps({"operation_id": str(uuid.uuid4())}),
                              cls="row-menu-item", **swap))
    if item.get("can_restore"):
        actions.append(Button(t("company_backup.copies_undo"), type="button", hx_post=f"{path}/restore",
                              cls="row-menu-item", **swap))
    if item["status"] != "pending":
        resend = {k: item[k] for k in ("firm_name", "recipient") if item.get(k)}
        actions.append(A(t("company_backup.copies_resend"), cls="row-menu-item",
                         href=f"{SEND}?{urlencode(resend)}" if resend else SEND))
    if item["status"] in ("pending", "ready", "sent", "imported"):
        actions.append(Button(t("btn.revoke"), type="button", hx_post=f"{path}/revoke",
                              hx_confirm=t("company_backup.copies_revoke_confirm"),
                              cls="row-menu-item row-menu-item--danger", **swap))
    return row_menu(f"copy-{item['id']}", *actions) if actions else Td("--")


def _copies_table(items: list[dict], *, notice: str | None = None, warning: str | None = None) -> FT:
    """This company's copies sent, newest first; every action swaps this table in place."""
    rows = [Tr(
        Td(item.get("recipient") or "--"),
        Td(item.get("firm_name") or "--"),
        Td(_handoff_status(item),
           P(t("company_backup.copies_key_missing"), cls="form-hint")
           if item.get("key_missing") and not item.get("renewable") else None),
        Td(format_value(item["sent_at"], "date") if item.get("sent_at") else "--", cls="cell--date"),
        Td(format_value(item["expires_at"], "date") if item.get("expires_at") else "--", cls="cell--date"),
        _copy_actions(item),
    ) for item in sorted(items, key=lambda item: str(item.get("sent_at") or ""), reverse=True)]
    return Div(
        P(notice, cls="flash flash--success") if notice else "",
        P(warning, cls="flash flash--warning") if warning else "",
        wizard_table(Thead(Tr(Th(t("company_backup.copies_sent_to")), Th(t("company_backup.firm_label")),
                              Th(t("th.status")), Th(t("company_backup.copies_sent")),
                              Th(t("company_backup.copies_expires")), Th(t("th.actions")))),
                     Tbody(*rows))
        if rows else P(t("company_backup.handoffs_empty")),
        P(t("company_backup.copies_revoke_reason"), cls="form-hint") if rows else "",
        id="copies-list",
    )


async def _handoffs_page(request: Request):
    if (denied := await _gate(request, COPIES)) is not None:
        return denied
    try:
        rows = (await api.company_backup_handoffs(get_token(request)))["items"]
    except (APIError, KeyError, TypeError) as exc:
        reason = _message(exc) if isinstance(exc, APIError) and exc.status != 502 else None
        return wizard_page(request, auth_header(t("company_backup.handoffs_title")),
                           flash(reason or t("company_backup.handoffs_unavailable")), back_link(BACKUP_TAB),
                           title="company_backup.handoffs_title")
    return wizard_page(
        request, auth_header(t("company_backup.handoffs_title")),
        _copies_table(rows),
        A(t("company_backup.send_title"), href=SEND, cls="btn btn--primary"),
        back_link(BACKUP_TAB), title="company_backup.handoffs_title")


async def _copy_action(request: Request, act, done: str):
    """Revoke, Undo or New link: the table swaps in place with what happened. Without
    htmx the page loads again."""
    if (denied := await _gate(request, COPIES)) is not None:
        return denied
    notice = warning = None
    token = get_token(request)
    try:
        await act(token, str(uuid.UUID(request.path_params["copy_id"])))
        notice = t(done)
    except APIError as exc:
        warning = _message(exc)
    except ValueError:
        warning = t("company_backup.handoffs_unavailable")
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(HANDOFFS, status_code=303)
    try:
        rows = (await api.company_backup_handoffs(token))["items"]
    except (APIError, KeyError, TypeError):
        return _html(Div(flash(warning or t("company_backup.handoffs_unavailable")), id="copies-list"))
    return _html(_copies_table(rows, notice=notice, warning=warning))


async def _revoke_handoff(request: Request):
    return await _copy_action(request, api.company_backup_revoke_handoff, "company_backup.copies_revoked")


async def _restore_handoff(request: Request):
    return await _copy_action(request, api.company_backup_restore_handoff, "company_backup.copies_restored")


async def _new_handoff_link(request: Request):
    form = await request.form()
    operation = str(form.get("operation_id", ""))

    async def act(token, copy_id):
        if str(uuid.UUID(operation)) != operation:
            raise ValueError
        return await api.company_backup_new_handoff_link(token, copy_id, operation)
    return await _copy_action(request, act, "company_backup.copies_new_link_done")


# ---------------------------------------------------------------------------
# Saved copies: the version each replace put aside
# ---------------------------------------------------------------------------

def _kept_id(request: Request) -> str | None:
    kept = request.path_params["kept_id"]
    return kept if len(kept) == 32 and all(c in "0123456789abcdef" for c in kept) else None


def _saved_table(items: list[dict], company: str, *, notice: str | None = None,
                 warning: str | None = None) -> FT:
    """The copies saved of this company before a copy replaced it, newest first. Delete
    swaps this table in place."""
    def actions(item: dict) -> FT:
        path = f"{SAVED}/{item['id']}"
        return Div(
            Div(A(t("company_backup.saved_download"), href=f"{path}/download", cls="btn btn--secondary btn--sm"),
                Button(t("btn.delete"), type="button", hx_post=f"{path}/delete",
                       hx_confirm=t("company_backup.saved_delete_confirm"), hx_target="#saved-copies",
                       hx_swap="outerHTML", cls="btn btn--danger btn--sm"),
                cls="flex-row gap-sm"),
            Form(_typed_name(company, "company_backup.saved_type_name", f"put-back-{item['id']}"),
                 Button(t("company_backup.saved_put_back"), type="submit", cls="btn btn--primary btn--sm"),
                 method="post", action=f"{path}/put-back", cls="flex-row gap-sm"),
            cls="flex-col gap-sm")
    rows = [Tr(
        Td(format_value(item["kept_at"], "date") if item.get("kept_at") else "--", cls="cell--date"),
        Td(item.get("company_name") or "--"),
        Td(fmt_size(_count(item.get("size"))), cls="cell--number"),
        Td(actions(item)),
    ) for item in sorted(items, key=lambda item: str(item.get("kept_at") or ""), reverse=True)]
    return Div(
        P(notice, cls="flash flash--success") if notice else "",
        P(warning, cls="flash flash--warning") if warning else "",
        wizard_table(Thead(Tr(Th(t("company_backup.saved_at")), Th(t("company_backup.saved_company")),
                              Th(t("company_backup.saved_size"), cls="cell--number"), Th(t("th.actions")))),
                     Tbody(*rows))
        if rows else P(t("company_backup.saved_empty")),
        P(t("company_backup.saved_put_back_hint", company=company), cls="form-hint") if rows else "",
        P(t("company_backup.saved_delete_reason"), cls="form-hint") if rows else "",
        id="saved-copies",
    )


async def _company_name(token: str) -> str:
    try:
        return (await api.get_company(token)).get("name", "")
    except APIError:
        return ""


async def _saved_page(request: Request, *, warning: str | None = None, status_code: int = 200):
    if (denied := await _gate(request, SAVED_COPIES)) is not None:
        return denied
    token = get_token(request)
    try:
        items = (await api.company_backup_kept(token))["items"]
    except (APIError, KeyError, TypeError) as exc:
        return wizard_page(request, auth_header(t("company_backup.saved_title")),
                           flash(_message(exc) if isinstance(exc, APIError) else t("error.invalid_value")),
                           back_link(BACKUP_TAB), status_code=_status(exc) if isinstance(exc, APIError) else 502,
                           title="company_backup.saved_title")
    return wizard_page(
        request, auth_header(t("company_backup.saved_title")),
        P(t("company_backup.saved_intro"), cls="form-hint"),
        _saved_table(items, await _company_name(token), warning=warning),
        back_link(BACKUP_TAB), status_code=status_code, title="company_backup.saved_title")


async def _saved_download(request: Request):
    if (denied := await _gate(request, SAVED_COPIES)) is not None:
        return denied
    if (kept := _kept_id(request)) is None:
        return await _saved_page(request, warning=t("company_backup.err_kept_not_found"), status_code=404)
    try:
        chunks, headers = await api.company_backup_kept_download(get_token(request), kept)
    except APIError as e:
        return await _saved_page(request, warning=_message(e), status_code=_status(e))
    return ClosingStreamingResponse(chunks, media_type="application/octet-stream",
                                    headers={k.title(): v for k, v in headers.items() if k != "content-type"})


async def _saved_delete(request: Request):
    """Delete a saved copy, after the page asked. The table swaps in place; without htmx
    the page loads again."""
    if (denied := await _gate(request, SAVED_COPIES)) is not None:
        return denied
    token = get_token(request)
    notice = warning = None
    if (kept := _kept_id(request)) is None:
        warning = t("company_backup.err_kept_not_found")
    else:
        try:
            await api.company_backup_delete_kept(token, kept)
            notice = t("company_backup.saved_deleted")
        except APIError as exc:
            warning = _message(exc)
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(SAVED, status_code=303)
    try:
        items = (await api.company_backup_kept(token))["items"]
    except (APIError, KeyError, TypeError):
        return _html(Div(flash(warning or t("error.invalid_value")), id="saved-copies"))
    return _html(_saved_table(items, await _company_name(token), notice=notice, warning=warning))


async def _put_back(request: Request):
    """Put a saved copy back in place of this company, once its name is typed."""
    if (denied := await _gate(request, SAVED_COPIES)) is not None:
        return denied
    if (kept := _kept_id(request)) is None:
        return await _saved_page(request, warning=t("company_backup.err_kept_not_found"), status_code=404)
    form = await request.form()
    try:
        done = await api.company_backup_put_back(get_token(request), kept, str(form.get("company_name", "")))
    except APIError as e:
        return await _saved_page(request, warning=_message(e), status_code=_status(e))
    resp = RedirectResponse(f"{SETTINGS.base}/done", status_code=303)
    set_session_cookies(resp, done["access_token"], done["refresh_token"], request)
    _set_outcome_cookie(resp, SETTINGS, request, {**done, "outcome": _PUT_BACK})
    return resp


async def _send_page(request: Request, *, error: str | None = None, values: dict | None = None,
                     status_code: int = 200, account: bool = False):
    """The send form. *account* embeds the free-account sign-in in place of the
    error; once verified it submits this same form again (same operation)."""
    if (denied := await _gate(request, COPIES)) is not None:
        return denied
    if not values:
        # The firm name and reply address start as the last send's; Resend from the
        # copies list brings the firm and the client's email.
        try:
            last = await api.company_backup_sender(get_token(request))
        except APIError:
            last = {}
        values = {"firm_name": request.query_params.get("firm_name") or last.get("firm_name") or "",
                  "recipient": request.query_params.get("recipient", ""),
                  "reply_to": last.get("reply_to") or ""}
    notice = flash(error) if error else ""
    if account:
        try:
            google = bool((await api.account_methods(get_token(request))).get("google"))
        except APIError:
            google = False
        notice = account_panel(get_lang(request), google=google, next_action="copy-send")
    operation = values.get("operation_id") or str(uuid.uuid4())
    secret = values.get("key") or base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    return wizard_page(
        request,
        auth_header(t("company_backup.send_title")),
        notice,
        Form(
            Input(type="hidden", name="operation_id", value=operation),
            Input(type="hidden", name="key", value=secret),
            Div(Label(t("company_backup.firm_label"), For="firm_name", cls="form-label"),
                Input(type="text", id="firm_name", name="firm_name", value=values.get("firm_name", ""),
                      required=True, maxlength="160", cls="form-input"), cls="form-group"),
            Div(Label(t("company_backup.recipient_label"), For="recipient", cls="form-label"),
                Input(type="email", id="recipient", name="recipient", value=values.get("recipient", ""),
                      required=True, maxlength="320", cls="form-input"), cls="form-group"),
            Div(Label(t("company_backup.reply_label"), For="reply_to", cls="form-label"),
                Input(type="email", id="reply_to", name="reply_to", value=values.get("reply_to", ""),
                      required=True, maxlength="320", cls="form-input"), cls="form-group"),
            Div(Label(t("company_backup.note_label"), For="note", cls="form-label"),
                Textarea(values.get("note", ""), id="note", name="note", maxlength="1500",
                         cls="form-input"), cls="form-group"),
            P(t("company_backup.send_privacy"), cls="form-hint"),
            Button(t("company_backup.send_title"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=SEND, id="copy-send-form", cls="auth-form",
        ),
        Div(A(t("company_backup.ready_download"), href=DOWNLOAD, cls="auth-link"),
            A(t("company_backup.handoffs_title"), href=HANDOFFS, cls="auth-link"),
            cls="flex-col gap-sm"),
        back_link(BACKUP_TAB), status_code=status_code,
        title="company_backup.send_title",
    )


async def _send_handoff(request: Request):
    if (denied := await _gate(request, COPIES)) is not None:
        return denied
    form = await request.form()
    keys = ("operation_id", "key", "firm_name", "recipient", "reply_to", "note")
    values = {k: str(form.get(k, "")).strip() for k in keys}
    try:
        operation = uuid.UUID(values["operation_id"])
        if str(operation) != values["operation_id"] or len(values["key"]) != 43:
            raise ValueError
    except ValueError:
        return await _send_page(request, error=t("company_backup.send_invalid"))
    try:
        sent = await api.company_backup_send_handoff(get_token(request), values)
    except APIError as exc:
        # Retain the same operation and secret for ambiguous delivery retries.
        return await _send_page(request, error=_message(exc), values=values, status_code=_status(exc),
                                account=exc.status == 428)
    return await _ready_page(request, values, sent)


async def _ready_page(request: Request, values: dict, sent: dict):
    """The company copy ready page: the copy's actions, then what happens next."""
    try:
        rows = (await api.company_backup_handoffs(get_token(request)))["items"]
        expires = next(item["expires_at"] for item in rows if item["id"] == sent["id"])
        expiry = t("company_backup.ready_expires_on", date=format_value(expires, "date"))
    except (APIError, KeyError, TypeError, StopIteration):
        expiry = t("company_backup.ready_expires_rule")
    return wizard_page(
        request,
        auth_header(t("company_backup.ready_sent_to", email=values["recipient"])),
        Div(Button(t("company_backup.ready_copy_link"), type="button", cls="btn btn--primary btn--full",
                   **{"data-copy": "", "data-copy-text": sent["link"]}),
            A(t("company_backup.ready_download"), href=DOWNLOAD, cls="btn btn--secondary btn--full"),
            A(t("migration.move_another"), href=COMPANY.base, cls="btn btn--secondary btn--full"),
            cls="flex-col gap-sm"),
        H2(t("company_backup.ready_next"), cls="mt-md"),
        Ul(Li(t("company_backup.ready_receives", email=values["recipient"], firm=values["firm_name"])),
           Li(expiry),
           Li(A(t("company_backup.ready_watch"), href=HANDOFFS)),
           Li(A(t("company_backup.ready_new_link"), href=HANDOFFS))),
        back_link(BACKUP_TAB), title="company_backup.ready_title",
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def company_backup_routes(app) -> None:
    app.get(DOWNLOAD)(_download)
    app.get(SEND)(_send_page)
    app.post(SEND)(_send_handoff)
    app.get(HANDOFFS)(_handoffs_page)
    app.post(HANDOFFS + "/{copy_id}/revoke")(_revoke_handoff)
    app.post(HANDOFFS + "/{copy_id}/restore")(_restore_handoff)
    app.post(HANDOFFS + "/{copy_id}/new-link")(_new_handoff_link)
    app.get(SAVED)(_saved_page)
    app.get(SAVED + "/{kept_id}/download")(_saved_download)
    app.post(SAVED + "/{kept_id}/delete")(_saved_delete)
    app.post(SAVED + "/{kept_id}/put-back")(_put_back)
    for mode in (SETTINGS, NEW_COMPANY, BOOTSTRAP, START_COMPANY):
        for method, suffix, handler in (("get", "", _start), ("post", "/read", _read),
                                        ("get", "/opening", _opening_page), ("get", "/opening/status", _opening_state),
                                        ("post", "/opening/cancel", _opening_cancel), ("get", "/opened", _opened),
                                        ("post", "/restore", _restore), ("get", "/done", _done)):
            getattr(app, method)(f"{mode.base}{suffix}")(bind(handler, mode, "company_backup"))
    for mode in (SETTINGS, NEW_COMPANY, BOOTSTRAP):
        for method, suffix, handler in (("get", "/staged", _staged), ("post", "/staged", _open_staged),
                                        ("post", "/prepare", _prepare),
                                        ("get", "/restarting", _restarting), ("post", "/import-module", _import_module),
                                        ("post", "/discard", _discard), ("post", "/decline", _decline)):
            getattr(app, method)(f"{mode.base}{suffix}")(bind(handler, mode, "company_backup"))
    for mode in (SETTINGS, NEW_COMPANY):
        app.post(f"{mode.base}/reactivate")(bind(_reactivate, mode, "company_backup"))
        app.post(f"{mode.base}/replace")(bind(_replace, mode, "company_backup"))
