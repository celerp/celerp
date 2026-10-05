# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Company migration wizard: move a company's books from another system into Celerp.

The wizard runs in three modes that share every step renderer and differ only in
their base path, Back target and API authentication:

- bootstrap (`/setup/migrate`): no user exists yet; the first owner and company
  are created by the migration itself.
- company (`/setup/new-company/migrate`): a signed-in owner adds another company.
- start_company (`/setup/start-company/migrate`): a login whose last company was
  reset signs in with its email and password at the upload and at the start, and the
  migration creates its company.

Steps: choose source and upload, coverage and method, mapping (only when the scan
asks questions), review, then the run pages under `/migrations/{run_id}`: progress,
verify, complete, discard.

The scan token lives only in an HttpOnly cookie scoped to the mode's base path and
in request bodies to the API, never in a URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, StreamingResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import auth_shell, client_scripts, flash, page_title
from ui.components.activity import fmt_qty
from ui.components.table import EMPTY, display_enum, fmt_money, searchable_select
from ui.config import (
    clear_session_cookies,
    cookie_domain,
    get_role,
    get_token,
    session_cookie_secure,
    set_session_cookies,
)
from ui.i18n import get_lang, t
from ui.routes.auth import auth_header

SCAN_COOKIE = "celerp_migration_scan"
SCAN_TTL_SECONDS = 3600
SOURCE_REQUEST_URL = "https://celerp.com/migrate/request"
_SEARCHABLE_OVER = 10
_ACTIVE_STATUSES = ("preparing", "running", "cancel_requested", "reconciling")
_RESUMABLE_STATUSES = ("cancelled", "interrupted", "failed")
_PHASES = (
    "company_settings", "currencies_taxes_accounts", "contacts_locations", "inventory_masters",
    "operational_documents", "settlements", "inventory_opening_adjustments",
    "residual_journals_or_cutover_opening", "attachments", "reconciliation", "ready_to_finalize",
)
_BLOCKING_COVERAGE = ("unsupported_financial_blocker", "unclassified")
_COVERAGE_BADGE = {
    "mapped": "badge--active",
    "mapped_with_loss": "badge--warning",
    "ignored_non_business": "badge--inactive",
    "unsupported_nonfinancial": "badge--warning",
    "unsupported_financial_blocker": "badge--danger",
    "unclassified": "badge--danger",
}
_PHASE_BADGE = {"done": "badge--active", "running": "badge--open", "failed": "badge--danger"}
_RESULT_BADGE = {"pass": "badge--active", "rounding": "badge--warning", "fail": "badge--danger"}
_TOTAL_CHECKS = ("debits_equal_credits", "ar_control", "ap_control")
_UNIT_CHECKS = frozenset({"document_count", "document_status", "inventory_quantity"})


@dataclass(frozen=True)
class WizardMode:
    key: str
    base: str
    back: str
    title: str = "migration.title"
    owner_only: str = "migration.owner_only"

    @property
    def bootstrap(self) -> bool:
        return self.key == "bootstrap"

    @property
    def start_company(self) -> bool:
        return self.key == "start_company"

    @property
    def api_group(self) -> str:
        """The API routes the steps use without a session."""
        return "start-company" if self.start_company else "bootstrap"


BOOTSTRAP = WizardMode("bootstrap", "/setup/migrate", "/setup")
COMPANY = WizardMode("company", "/setup/new-company/migrate", "/setup/new-company")
START_COMPANY = WizardMode("start_company", "/setup/start-company/migrate", "/setup/start-company")


# ---------------------------------------------------------------------------
# Scan state
# ---------------------------------------------------------------------------
# The scan lives in the API's scan store and every step reads it back by token.
# The Prepared by name typed at upload rides in its own cookie until the first
# decisions save stores it with the scan.

PREPARED_BY_COOKIE = "celerp_migration_prepared_by"
# A login with no company: the email it signed the upload with, so the start asks only
# for its password again.
EMAIL_COOKIE = "celerp_migration_email"


def _set_scan_cookies(resp, token: str, prepared_by: str, mode: WizardMode, request: Request,
                      email: str = "") -> None:
    for name, value in ((SCAN_COOKIE, token), (PREPARED_BY_COOKIE, prepared_by), (EMAIL_COOKIE, email)):
        if name == EMAIL_COOKIE and not email:
            continue
        resp.set_cookie(name, value, max_age=SCAN_TTL_SECONDS, path=mode.base, httponly=True,
                        samesite="strict", secure=session_cookie_secure(request),
                        domain=cookie_domain(request))


def _clear_scan_cookie(resp, mode: WizardMode, request: Request) -> None:
    for name in (SCAN_COOKIE, PREPARED_BY_COOKIE, EMAIL_COOKIE):
        resp.delete_cookie(name, path=mode.base, domain=cookie_domain(request))


async def _read_scan(request: Request, mode: WizardMode, token: str) -> dict:
    """The scan entry for a token: the API's scan view plus the Prepared by name, or
    ``{"run_id"}`` when a run was already started from the scan. Raises APIError."""
    body = await api.migration_scan_read(api_token(request, mode), token, group=mode.api_group)
    if "run_id" in body:
        return {"run_id": body["run_id"]}
    scan = body["scan"]
    saved = (scan.get("decisions") or {}).get("prepared_by")
    return {"scan": scan, "prepared_by": saved or request.cookies.get(PREPARED_BY_COOKIE, "")}


# ---------------------------------------------------------------------------
# Shared page pieces
# ---------------------------------------------------------------------------

def wizard_page(request: Request, *content, status_code: int = 200, title: str = "migration.title"):
    page = auth_shell(*client_scripts(get_lang(request)), Div(*content, cls="auth-card migration-wizard"),
                      title=page_title(title))
    if status_code == 200:
        return page
    return HTMLResponse(to_xml(page), status_code=status_code)


def back_link(href: str) -> FT:
    label = t("auth.return_to_setup") if href == "/setup" else t("btn.back")
    return P(A(label, href=href, cls="auth-link"), cls="auth-alt-action")


