# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Company migration wizard: move a company's books from another system into Celerp.

The wizard runs in two modes that share every step renderer and differ only in
their base path, Back target and API authentication:

- bootstrap (`/setup/migrate`): no user exists yet; the first owner and company
  are created by the migration itself.
- company (`/setup/new-company/migrate`): a signed-in owner adds another company.

Steps: choose source and upload, coverage and method, mapping (only when the scan
asks questions), review, then the run pages under `/migrations/{run_id}`: progress,
verify, complete, discard.

The scan token lives only in an HttpOnly cookie scoped to the mode's base path and
in request bodies to the API, never in a URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, StreamingResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import auth_shell, client_scripts, flash, page_title
from ui.components.table import searchable_select
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


@dataclass(frozen=True)
class _Mode:
    key: str
    base: str
    back: str

    @property
    def bootstrap(self) -> bool:
        return self.key == "bootstrap"


BOOTSTRAP = _Mode("bootstrap", "/setup/migrate", "/setup")
COMPANY = _Mode("company", "/setup/new-company/migrate", "/setup/new-company")


# ---------------------------------------------------------------------------
# Scan state
# ---------------------------------------------------------------------------
# The scan lives in the API's scan store and every step reads it back by token.
# The Prepared by name typed at upload rides in its own cookie until the first
# decisions save stores it with the scan.

PREPARED_BY_COOKIE = "celerp_migration_prepared_by"


def _set_scan_cookies(resp, token: str, prepared_by: str, mode: _Mode, request: Request) -> None:
    for name, value in ((SCAN_COOKIE, token), (PREPARED_BY_COOKIE, prepared_by)):
        resp.set_cookie(name, value, max_age=SCAN_TTL_SECONDS, path=mode.base, httponly=True,
                        samesite="strict", secure=session_cookie_secure(request),
                        domain=cookie_domain(request))


def _clear_scan_cookie(resp, mode: _Mode, request: Request) -> None:
    for name in (SCAN_COOKIE, PREPARED_BY_COOKIE):
        resp.delete_cookie(name, path=mode.base, domain=cookie_domain(request))


async def _read_scan(request: Request, mode: _Mode, token: str) -> dict:
    """The scan entry for a token: the API's scan view plus the Prepared by name, or
    ``{"run_id"}`` when a run was already started from the scan. Raises APIError."""
    body = await api.migration_scan_read(_api_token(request, mode), token)
    if "run_id" in body:
        return {"run_id": body["run_id"]}
    scan = body["scan"]
    saved = (scan.get("decisions") or {}).get("prepared_by")
    return {"scan": scan, "prepared_by": saved or request.cookies.get(PREPARED_BY_COOKIE, "")}


# ---------------------------------------------------------------------------
# Shared page pieces
# ---------------------------------------------------------------------------

def _page(request: Request, *content, status_code: int = 200):
    page = auth_shell(*client_scripts(get_lang(request)), Div(*content, cls="auth-card migration-wizard"),
                      title=page_title("migration.title"))
    if status_code == 200:
        return page
    return HTMLResponse(to_xml(page), status_code=status_code)


def _back(href: str) -> FT:
    label = t("auth.back_to_setup") if href == "/setup" else t("btn.back")
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


def _field_error(errors: dict, field: str) -> FT | str:
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
    """The setup choice screen shared by /setup and /setup/new-company."""
    return Div(
        auth_header(title, subtitle),
        Div(*cards, cls="quick-links-grid"),
        back,
        cls="onboarding-card setup-chooser",
    )


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

async def _gate(request: Request, mode: _Mode):
    """Return a response when the request may not use this mode, else None."""
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
        return _page(request, auth_header(t("migration.title")),
                     flash(t("migration.owner_only")), _back(mode.back), status_code=403)
    return None


def _api_token(request: Request, mode: _Mode) -> str | None:
    return None if mode.bootstrap else get_token(request)


def _expired(request: Request, mode: _Mode, message: str | None = None):
    resp = _page(request, auth_header(t("migration.title")),
                 flash(message or t("migration.scan_expired")),
                 A(t("migration.upload_again"), href=mode.base, cls="btn btn--primary btn--full"),
                 _back(mode.back))
    if not isinstance(resp, HTMLResponse):
        resp = HTMLResponse(to_xml(resp))
    _clear_scan_cookie(resp, mode, request)
    return resp