def _steps(current: int) -> FT:
    labels = [t("migration.step_source"), t("migration.step_coverage"), t("migration.step_mapping"),
              t("migration.step_review"), t("migration.step_import"), t("migration.step_verify")]
    return Div(
        *[
            Div(
                Span(str(i + 1), cls=f"step-num {'step-num--active' if i + 1 == current else 'step-num--done' if i + 1 < current else ''}"),
                Span(label, cls=f"step-label {'step-label--active' if i + 1 == current else ''}"),
                cls="wizard-step",
            )
            for i, label in enumerate(labels)
        ],
        cls="wizard-steps",
    )


def _badge(text: str, cls: str) -> FT:
    return Span(text, cls=f"badge {cls}")


def field_error(errors: dict, field: str) -> FT | str:
    message = errors.get(field)
    return P(str(message), cls="form-hint text-danger") if message else ""


def choice_card(label: str, desc: str, *, href: str | None = None, post_to: str | None = None) -> FT:
    """One card of a setup chooser: a link, or a POST form for an action. The
    whole card is the click target; the label is the link or button itself."""
    if post_to:
        action = Button(label, type="submit", cls="quick-link-action")
        return Form(Strong(action), P(desc, cls="quick-link-desc"), method="post", action=post_to,
                    cls="quick-link-card")
    return Div(Strong(A(label, href=href, cls="quick-link-action")), P(desc, cls="quick-link-desc"),
               cls="quick-link-card")


def chooser(title: str, subtitle: str, cards: list, back: FT | str = "") -> FT:
    """The choice screen for adding another company (/setup/new-company)."""
    return Div(
        auth_header(title, subtitle),
        Div(*cards, cls="quick-links-grid"),
        back,
        cls="onboarding-card setup-chooser",
    )


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

async def gate(request: Request, mode: WizardMode):
    """Return a response when the request may not use this mode, else None."""
    if mode.start_company:
        # A signed-in user has a company: this way in is for a login without one.
        return RedirectResponse("/", status_code=302) if get_token(request) else None
    if mode.bootstrap:
        if get_token(request):
            return RedirectResponse("/", status_code=302)
        try:
            bootstrapped = await api.bootstrap_status()
        except APIError as e:
            from ui.routes.auth import _api_error_page
            return auth_shell(_api_error_page(str(e.detail)), title=page_title("page.api_unavailable"))
        if bootstrapped:
            return RedirectResponse("/login", status_code=302)
        return None
    if not get_token(request):
        return RedirectResponse("/login", status_code=302)
    from celerp.services.permissions import role_has_permission
    if not role_has_permission({}, get_role(request), "manage_company_lifecycle"):
        return wizard_page(request, auth_header(t(mode.title)), flash(t(mode.owner_only)), back_link(mode.back),
                           status_code=403, title=mode.title)
    return None


def api_token(request: Request, mode: WizardMode) -> str | None:
    """The session token for the API, or None where there is no session: in bootstrap mode
    no user exists yet, and a login with no company has none."""
    return None if mode.bootstrap or mode.start_company else get_token(request)


def upload_again_page(request: Request, mode: WizardMode, message: str) -> HTMLResponse:
    """An upload that is gone: the reason, and the way to upload the file again."""
    resp = wizard_page(request, auth_header(t(mode.title)), flash(message),
                       A(t("migration.upload_again"), href=mode.base, cls="btn btn--primary btn--full"),
                       back_link(mode.back), title=mode.title)
    return resp if isinstance(resp, HTMLResponse) else HTMLResponse(to_xml(resp))


def _expired(request: Request, mode: WizardMode, message: str | None = None):
    resp = upload_again_page(request, mode, message or t("migration.scan_expired"))
    _clear_scan_cookie(resp, mode, request)
    return resp


async def _current_scan(request: Request, mode: WizardMode):
    """(token, entry) for the request's scan, or (None, the response explaining why not)."""
    token = request.cookies.get(SCAN_COOKIE)
    if not token:
        return None, _expired(request, mode)
    try:
        entry = await _read_scan(request, mode, token)
    except APIError as e:
        if e.status == 410:
            return None, _expired(request, mode, str(e.detail))
        return None, wizard_page(request, auth_header(t("migration.title")), flash(str(e.detail)), back_link(mode.back),
                           status_code=e.status if e.status >= 400 else 502)
    if "run_id" in entry:
        # The start went through but its response was lost: continue with that run.
        resp = RedirectResponse(f"/migrations/{entry['run_id']}", status_code=303)
        _clear_scan_cookie(resp, mode, request)
        return None, resp
    return token, entry


# ---------------------------------------------------------------------------
# Step 1: choose source and upload
# ---------------------------------------------------------------------------

async def _sources() -> tuple[list[dict], str | None]:
    try:
        return await api.migration_sources(), None
    except APIError as e:
        return [], str(e.detail)


async def setup_code_required(mode: WizardMode) -> bool:
    return mode.bootstrap and await api.setup_code_required()


def _source_request_form() -> FT:
    return Form(
        Label(t("migration.source_request"), For="source-request", cls="form-label"),
        Div(
            Input(type="text", id="source-request", name="source", cls="form-input",
                  placeholder=t("migration.source_request_placeholder")),
            Button(t("migration.source_request_open"), type="submit", cls="btn btn--secondary"),
            cls="inline-form-row",
        ),
        method="get", action=SOURCE_REQUEST_URL, target="_blank", rel="noopener",
        cls="auth-form mt-md",
    )


def sign_in_fields(email: str) -> list:
    """The email and password of a login with no company, which every step that acts checks."""
    return [
        Div(Label(t("label.email"), For="email", cls="form-label"),
            Input(type="email", id="email", name="email", value=email, required=True, cls="form-input"),
            cls="form-group"),
        Div(Label(t("label.password"), For="password", cls="form-label"),
            Input(type="password", id="password", name="password", required=True, cls="form-input"),
            cls="form-group"),
    ]