async def _current_scan(request: Request, mode: _Mode):
    """(token, entry) for the request's scan, or (None, the response explaining why not)."""
    token = request.cookies.get(SCAN_COOKIE)
    if not token:
        return None, _expired(request, mode)
    try:
        entry = await _read_scan(request, mode, token)
    except APIError as e:
        if e.status == 410:
            return None, _expired(request, mode, str(e.detail))
        return None, _page(request, auth_header(t("migration.title")), flash(str(e.detail)), _back(mode.back),
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


async def _setup_code_required(mode: _Mode) -> bool:
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


def _setup_code_field() -> FT:
    from celerp.config import config_path
    return Div(
        Label(t("label.setup_code"), For="setup_code", cls="form-label"),
        Input(type="text", id="setup_code", name="setup_code", required=True, cls="form-input"),
        P(t("msg.setup_code_hint", path=str(config_path().parent / "setup-code")), cls="form-hint"),
        cls="form-group",
    )


async def _source_page(request: Request, mode: _Mode, *, selected: str = "", prepared_by: str = "",
                       error: str | None = None, back: str | None = None):
    sources, sources_error = await _sources()
    code_required = await _setup_code_required(mode)
    extensions = sorted({ext for s in sources for a in s.get("artifacts", []) for ext in a.get("extensions", [])})
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
            Input(type="file", id="files", name="files", multiple=True, required=True,
                  accept=",".join(extensions) if extensions else None, cls="form-input"),
            cls="form-group",
        ),
        Div(
            Label(t("migration.prepared_by"), For="prepared_by", cls="form-label"),
            Input(type="text", id="prepared_by", name="prepared_by", value=prepared_by, cls="form-input"),
            P(t("migration.prepared_by_hint"), cls="form-hint"),
            cls="form-group",
        ),
        _setup_code_field() if code_required else "",
        P(t("migration.retention"), cls="form-hint"),
        Button(t("migration.analyze"), type="submit", cls="btn btn--primary btn--full"),
        method="post", action=f"{mode.base}/scan", enctype="multipart/form-data", cls="auth-form",
    )
    sample = Form(
        _setup_code_field() if code_required else "",
        Button(t("migration.try_sample"), type="submit", cls="btn btn--secondary btn--full"),
        method="post", action=f"{mode.base}/sample", cls="auth-form mt-sm",
    )
    return _page(
        request,
        _steps(1),
        auth_header(t("migration.title"), t("migration.source_subtitle")),
        flash(error) if error else "",
        flash(sources_error) if sources_error else "",
        upload,
        sample,
        _source_request_form(),
        _back(back or mode.back),
    )


def _change_source_page(request: Request, mode: _Mode, entry: dict, source: str):
    scan = entry["scan"]
    return _page(
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
        _back(mode.back),
    )


async def _choose_source(request: Request, mode: _Mode):
    if (denied := await _gate(request, mode)) is not None:
        return denied
    q = request.query_params
    source = q.get("source", "")
    prepared_by = q.get("prepared_by", "")
    back = None
    clear = False
    token = request.cookies.get(SCAN_COOKIE)

    from_run = q.get("from_run", "") if not mode.bootstrap else ""
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


def _with_cleared_scan(resp, request: Request, mode: _Mode):
    if not isinstance(resp, HTMLResponse):
        resp = HTMLResponse(to_xml(resp))
    _clear_scan_cookie(resp, mode, request)
    return resp


async def _scan_and_continue(request: Request, mode: _Mode, files: list, source: str | None,
                             prepared_by: str, setup_code: str | None):
    try:
        result = await api.migration_scan(_api_token(request, mode), files, source, setup_code=setup_code)
    except APIError as e:
        return await _source_page(request, mode, selected=source or "", prepared_by=prepared_by,
                                  error=str(e.detail))
    resp = RedirectResponse(f"{mode.base}/coverage", status_code=303)
    _set_scan_cookies(resp, result["scan_token"], prepared_by, mode, request)
    return resp