def _credentials(form) -> tuple[str, str] | None:
    """The email and password a login with no company typed, or None when either is missing."""
    email, password = str(form.get("email", "")).strip(), str(form.get("password", ""))
    return (email, password) if email and password else None


def setup_code_field() -> FT:
    from celerp.config import config_path
    return Div(
        Label(t("label.setup_code"), For="setup_code", cls="form-label"),
        Input(type="text", id="setup_code", name="setup_code", required=True, cls="form-input"),
        P(t("msg.setup_code_hint", path=str(config_path().parent / "setup-code")), cls="form-hint"),
        cls="form-group",
    )


# Where each source's file comes from, in the words of that system's own menus.
_EXPORT_HELP = {"manager_io": "migration.export_help.manager_io"}


async def _source_page(request: Request, mode: WizardMode, *, selected: str = "", prepared_by: str = "",
                       error: str | None = None, back: str | None = None, email: str = ""):
    sources, sources_error = await _sources()
    code_required = await setup_code_required(mode)
    artifacts = [a for s in sources for a in s.get("artifacts", [])]
    extensions = sorted({ext for a in artifacts for ext in a.get("extensions", [])})
    # One file per source unless a source reads several together.
    several = any(len(s.get("artifacts", [])) > 1 for s in sources)
    accepted = ", ".join(f"{a['label']} ({', '.join(a.get('extensions', []))})" for a in artifacts)
    guidance = [P(t(_EXPORT_HELP[s["key"]]), cls="form-hint") for s in sources if s["key"] in _EXPORT_HELP]
    options = [("", t("migration.detect_source"))] + [(s["key"], s["display_name"]) for s in sources]
    radios = [
        Label(
            Input(type="radio", name="source", value=key, checked=(key == selected)),
            Span(label),
            cls="migration-source",
        )
        for key, label in options
    ]
    upload = Form(
        Fieldset(Legend(t("migration.source_label"), cls="form-label"), *radios, cls="form-group"),
        Div(
            Label(t("migration.files_label"), For="files", cls="form-label"),
            Input(type="file", id="files", name="files", multiple=several,
                  accept=",".join(extensions) if extensions else None, cls="form-input"),
            P(t("migration.accepted_files", files=accepted), cls="form-hint") if accepted else "",
            *guidance,
            cls="form-group",
        ),
        Div(
            Label(t("migration.prepared_by"), For="prepared_by", cls="form-label"),
            Input(type="text", id="prepared_by", name="prepared_by", value=prepared_by, cls="form-input"),
            P(t("migration.prepared_by_hint"), cls="form-hint"),
            cls="form-group",
        ),
        setup_code_field() if code_required else "",
        *(sign_in_fields(email) if mode.start_company else []),
        P(t("migration.retention"), cls="form-hint"),
        Button(t("migration.analyze"), type="submit", cls="btn btn--primary btn--full"),
        method="post", action=f"{mode.base}/scan", enctype="multipart/form-data", cls="auth-form",
    )
    sample = Form(
        setup_code_field() if code_required else "",
        *(sign_in_fields(email) if mode.start_company else []),
        Button(t("migration.try_sample"), type="submit", cls="btn btn--secondary btn--full"),
        method="post", action=f"{mode.base}/sample", cls="auth-form mt-sm",
    )
    return wizard_page(
        request,
        _steps(1),
        auth_header(t("migration.title"), t("migration.source_subtitle")),
        P(t("migration.start_company_hint"), cls="form-hint") if mode.start_company else "",
        flash(error) if error else "",
        flash(sources_error) if sources_error else "",
        upload,
        sample,
        _source_request_form(),
        back_link(back or mode.back),
    )


def _change_source_page(request: Request, mode: WizardMode, entry: dict, source: str):
    scan = entry["scan"]
    return wizard_page(
        request,
        _steps(1),
        auth_header(t("migration.change_source_title"),
                t("migration.change_source_body", file=scan.get("file_name", ""))),
        Form(
            Input(type="hidden", name="source", value=source),
            Input(type="hidden", name="confirm", value="1"),
            Input(type="hidden", name="prepared_by", value=entry.get("prepared_by", "")),
            Button(t("migration.change_source_confirm"), type="submit", cls="btn btn--primary btn--full"),
            method="get", action=mode.base, cls="auth-form",
        ),
        A(t("migration.keep_current_file"), href=f"{mode.base}/coverage", cls="btn btn--secondary btn--full mt-sm"),
        back_link(mode.back),
    )


async def _choose_source(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    q = request.query_params
    source = q.get("source", "")
    prepared_by = q.get("prepared_by", "")
    back = None
    clear = False
    token = request.cookies.get(SCAN_COOKIE)

    from_run = q.get("from_run", "") if api_token(request, mode) else ""
    if from_run:
        clear = True
        back = f"/migrations/{from_run}/complete"
        try:
            run = await api.get_migration_run(get_token(request), from_run)
            source = source or run.get("source_system") or ""
            prepared_by = prepared_by or run.get("prepared_by") or ""
        except APIError as e:
            resp = await _source_page(request, mode, selected=source, prepared_by=prepared_by,
                                      error=str(e.detail), back=back)
            return _with_cleared_scan(resp, request, mode)
    elif source and token:
        try:
            entry = await _read_scan(request, mode, token)
        except APIError:
            entry = None  # no readable scan: nothing to replace
        if entry is not None and "scan" in entry and source != entry["scan"].get("source_system"):
            if q.get("confirm") != "1":
                return _change_source_page(request, mode, entry, source)
            clear = True

    resp = await _source_page(request, mode, selected=source, prepared_by=prepared_by, back=back)
    return _with_cleared_scan(resp, request, mode) if clear else resp


def _with_cleared_scan(resp, request: Request, mode: WizardMode):
    if not isinstance(resp, HTMLResponse):
        resp = HTMLResponse(to_xml(resp))
    _clear_scan_cookie(resp, mode, request)
    return resp


async def _scan_and_continue(request: Request, mode: WizardMode, files: list, source: str | None,
                             prepared_by: str, setup_code: str | None, credentials: tuple[str, str] | None):
    email = credentials[0] if credentials else ""
    if mode.start_company and credentials is None:
        return await _source_page(request, mode, selected=source or "", prepared_by=prepared_by, email=email,
                                  error=t("auth.email_password_required"))
    try:
        result = await api.migration_scan(api_token(request, mode), files, source, setup_code=setup_code,
                                          group=mode.api_group, credentials=credentials)
    except APIError as e:
        return await _source_page(request, mode, selected=source or "", prepared_by=prepared_by, email=email,
                                  error=str(e.detail))
    resp = RedirectResponse(f"{mode.base}/coverage", status_code=303)
    _set_scan_cookies(resp, result["scan_token"], prepared_by, mode, request, email)
    return resp


async def _upload(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    form = await request.form()
    source = str(form.get("source", "")).strip() or None
    prepared_by = str(form.get("prepared_by", "")).strip()
    setup_code = str(form.get("setup_code", "")).strip() or None
    credentials = _credentials(form) if mode.start_company else None
    uploads = [f for f in form.getlist("files") if getattr(f, "filename", "")]
    if not uploads:
        return await _source_page(request, mode, selected=source or "", prepared_by=prepared_by,
                                  email=str(form.get("email", "")).strip(), error=t("migration.choose_file"))
    files = [(f.filename, f.file) for f in uploads]
    return await _scan_and_continue(request, mode, files, source, prepared_by, setup_code, credentials)


async def _sample(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    form = await request.form()
    setup_code = str(form.get("setup_code", "")).strip() or None
    credentials = _credentials(form) if mode.start_company else None
    try:
        from celerp.importers.sample import SAMPLE_ARTIFACT
    except ImportError:
        return await _source_page(request, mode, error=t("migration.sample_unavailable"))
    try:
        artifact = Path(SAMPLE_ARTIFACT)
        with artifact.open("rb") as fh:
            return await _scan_and_continue(request, mode, [(artifact.name, fh)], None, "",
                                            setup_code, credentials)
    except OSError:
        return await _source_page(request, mode, error=t("migration.sample_unavailable"))


# ---------------------------------------------------------------------------
# Step 2: coverage and method
# ---------------------------------------------------------------------------

def _coverage_label(coverage_class: str) -> str:
    return t(f"migration.coverage.{coverage_class}")


def _default_mode(scan: dict) -> str:
    if any(row.get("coverage_class") in _BLOCKING_COVERAGE for row in scan.get("coverage", [])):
        return "cutover"
    return "full_history"


def _decisions(scan: dict) -> dict:
    return scan.get("decisions") or {}


def _summary(scan: dict) -> FT:
    period = f"{scan.get('period_start') or '--'} - {scan.get('period_end') or '--'}"
    rows = [
        (t("migration.file"), f"{scan.get('file_name', '')} ({scan.get('sha256_short', '')})"),
        (t("label.company_name"), scan.get("company_name") or "--"),
        (t("migration.base_currency"), scan.get("base_currency") or "--"),
        (t("migration.period"), period),
        (t("migration.lock_date"), scan.get("lock_date") or "--"),
        (t("migration.currencies"), ", ".join(scan.get("currencies") or []) or "--"),
    ]
    return Table(Tbody(*[Tr(Td(k), Td(v)) for k, v in rows]), cls="data-table")


def _coverage_table(scan: dict) -> FT:
    rows = scan.get("coverage") or []
    if not rows:
        return P(t("migration.no_records"), cls="form-hint")
    return Table(
        Thead(Tr(Th(t("migration.record_type")), Th(t("migration.count"), cls="cell--number"),
                 Th(t("migration.outcome")), Th(t("migration.note")))),
        Tbody(*[
            Tr(
                Td(row.get("source_type", "")),
                Td(str(row.get("count", 0)), cls="cell--number"),
                Td(_badge(_coverage_label(row.get("coverage_class", "unclassified")),
                          _COVERAGE_BADGE.get(row.get("coverage_class"), "badge--danger"))),
                Td(row.get("note") or "--"),
            )
            for row in rows
        ]),
        cls="data-table",
    )


def _issues(scan: dict) -> FT:
    blockers = scan.get("blockers") or []
    warnings = scan.get("warnings") or []
    return Div(
        H3(t("migration.blocking_issues")),
        Ul(*[Li(t("migration.blocker_line", type=b.get("source_type", ""), count=b.get("count", 0),
                   reason=b.get("reason", ""))) for b in blockers])
        if blockers else P(t("migration.no_blockers"), cls="form-hint"),
        Div(H3(t("migration.warnings")), Ul(*[Li(w) for w in warnings])) if warnings else "",
    )


def _coverage_page(request: Request, mode: WizardMode, entry: dict, errors: dict | None = None,
                   error: str | None = None):
    scan = entry["scan"]
    decisions = _decisions(scan)
    errors = errors or {}
    chosen = decisions.get("mode") or _default_mode(scan)
    method = Fieldset(
        Legend(t("migration.method"), cls="form-label"),
        Label(Input(type="radio", name="mode", value="full_history", checked=chosen == "full_history"),
              Span(t("migration.full_history")), cls="migration-source"),
        Label(Input(type="radio", name="mode", value="cutover", checked=chosen == "cutover"),
              Span(t("migration.cutover")), cls="migration-source"),
        P(t("migration.cutover_explanation"), cls="form-hint"),
        field_error(errors, "mode"),
        cls="form-group",
    )
    cutover = Div(
        Label(t("migration.cutover_date"), For="cutover_date", cls="form-label"),
        Input(type="date", id="cutover_date", name="cutover_date", value=decisions.get("cutover_date") or "",
              cls="form-input"),
        field_error(errors, "cutover_date"),
        cls="form-group cutover-date",
    )
    return wizard_page(
        request,
        _steps(2),
        auth_header(t("migration.coverage_title"), t("migration.coverage_subtitle")),
        flash(error) if error else "",
        _summary(scan),
        _coverage_table(scan),
        _issues(scan),
        Form(
            Input(type="hidden", name="step", value="coverage"),
            method, cutover,
            field_error(errors, "mappings"),
            field_error(errors, "prepared_by"),
            Button(t("btn.continue"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/decisions", cls="auth-form",
        ),
        back_link(mode.base),
    )


async def _coverage(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    token, entry = await _current_scan(request, mode)
    if token is None:
        return entry
    return _coverage_page(request, mode, entry)


# ---------------------------------------------------------------------------
# Step 3: mapping
# ---------------------------------------------------------------------------

def _mapping_control(question: dict, value: str) -> FT:
    name = f"mapping__{question['key']}"
    options = list(question.get("options") or [])
    if len(options) > _SEARCHABLE_OVER:
        return searchable_select(name, options, value=value, aria_label=question.get("label", ""))
    return Select(*[Option(o, value=o, selected=o == value) for o in options], name=name,
                  aria_label=question.get("label", ""), cls="form-input")


def _mapping_page(request: Request, mode: WizardMode, entry: dict, errors: dict | None = None,
                  error: str | None = None):
    scan = entry["scan"]
    current = _decisions(scan).get("mappings") or {}
    errors = errors or {}
    questions = scan.get("questions") or []
    return wizard_page(
        request,
        _steps(3),
        auth_header(t("migration.mapping_title"), t("migration.mapping_subtitle")),
        flash(error) if error else "",
        Form(
            Input(type="hidden", name="step", value="mapping"),
            Table(
                Thead(Tr(Th(t("migration.source_item")), Th(t("migration.celerp_item")))),
                Tbody(*[
                    Tr(
                        Td(q.get("label", ""), Br(), Span(q.get("source_type", ""), cls="form-hint")),
                        Td(_mapping_control(q, current.get(q["key"], q.get("suggested") or "")),
                           field_error(errors, q["key"])),
                    )
                    for q in questions
                ]),
                cls="data-table",
            ),
            field_error(errors, "mappings"),
            Button(t("btn.continue"), type="submit", cls="btn btn--primary btn--full mt-md"),
            method="post", action=f"{mode.base}/decisions", cls="auth-form",
        ),
        back_link(f"{mode.base}/coverage"),
    )


async def _mapping(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    token, entry = await _current_scan(request, mode)
    if token is None:
        return entry
    if not entry["scan"].get("questions"):
        return RedirectResponse(f"{mode.base}/review", status_code=303)
    return _mapping_page(request, mode, entry)


async def _save_decisions(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    token, entry = await _current_scan(request, mode)
    if token is None:
        return entry
    form = await request.form()
    scan = entry["scan"]
    saved = _decisions(scan)
    step = "mapping" if form.get("step") == "mapping" else "coverage"
    mappings = saved.get("mappings") or {
        q["key"]: q.get("suggested") for q in scan.get("questions") or [] if q.get("suggested")
    }
    mappings = {**mappings, **{
        key[len("mapping__"):]: str(value) for key, value in form.multi_items() if key.startswith("mapping__")
    }}
    decisions = {
        "mode": str(form.get("mode") or saved.get("mode") or _default_mode(scan)),
        "cutover_date": str(form.get("cutover_date") or saved.get("cutover_date") or "") or None,
        "mappings": mappings,
        "prepared_by": entry.get("prepared_by") or None,
    }
    render = _mapping_page if step == "mapping" else _coverage_page
    try:
        new_scan = await api.migration_save_decisions(api_token(request, mode), token, decisions,
                                                      group=mode.api_group)
    except APIError as e:
        if e.status == 410:
            return _expired(request, mode, str(e.detail))
        if isinstance(e.detail, dict):
            return render(request, mode, entry, errors=e.detail)
        return render(request, mode, entry, error=str(e.detail))
    if step == "coverage" and new_scan.get("questions"):
        return RedirectResponse(f"{mode.base}/mapping", status_code=303)
    return RedirectResponse(f"{mode.base}/review", status_code=303)


# ---------------------------------------------------------------------------
# Step 4: review and start
# ---------------------------------------------------------------------------

def account_fields(values: dict) -> list:
    fields = [
        ("name", "label.your_name", "text"),
        ("email", "label.email", "email"),
        ("password", "label.password", "password"),
        ("confirm_password", "label.confirm_password", "password"),
    ]
    return [
        Div(
            Label(t(label), For=name, cls="form-label"),
            Input(type=kind, id=name, name=name, cls="form-input",
                  value=values.get(name, "") if kind != "password" else None),
            cls="form-group",
        )
        for name, label, kind in fields
    ]


async def _review_page(request: Request, mode: WizardMode, entry: dict, *, values: dict | None = None,
                       error: str | None = None, errors: dict | None = None):
    scan = entry["scan"]
    decisions = _decisions(scan)
    values = values or {}
    errors = errors or {}
    method = t("migration.cutover") if decisions.get("mode") == "cutover" else t("migration.full_history")
    if decisions.get("mode") == "cutover":
        method = f"{method} ({decisions.get('cutover_date') or '--'})"
    rows = [
        (t("migration.method"), method),
        (t("migration.prepared_by"), entry.get("prepared_by") or "--"),
    ]
    back = f"{mode.base}/mapping" if scan.get("questions") else f"{mode.base}/coverage"
    return wizard_page(
        request,
        _steps(4),
        auth_header(t("migration.review_title"), t("migration.review_subtitle")),
        flash(error) if error else "",
        _summary(scan),
        Table(Tbody(*[Tr(Td(k), Td(v)) for k, v in rows]), cls="data-table"),
        H3(t("migration.records")),
        _coverage_table(scan),
        _issues(scan),
        P(t("migration.expected_reconciliation"), cls="form-hint"),
        Form(
            Div(
                Label(t("label.company_name"), For="company_name", cls="form-label"),
                Input(type="text", id="company_name", name="company_name", cls="form-input",
                      value=values.get("company_name", scan.get("company_name") or "")),
                field_error(errors, "company_name"),
                cls="form-group",
            ),
            *(account_fields(values) if mode.bootstrap else []),
            *(sign_in_fields(values.get("email") or request.cookies.get(EMAIL_COOKIE, ""))
              if mode.start_company else []),
            setup_code_field() if await setup_code_required(mode) else "",
            Button(t("migration.create_and_migrate"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/start", cls="auth-form",
        ),
        back_link(back),
    )


async def _review(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    token, entry = await _current_scan(request, mode)
    if token is None:
        return entry
    if not entry["scan"].get("decisions"):
        return RedirectResponse(f"{mode.base}/coverage", status_code=303)
    return await _review_page(request, mode, entry)


def account_error(values: dict) -> str | None:
    """The first owner's account fields, checked before the API is called."""
    from celerp.services.auth import MIN_PASSWORD_LENGTH
    if not all(values.get(k) for k in ("name", "email", "password")):
        return t("settings.all_fields_required")
    if values["password"] != values.get("confirm_password"):
        return t("settings.passwords_do_not_match")
    if len(values["password"]) < MIN_PASSWORD_LENGTH:
        return t("settings.password_min_length")
    return None


async def _start(request: Request, mode: WizardMode):
    if (denied := await gate(request, mode)) is not None:
        return denied
    # The scan is read only to show the review page again: a repeated start whose first
    # response was lost finds its scan already claimed, and the API returns that run.
    token = request.cookies.get(SCAN_COOKIE)
    if not token:
        return _expired(request, mode)
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in ("company_name", "name", "email")}
    values["password"] = str(form.get("password", ""))
    values["confirm_password"] = str(form.get("confirm_password", ""))
    setup_code = str(form.get("setup_code", "")).strip() or None

    async def review(**feedback):
        scan_token, entry = await _current_scan(request, mode)
        if scan_token is None:
            return entry
        return await _review_page(request, mode, entry, values=values, **feedback)

    if not values["company_name"]:
        return await review(error=t("settings.all_fields_required"))
    try:
        if mode.bootstrap:
            if (problem := account_error(values)) is not None:
                return await review(error=problem)
            if await setup_code_required(mode) and not setup_code:
                return await review(error=t("auth.setup_code_required"))
            started = await api.migration_bootstrap_start(
                token, values["company_name"], values["name"], values["email"], values["password"],
                setup_code=setup_code)
        elif mode.start_company:
            if not (values["email"] and values["password"]):
                return await review(error=t("auth.email_password_required"))
            started = await api.migration_start_company_start(values["email"], values["password"], token,
                                                               values["company_name"])
        else:
            started = await api.migration_start_from_scan(get_token(request), token, values["company_name"])
    except APIError as e:
        if e.status == 410:
            return _expired(request, mode, str(e.detail))
        if isinstance(e.detail, dict):
            return await review(errors=e.detail)
        return await review(error=str(e.detail))
    resp = RedirectResponse(f"/migrations/{started['run_id']}", status_code=303)
    if api_token(request, mode) is None:
        # The first owner, or a login with no company, has no working session yet. A
        # company-mode start keeps the current session: the new company is opened only
        # after the migration finishes.
        set_session_cookies(resp, started["access_token"], started["refresh_token"], request)
    _clear_scan_cookie(resp, mode, request)
    return resp


# ---------------------------------------------------------------------------
# Run pages
# ---------------------------------------------------------------------------

def _run_error_page(request: Request, message: str):
    return wizard_page(request, auth_header(t("migration.title")), flash(message), back_link("/"))


def _phase_label(phase: dict) -> str:
    key = phase.get("phase", "")
    return t(f"migration.phase.{key}") if key in _PHASES else phase.get("label", key)


def _created_cell(phase: dict) -> str:
    created = phase.get("created", 0) or 0
    if phase.get("phase") == "attachments":
        return t("migration.n_attachments", n=created)
    return str(created)


def _run_actions(run: dict) -> FT:
    run_id = run["id"]
    status = run.get("status")
    buttons = []
    if status == "running":  # a run that is reconciling cannot be cancelled
        buttons.append(Form(Button(t("btn.cancel"), type="submit", cls="btn btn--secondary"),
                            method="post", action=f"/migrations/{run_id}/cancel"))
    if status == "interrupted":
        buttons.append(Form(Button(t("migration.interrupted_resume"), type="submit", cls="btn btn--primary"),
                            method="post", action=f"/migrations/{run_id}/start"))
    elif status in _RESUMABLE_STATUSES:
        buttons.append(Form(Button(t("migration.resume"), type="submit", cls="btn btn--primary"),
                            method="post", action=f"/migrations/{run_id}/start"))
    elif status == "ready" and not run.get("preparing"):
        buttons.append(Form(Button(t("migration.start"), type="submit", cls="btn btn--primary"),
                            method="post", action=f"/migrations/{run_id}/start"))
    if status == "ready_to_finalize":
        buttons.append(A(t("migration.check_results"), href=f"/migrations/{run_id}/verify", cls="btn btn--primary"))
    if status == "completed":
        buttons.append(A(t("btn.continue"), href=f"/migrations/{run_id}/complete", cls="btn btn--primary"))
    else:
        buttons.append(A(t("migration.discard"), href=f"/migrations/{run_id}/discard", cls="btn btn--secondary"))
    return Div(*buttons, cls="form-actions")


def _error_block(error: dict | None) -> FT | str:
    """A failed run's message, and the records it names when there are any."""
    if not error:
        return ""
    missing = error.get("missing") or []
    records = ", ".join(str(m) for m in missing) if isinstance(missing, list) else str(missing)
    return Div(P(error["message"]),
               P(t("migration.error_records", records=records), cls="form-hint") if records else "",
               cls="flash flash--error")


def _progress_fragment(run: dict, error: str | None = None) -> FT:
    status = run.get("status", "")
    phases = run.get("phases") or []
    polling = {"hx_get": f"/migrations/{run['id']}", "hx_trigger": "every 2s", "hx_swap": "outerHTML"} \
        if status in _ACTIVE_STATUSES or run.get("preparing") else {}
    return Div(
        flash(error) if error else "",
        P(_badge(t(f"migration.status.{status}"), _PHASE_BADGE.get(
            {"completed": "done", "failed": "failed", "running": "running"}.get(status, ""), "badge--inactive"))),
        Table(
            Thead(Tr(Th(t("migration.phase")), Th(t("label.status")),
                     Th(t("migration.created"), cls="cell--number"),
                     Th(t("migration.skipped"), cls="cell--number"),
                     Th(t("migration.errors"), cls="cell--number"))),
            Tbody(*[
                Tr(
                    Td(_phase_label(p)),
                    Td(_badge(t(f"migration.phase_status.{p.get('status', 'pending')}"),
                              _PHASE_BADGE.get(p.get("status"), "badge--inactive"))),
                    Td(_created_cell(p), cls="cell--number"),
                    Td(str(p.get("skipped", 0) or 0), cls="cell--number"),
                    Td(str(p.get("errors", 0) or 0), cls="cell--number"),
                )
                for p in phases
            ]),
            cls="data-table",
        ),
        P(t("migration.preparing_notice"), cls="form-hint") if run.get("preparing") else "",
        _error_block(run.get("error")),
        _run_actions(run),
        id="migration-progress",
        **polling,
    )


async def _load_run(request: Request, run_id: str):
    try:
        return await api.get_migration_run(get_token(request), run_id), None
    except APIError as e:
        return None, _run_error_page(request, str(e.detail))


def _progress_page(request: Request, run: dict, error: str | None = None):
    return wizard_page(
        request,
        _steps(5),
        auth_header(t("migration.progress_title", company=run.get("company_name", "")),
                t("migration.progress_subtitle") if run.get("status") in _ACTIVE_STATUSES or run.get("preparing")
                else ""),
        _progress_fragment(run, error),
        P(t("migration.retention"), cls="form-hint"),
    )


def migrations_routes(app) -> None:
    """Register the wizard for every mode and the run pages."""

    for mode in (BOOTSTRAP, COMPANY, START_COMPANY):
        _register_wizard(app, mode)

    @app.get("/migrations/{run_id}")
    async def migration_progress(request: Request, run_id: str):
        run, failure = await _load_run(request, run_id)
        if failure is not None:
            return failure
        if request.headers.get("HX-Request"):
            return _progress_fragment(run)
        return _progress_page(request, run)

    async def _action(request: Request, run_id: str, action: str):
        token = get_token(request)
        try:
            return await api.migration_run_action(token, run_id, action), None
        except APIError as e:
            return None, str(e.detail)

    def _run_step(action: str):
        async def route(request: Request, run_id: str):
            _, error = await _action(request, run_id, action)
            if error is None:
                return RedirectResponse(f"/migrations/{run_id}", status_code=303)
            run, failure = await _load_run(request, run_id)
            return failure if failure is not None else _progress_page(request, run, error)
        route.__name__ = f"migration_{action}"
        return route

    for action in ("start", "cancel"):
        app.post(f"/migrations/{{run_id}}/{action}")(_run_step(action))

    @app.post("/migrations/{run_id}/finalize")
    async def migration_finalize(request: Request, run_id: str):
        _, error = await _action(request, run_id, "finalize")
        if error is None:
            return RedirectResponse(f"/migrations/{run_id}/complete", status_code=303)
        return await _verify_page(request, run_id, error)

    @app.get("/migrations/{run_id}/verify")
    async def migration_verify(request: Request, run_id: str):
        return await _verify_page(request, run_id)

    @app.get("/migrations/{run_id}/pack")
    async def migration_pack(request: Request, run_id: str):
        try:
            chunks, headers = await api.migration_pack(get_token(request), run_id)
        except APIError as e:
            return await _verify_page(request, run_id, str(e.detail))
        return StreamingResponse(chunks, media_type=headers.get("content-type", "text/csv"),
                                 headers={k.title(): v for k, v in headers.items() if k != "content-type"})

    @app.get("/migrations/{run_id}/complete")
    async def migration_complete(request: Request, run_id: str):
        run, failure = await _load_run(request, run_id)
        if failure is not None:
            return failure
        return await _complete_page(request, run)

    @app.get("/migrations/{run_id}/discard")
    async def migration_discard_confirm(request: Request, run_id: str):
        run, failure = await _load_run(request, run_id)
        if failure is not None:
            return failure
        return _discard_page(request, run)

    @app.post("/migrations/{run_id}/discard")
    async def migration_discard(request: Request, run_id: str):
        result, error = await _action(request, run_id, "discard")
        if error is not None:
            run, failure = await _load_run(request, run_id)
            return failure if failure is not None else _discard_page(request, run, error)
        target = result.get("redirect") or "/"
        resp = RedirectResponse(target, status_code=303)
        if target in ("/setup", START_COMPANY.back):
            # The login has no company left, so its session has nothing to open.
            clear_session_cookies(resp, request)
        return resp


def _register_wizard(app, mode: WizardMode) -> None:
    steps = (
        ("get", "", _choose_source), ("post", "/scan", _upload), ("post", "/sample", _sample),
        ("get", "/coverage", _coverage), ("get", "/mapping", _mapping),
        ("post", "/decisions", _save_decisions), ("get", "/review", _review), ("post", "/start", _start),
    )
    for method, suffix, handler in steps:
        getattr(app, method)(f"{mode.base}{suffix}")(bind(handler, mode, "migrate"))


def bind(handler, mode: WizardMode, prefix: str):
    """A route calling ``handler`` in ``mode``, named ``<prefix>_<mode><handler>``."""
    async def route(request: Request):
        return await handler(request, mode)
    route.__name__ = f"{prefix}_{mode.key}{handler.__name__}"
    return route


# ---------------------------------------------------------------------------
# Verify, complete, discard
# ---------------------------------------------------------------------------

def _check_subject(row: dict) -> str:
    """What a check is about, by the source's own name or a translated term. A record key
    without a name is never shown: it is an identifier, kept for the technical pack."""
    check, key = row.get("check", ""), row.get("key") or ""
    if row.get("label"):
        return row["label"]
    if check == "document_status":
        doc_type, _, status = key.partition(":")
        return f"{display_enum(doc_type, 'doc_type')}, {display_enum(status, 'doc_status')}"
    if check == "settlement_allocation":
        return display_enum(key, "settlement_kind")
    if check in ("document_count", "document_total") and key:
        doc_type = display_enum(key, "doc_type")
        return f"{doc_type} ({row['currency']})" if check == "document_count" and row.get("currency") else doc_type
    return ""


def _check_label(row: dict) -> str:
    label = t(f"migration.check.{row.get('check', '')}")
    subject = _check_subject(row)
    return f"{label}: {subject}" if subject else label


def _figure(row: dict, field: str) -> str:
    """One figure of a check: money at its currency's precision, counts and quantities as
    plain numbers, and a credit-side balance shown as the amount it is."""
    try:
        value = Decimal(str(row.get(field)))
    except (InvalidOperation, ValueError):
        return EMPTY
    if not value.is_finite():
        return EMPTY
    if row.get("credit_normal"):
        value = -value + 0
    if row.get("check") in _UNIT_CHECKS:
        return fmt_qty(value)
    return fmt_money(value, row.get("currency"))


async def _verify_page(request: Request, run_id: str, error: str | None = None):
    token = get_token(request)
    try:
        recon = await api.migration_reconciliation(token, run_id)
        run = await api.get_migration_run(token, run_id)
    except APIError as e:
        if e.status != 409:
            return _run_error_page(request, str(e.detail))
        return wizard_page(request, _steps(6), auth_header(t("migration.verify_title")), flash(error) if error else "",
                     P(str(e.detail), cls="form-hint"), back_link(f"/migrations/{run_id}"))
    rows = recon.get("rows") or []
    lock_date = run.get("lock_date")
    return wizard_page(
        request,
        _steps(6),
        auth_header(t("migration.verify_title"), t("migration.verify_subtitle")),
        flash(error) if error else "",
        Table(
            Thead(Tr(Th(t("migration.col_check")), Th(t("migration.col_source"), cls="cell--number"),
                     Th(t("migration.col_celerp"), cls="cell--number"),
                     Th(t("migration.col_difference"), cls="cell--number"),
                     Th(t("migration.col_result")))),
            Tbody(*[
                Tr(
                    Td(_check_label(row)),
                    Td(_figure(row, "source"), cls="cell--number"),
                    Td(_figure(row, "celerp"), cls="cell--number"),
                    Td(_figure(row, "difference"), cls="cell--number"),
                    Td(_badge(t(f"migration.result.{row.get('result', 'n-a').replace('-', '_')}"),
                              _RESULT_BADGE.get(row.get("result"), "badge--inactive"))),
                )
                for row in rows
            ]),
            cls="data-table",
        ) if rows else P(t("migration.no_checks"), cls="form-hint"),
        P(t("migration.lock_date_notice", date=lock_date), cls="form-hint") if lock_date else "",
        Form(Button(t("migration.finish"), type="submit", cls="btn btn--primary btn--full"),
             method="post", action=f"/migrations/{run_id}/finalize", cls="auth-form mt-md"),
        P(A(t("migration.download_pack"), href=f"/migrations/{run_id}/pack", cls="auth-link")),
        P(A(t("migration.discard"), href=f"/migrations/{run_id}/discard", cls="auth-link")),
        back_link(f"/migrations/{run_id}"),
    )


async def _complete_page(request: Request, run: dict):
    run_id = run["id"]
    pack = P(A(t("migration.download_pack"), href=f"/migrations/{run_id}/pack", cls="auth-link"))
    open_href = f"/switch-company/{run['company_id']}"
    open_company = A(t("migration.open_company"), href=open_href, cls="btn btn--primary btn--full")
    if run.get("is_sample"):
        return wizard_page(
            request,
            auth_header(t("migration.sample_done_title"), t("migration.sample_done_body")),
            pack,
            A(t("migration.move_first_company"), href=COMPANY.base, cls="btn btn--primary btn--full"),
            A(t("migration.open_sample"), href=open_href, cls="btn btn--secondary btn--full mt-sm"),
        )
    try:
        rows = (await api.migration_reconciliation(get_token(request), run_id)).get("rows") or []
    except APIError:
        rows = []
    totals = [row for row in rows if row.get("check") in _TOTAL_CHECKS]
    return wizard_page(
        request,
        auth_header(t("migration.success_title"), run.get("company_name", "")),
        Table(
            Thead(Tr(Th(t("migration.col_check")), Th(t("migration.col_celerp"), cls="cell--number"))),
            Tbody(*[Tr(Td(_check_label(row)), Td(_figure(row, "celerp"), cls="cell--number"))
                    for row in totals]),
            cls="data-table",
        ) if totals else "",
        pack,
        open_company,
        A(t("company_backup.download"), href=f"/company-backup/download?from_run={run_id}",
          cls="btn btn--secondary btn--full mt-sm"),
        A(t("migration.move_another"), href=f"{COMPANY.base}?from_run={run_id}",
          cls="btn btn--secondary btn--full mt-sm"),
    )


def _discard_page(request: Request, run: dict, error: str | None = None):
    run_id = run["id"]
    return wizard_page(
        request,
        auth_header(t("migration.discard")),
        flash(error) if error else "",
        P(t("migration.discard_confirm", company=run.get("company_name", ""))),
        Form(Button(t("migration.discard"), type="submit", cls="btn btn--danger btn--full"),
             method="post", action=f"/migrations/{run_id}/discard", cls="auth-form"),
        back_link(f"/migrations/{run_id}"),
    )