async def _upload(request: Request, mode: _Mode):
    if (denied := await _gate(request, mode)) is not None:
        return denied
    form = await request.form()
    source = str(form.get("source", "")).strip() or None
    prepared_by = str(form.get("prepared_by", "")).strip()
    setup_code = str(form.get("setup_code", "")).strip() or None
    uploads = [f for f in form.getlist("files") if getattr(f, "filename", "")]
    if not uploads:
        return await _source_page(request, mode, selected=source or "", prepared_by=prepared_by,
                                  error=t("migration.choose_file"))
    files = [(f.filename, f.file) for f in uploads]
    return await _scan_and_continue(request, mode, files, source, prepared_by, setup_code)


async def _sample(request: Request, mode: _Mode):
    if (denied := await _gate(request, mode)) is not None:
        return denied
    form = await request.form()
    setup_code = str(form.get("setup_code", "")).strip() or None
    try:
        from celerp.importers.sample import SAMPLE_ARTIFACT
    except ImportError:
        return await _source_page(request, mode, error=t("migration.sample_unavailable"))
    try:
        artifact = Path(SAMPLE_ARTIFACT)
        with artifact.open("rb") as fh:
            return await _scan_and_continue(request, mode, [(artifact.name, fh)], None, "",
                                            setup_code)
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


def _coverage_page(request: Request, mode: _Mode, entry: dict, errors: dict | None = None,
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
        _field_error(errors, "mode"),
        cls="form-group",
    )
    cutover = Div(
        Label(t("migration.cutover_date"), For="cutover_date", cls="form-label"),
        Input(type="date", id="cutover_date", name="cutover_date", value=decisions.get("cutover_date") or "",
              cls="form-input"),
        _field_error(errors, "cutover_date"),
        cls="form-group",
    )
    return _page(
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
            _field_error(errors, "mappings"),
            _field_error(errors, "prepared_by"),
            Button(t("btn.continue"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/decisions", cls="auth-form",
        ),
        _back(mode.base),
    )


async def _coverage(request: Request, mode: _Mode):
    if (denied := await _gate(request, mode)) is not None:
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


def _mapping_page(request: Request, mode: _Mode, entry: dict, errors: dict | None = None,
                  error: str | None = None):
    scan = entry["scan"]
    current = _decisions(scan).get("mappings") or {}
    errors = errors or {}
    questions = scan.get("questions") or []
    return _page(
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
                           _field_error(errors, q["key"])),
                    )
                    for q in questions
                ]),
                cls="data-table",
            ),
            _field_error(errors, "mappings"),
            Button(t("btn.continue"), type="submit", cls="btn btn--primary btn--full mt-md"),
            method="post", action=f"{mode.base}/decisions", cls="auth-form",
        ),
        _back(f"{mode.base}/coverage"),
    )


async def _mapping(request: Request, mode: _Mode):
    if (denied := await _gate(request, mode)) is not None:
        return denied
    token, entry = await _current_scan(request, mode)
    if token is None:
        return entry
    if not entry["scan"].get("questions"):
        return RedirectResponse(f"{mode.base}/review", status_code=303)
    return _mapping_page(request, mode, entry)


async def _save_decisions(request: Request, mode: _Mode):
    if (denied := await _gate(request, mode)) is not None:
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
        new_scan = await api.migration_save_decisions(_api_token(request, mode), token, decisions)
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

def _account_fields(values: dict) -> list:
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


async def _review_page(request: Request, mode: _Mode, entry: dict, *, values: dict | None = None,
                       error: str | None = None, errors: dict | None = None):
    scan = entry["scan"]
    decisions = _decisions(scan)
    values = values or {}
    errors = errors or {}
    method = t("migration.cutover") if decisions.get("mode") == "cutover" else t("migration.full_history")
    if decisions.get("mode") == "cutover":
        method = f"{method} ({decisions.get('cutover_date') or '--'})"
    counts = scan.get("object_counts") or {}
    changes = [row for row in scan.get("coverage") or [] if row.get("coverage_class") == "mapped_with_loss"]
    rows = [
        (t("migration.file"), scan.get("file_name", "")),
        (t("migration.method"), method),
        (t("migration.prepared_by"), entry.get("prepared_by") or "--"),
    ]
    back = f"{mode.base}/mapping" if scan.get("questions") else f"{mode.base}/coverage"
    return _page(
        request,
        _steps(4),
        auth_header(t("migration.review_title"), t("migration.review_subtitle")),
        flash(error) if error else "",
        Table(Tbody(*[Tr(Td(k), Td(v)) for k, v in rows]), cls="data-table"),
        H3(t("migration.records")),
        Table(
            Thead(Tr(Th(t("migration.record_type")), Th(t("migration.count"), cls="cell--number"))),
            Tbody(*[Tr(Td(k.replace("_", " ")), Td(str(v), cls="cell--number")) for k, v in counts.items()]),
            cls="data-table",
        ) if counts else P(t("migration.no_records"), cls="form-hint"),
        Div(H3(t("migration.transformations")),
            Ul(*[Li(f"{row.get('source_type', '')}: {row.get('note') or '--'}") for row in changes])) if changes else "",
        _issues(scan),
        P(t("migration.expected_reconciliation"), cls="form-hint"),
        Form(
            Div(
                Label(t("label.company_name"), For="company_name", cls="form-label"),
                Input(type="text", id="company_name", name="company_name", cls="form-input",
                      value=values.get("company_name", scan.get("company_name") or "")),
                _field_error(errors, "company_name"),
                cls="form-group",
            ),
            *(_account_fields(values) if mode.bootstrap else []),
            _setup_code_field() if await _setup_code_required(mode) else "",
            Button(t("migration.create_and_migrate"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action=f"{mode.base}/start", cls="auth-form",
        ),
        _back(back),
    )


async def _review(request: Request, mode: _Mode):
    if (denied := await _gate(request, mode)) is not None:
        return denied
    token, entry = await _current_scan(request, mode)
    if token is None:
        return entry
    if not entry["scan"].get("decisions"):
        return RedirectResponse(f"{mode.base}/coverage", status_code=303)
    return await _review_page(request, mode, entry)


def _account_error(values: dict) -> str | None:
    from celerp.services.auth import MIN_PASSWORD_LENGTH
    if not all(values.get(k) for k in ("company_name", "name", "email", "password")):
        return t("settings.all_fields_required")
    if values["password"] != values.get("confirm_password"):
        return t("settings.passwords_do_not_match")
    if len(values["password"]) < MIN_PASSWORD_LENGTH:
        return t("settings.password_min_length")
    return None


async def _start(request: Request, mode: _Mode):
    if (denied := await _gate(request, mode)) is not None:
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
            if (problem := _account_error(values)) is not None:
                return await review(error=problem)
            if await _setup_code_required(mode) and not setup_code:
                return await review(error=t("auth.setup_code_required"))
            started = await api.migration_bootstrap_start(
                token, values["company_name"], values["name"], values["email"], values["password"],
                setup_code=setup_code)
        else:
            started = await api.migration_start_from_scan(get_token(request), token, values["company_name"])
    except APIError as e:
        if e.status == 410:
            return _expired(request, mode, str(e.detail))
        if isinstance(e.detail, dict):
            return await review(errors=e.detail)
        return await review(error=str(e.detail))
    resp = RedirectResponse(f"/migrations/{started['run_id']}", status_code=303)
    if mode.bootstrap:
        # The first owner has no working session yet. A company-mode start keeps the
        # current session: the new company is opened only after the migration finishes.
        set_session_cookies(resp, started["access_token"], started["refresh_token"], request)
    _clear_scan_cookie(resp, mode, request)
    return resp


# ---------------------------------------------------------------------------
# Run pages
# ---------------------------------------------------------------------------

def _run_error_page(request: Request, message: str):
    return _page(request, auth_header(t("migration.title")), flash(message), _back("/"))


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
    elif status == "ready":
        buttons.append(Form(Button(t("migration.start"), type="submit", cls="btn btn--primary"),
                            method="post", action=f"/migrations/{run_id}/start"))
    if status == "ready_to_finalize":
        buttons.append(A(t("migration.check_results"), href=f"/migrations/{run_id}/verify", cls="btn btn--primary"))
    if status == "completed":
        buttons.append(A(t("btn.continue"), href=f"/migrations/{run_id}/complete", cls="btn btn--primary"))
    else:
        buttons.append(A(t("migration.discard"), href=f"/migrations/{run_id}/discard", cls="btn btn--secondary"))
    return Div(*buttons, cls="form-actions")


def _progress_fragment(run: dict, error: str | None = None) -> FT:
    status = run.get("status", "")
    phases = run.get("phases") or []
    polling = {"hx_get": f"/migrations/{run['id']}", "hx_trigger": "every 2s", "hx_swap": "outerHTML"} \
        if status in _ACTIVE_STATUSES else {}
    errors = run.get("error_summary") or {}
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
        Ul(*[Li(f"{k}: {v}") for k, v in errors.items()]) if errors else "",
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
    return _page(
        request,
        _steps(5),
        auth_header(t("migration.progress_title", company=run.get("company_name", "")),
                t("migration.progress_subtitle")),
        _progress_fragment(run, error),
        P(t("migration.retention"), cls="form-hint"),
    )


def migrations_routes(app) -> None:
    """Register the wizard for both modes and the run pages."""

    for mode in (BOOTSTRAP, COMPANY):
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
        if target == "/setup":
            clear_session_cookies(resp, request)
        return resp


def _register_wizard(app, mode: _Mode) -> None:
    steps = (
        ("get", "", _choose_source), ("post", "/scan", _upload), ("post", "/sample", _sample),
        ("get", "/coverage", _coverage), ("get", "/mapping", _mapping),
        ("post", "/decisions", _save_decisions), ("get", "/review", _review), ("post", "/start", _start),
    )
    for method, suffix, handler in steps:
        getattr(app, method)(f"{mode.base}{suffix}")(_bind(handler, mode))


def _bind(handler, mode: _Mode):
    async def route(request: Request):
        return await handler(request, mode)
    route.__name__ = f"migrate_{mode.key}{handler.__name__}"
    return route


# ---------------------------------------------------------------------------
# Verify, complete, discard
# ---------------------------------------------------------------------------

def _check_label(row: dict) -> str:
    label = t(f"migration.check.{row.get('check', '')}")
    extra = " ".join(x for x in (row.get("key") or "", row.get("currency") or "") if x)
    return f"{label} {extra}".strip()


async def _verify_page(request: Request, run_id: str, error: str | None = None):
    token = get_token(request)
    try:
        recon = await api.migration_reconciliation(token, run_id)
        run = await api.get_migration_run(token, run_id)
    except APIError as e:
        if e.status != 409:
            return _run_error_page(request, str(e.detail))
        return _page(request, _steps(6), auth_header(t("migration.verify_title")), flash(error) if error else "",
                     P(str(e.detail), cls="form-hint"), _back(f"/migrations/{run_id}"))
    rows = recon.get("rows") or []
    lock_date = run.get("lock_date")
    return _page(
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
                    Td(str(row.get("source") or "--"), cls="cell--number"),
                    Td(str(row.get("celerp") or "--"), cls="cell--number"),
                    Td(str(row.get("difference") or "--"), cls="cell--number"),
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
        _back(f"/migrations/{run_id}"),
    )


async def _complete_page(request: Request, run: dict):
    run_id = run["id"]
    pack = P(A(t("migration.download_pack"), href=f"/migrations/{run_id}/pack", cls="auth-link"))
    open_href = f"/switch-company/{run['company_id']}"
    open_company = A(t("migration.open_company"), href=open_href, cls="btn btn--primary btn--full")
    if run.get("is_sample"):
        return _page(
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
    return _page(
        request,
        auth_header(t("migration.success_title"), run.get("company_name", "")),
        Table(
            Thead(Tr(Th(t("migration.col_check")), Th(t("migration.col_celerp"), cls="cell--number"))),
            Tbody(*[Tr(Td(_check_label(row)), Td(str(row.get("celerp") or "--"), cls="cell--number"))
                    for row in totals]),
            cls="data-table",
        ) if totals else "",
        pack,
        open_company,
        A(t("migration.move_another"), href=f"{COMPANY.base}?from_run={run_id}",
          cls="btn btn--secondary btn--full mt-sm"),
    )


def _discard_page(request: Request, run: dict, error: str | None = None):
    run_id = run["id"]
    return _page(
        request,
        auth_header(t("migration.discard")),
        flash(error) if error else "",
        P(t("migration.discard_confirm", company=run.get("company_name", ""))),
        Form(Button(t("migration.discard"), type="submit", cls="btn btn--danger btn--full"),
             method="post", action=f"/migrations/{run_id}/discard", cls="auth-form"),
        _back(f"/migrations/{run_id}"),
    )
