# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Sign-in and first-run setup routes.

State machine:
    bootstrapped=false  -> /setup           (one form: first admin, company, business type, currency)
    bootstrapped=true   -> /login           (normal login)
    logged in           -> /dashboard

/register is disabled at the public URL once bootstrapped.
"""

from __future__ import annotations

import json
import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError, bootstrap_status
from ui.api_client import login as api_login, login_force as api_login_force, logout as api_logout, register as api_register
from ui.api_client import start_company as api_start_company
from ui.api_client import my_companies as api_my_companies
from ui.api_client import get_company as api_get_company
from ui.api_client import migration_staged_run as api_migration_staged_run
from ui.components.shell import auth_shell, client_scripts, flash, page_title, toast_header
from ui.config import COOKIE_NAME, REFRESH_COOKIE_NAME, set_session_cookies, clear_session_cookies
from ui.i18n import t, get_lang
from celerp.services.app_paths import is_app_local_path
from celerp.config import settings as _settings
from celerp.services.auth import MIN_PASSWORD_LENGTH, NO_COMPANY


logger = logging.getLogger(__name__)

# Where a login with no company left signs in and starts a new one.
START_COMPANY = "/setup/start-company"
START_COMPANY_RESTORE = f"{START_COMPANY}/restore-backup"
START_COMPANY_MIGRATE = f"{START_COMPANY}/migrate"


def auth_header(title: str, subtitle: str = "") -> FT:
    """The logo, title and optional subtitle that open every sign-in and setup card."""
    return Div(
        Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
        H1(title, cls="auth-title"),
        P(subtitle, cls="auth-subtitle") if subtitle else "",
        cls="auth-header",
    )


def _consume_restore_notice() -> dict | None:
    """Read and clear the one-shot restore notice the importer persists.

    The post-restore restart replaces whatever page showed the import result, so
    the outcome and any warnings are re-shown here, on the first login page after
    the restart, where the user cannot miss them."""
    from celerp.services.backup_import import RESTORE_NOTICE_FILE
    path = _settings.data_dir / RESTORE_NOTICE_FILE
    try:
        if not path.is_file():
            return None
        notice = json.loads(path.read_text())
        path.unlink(missing_ok=True)
        return notice if isinstance(notice, dict) else None
    except Exception:
        return None


def _restore_notice_message(notice: dict) -> str:
    """One readable sentence block for the post-restore login banner."""
    company = notice.get("company_name") or "backup"
    parts = [t("auth.restore_notice_message", company=company)]
    parts.extend(str(w) for w in notice.get("warnings") or [])
    if notice.get("safety_archive"):
        parts.append(t("system_recovery.safety_saved", path=notice["safety_archive"]))
    return " ".join(parts)


def setup_routes(app):

    # ── Pre-auth gate: check bootstrap state ────────────────────────────────

    def _safe_next(raw) -> str:
        """Same-app absolute paths only - ?next= must never become an open
        redirect or bounce back into the auth pages."""
        raw = str(raw or "")
        if is_app_local_path(raw) and not raw.startswith(("/login", "/logout")):
            return raw
        return "/"

    @app.get("/login")
    async def login_page(request: Request):
        nxt = _safe_next(request.query_params.get("next"))
        token = request.cookies.get(COOKIE_NAME)
        if token:
            # Validate before trusting - stale tokens (e.g. after init --force) must not
            # redirect back to dashboard and cause an infinite redirect loop.
            try:
                await api_get_company(token)
                return RedirectResponse(nxt, status_code=302)
            except APIError as e:
                if e.status == 401:
                    # Token invalid - clear it and fall through to login page
                    pass
                elif e.status == 404:
                    # Valid token but no company - redirect to setup
                    return RedirectResponse("/setup", status_code=302)
                elif e.status == 403 and (staged := await _staged_run_redirect(token)):
                    return staged
                else:
                    pass  # Any other error: show login page with cookie intact
        try:
            bootstrapped = await bootstrap_status()
        except APIError as e:
            return auth_shell(_api_error_page(str(e.detail)), title=page_title("page.api_unavailable"))
        if not bootstrapped:
            return RedirectResponse("/setup", status_code=302)
        deactivated = request.query_params.get("deactivated")
        reason = request.query_params.get("reason")
        by_ip_raw = request.query_params.get("by", "").strip()
        # Validate as IP address to prevent XSS - only show if it looks like a real IP
        import ipaddress as _ip
        try:
            by_ip = str(_ip.ip_address(by_ip_raw)) if by_ip_raw else ""
        except ValueError:
            by_ip = ""
        if deactivated:
            notice = flash(t("auth.company_deactivated"))
        elif reason == "evicted":
            ip_note = t("auth.evicted_from_ip", ip=by_ip) if by_ip else ""
            notice = flash(
                t("auth.evicted_message", ip_note=ip_note),
                kind="warning",
                raw=True,
            )
        elif reason == "expired":
            notice = flash(t("auth.session_expired_signin"), kind="warning")
        elif reason == "idle":
            notice = flash(t("auth.signed_out_idle"), kind="warning")
        elif reason == "password-changed":
            notice = flash(t("settings.password_changed"), kind="success")
        elif (restore_notice := _consume_restore_notice()) is not None:
            notice = flash(_restore_notice_message(restore_notice),
                           kind="warning" if restore_notice.get("warnings") else "success")
        elif request.query_params.get("imported"):
            notice = flash(t("auth.backup_restored_signin"), kind="success")
        else:
            notice = ""
        resp = auth_shell(_login_form(notice=notice, next_url=nxt), title=page_title("btn.sign_in"))
        if token:
            # Clear the invalid token so the browser doesn't keep sending it
            from starlette.responses import Response as _Resp
            from fasthtml.common import to_xml
            html_resp = _Resp(content=to_xml(resp), media_type="text/html")
            clear_session_cookies(html_resp, request)
            return html_resp
        return resp

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        nxt = _safe_next(form.get("next"))
        if not email or not password:
            return auth_shell(_login_form(email=email, error=t("auth.email_password_required"), next_url=nxt), title=page_title("btn.sign_in"))
        try:
            access_token, refresh_token = await api_login(email, password)
        except APIError as e:
            if e.status == 409 and e.detail == "direct_connection_limit":
                return auth_shell(
                    _direct_connection_gate(email, password),
                    title=page_title("btn.sign_in"),
                )
            if e.status == 401 and e.detail == NO_COMPANY:
                return RedirectResponse(START_COMPANY, status_code=302)
            return auth_shell(_login_form(email=email, error=_sign_in_error(e), next_url=nxt), title=page_title("btn.sign_in"))
        except Exception as e:
            return auth_shell(_login_form(email=email, error=t("auth.server_error", e=e), next_url=nxt), title=page_title("btn.sign_in"))
        resp = RedirectResponse(nxt, status_code=302)
        set_session_cookies(resp, access_token, refresh_token, request)
        return resp

    @app.post("/login-force")
    async def login_force_submit(request: Request):
        form = await request.form()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        nxt = _safe_next(form.get("next"))
        if not email or not password:
            return auth_shell(_login_form(email=email, error=t("auth.email_password_required"), next_url=nxt), title=page_title("btn.sign_in"))
        try:
            access_token, refresh_token = await api_login_force(email, password)
        except APIError as e:
            if e.status == 401 and e.detail == NO_COMPANY:
                return RedirectResponse(START_COMPANY, status_code=302)
            return auth_shell(_login_form(email=email, error=_sign_in_error(e), next_url=nxt), title=page_title("btn.sign_in"))
        except Exception as e:
            return auth_shell(_login_form(email=email, error=t("auth.server_error", e=e), next_url=nxt), title=page_title("btn.sign_in"))
        resp = RedirectResponse(nxt, status_code=302)
        set_session_cookies(resp, access_token, refresh_token, request)
        return resp

    @app.get(START_COMPANY)
    async def start_company_page(request: Request):
        return auth_shell(_start_company_form(), title=page_title("setup.start_company_title"))

    @app.post(START_COMPANY)
    async def start_company_submit(request: Request):
        form = await request.form()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        company_name = str(form.get("company_name", "")).strip()

        def _fail(msg):
            return auth_shell(_start_company_form(email=email, company_name=company_name, error=msg),
                              title=page_title("setup.start_company_title"))

        if not all([email, password, company_name]):
            return _fail(t("settings.all_fields_required"))
        try:
            access_token, refresh_token = await api_start_company(email, password, company_name)
        except APIError as e:
            if e.status == 409 and e.detail == "direct_connection_limit":
                return _fail(t("auth.direct_connection_gate_body"))
            return _fail(e.detail if isinstance(e.detail, str) else t("auth.server_error", e=e.detail))
        except Exception as e:
            return _fail(t("auth.server_error", e=e))
        resp = RedirectResponse("/setup/company", status_code=302)
        set_session_cookies(resp, access_token, refresh_token, request)
        return resp

    # ── Bootstrap wizard: first-admin + company setup ───────────────────────

    @app.get("/setup")
    async def setup_page(request: Request):
        if (gate := await _unbootstrapped_gate(request)) is not None:
            return gate
        from ui.api_client import setup_code_required as _code_req
        return await _setup_page(request, {}, code_required=await _code_req())

    @app.get("/setup/import-backup")
    async def setup_import_page(request: Request):
        if request.cookies.get(COOKIE_NAME):
            return RedirectResponse("/", status_code=302)
        try:
            bootstrapped = await bootstrap_status()
        except APIError as e:
            return auth_shell(_api_error_page(str(e.detail)), title=page_title("page.api_unavailable"))
        if bootstrapped:
            return RedirectResponse("/login", status_code=302)
        from ui.api_client import setup_code_required as _code_req
        return auth_shell(
            _setup_import_form(setup_code_required=await _code_req()),
            title=page_title("system_recovery.title"),
        )

    @app.post("/setup/import-backup")
    async def setup_import_submit(request: Request):
        import httpx
        try:
            bootstrapped = await bootstrap_status()
        except APIError as e:
            return auth_shell(_api_error_page(str(e.detail)), title=page_title("page.api_unavailable"))
        if bootstrapped:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        file = form.get("backup_file")
        setup_code = str(form.get("setup_code", "")).strip()
        if not file or not hasattr(file, "read"):
            return auth_shell(_setup_import_form(error=t("auth.select_backup_file")), title=page_title("system_recovery.title"))
        code_required = await api.setup_code_required()
        if code_required and not setup_code:
            return auth_shell(
                _setup_import_form(
                    error=t("auth.setup_code_required"),
                    setup_code_required=True,
                ),
                title=page_title("system_recovery.title"),
            )
        if getattr(file, "size", None) == 0:
            return auth_shell(
                _setup_import_form(
                    error=t("auth.file_empty"),
                    setup_code_required=code_required,
                ),
                title=page_title("system_recovery.title"),
            )
        try:
            await file.seek(0)
            async with api._local_client(timeout=120.0, follow_redirects=False, bulk=True) as c:
                r = await c.post(
                    "/backup/import-bootstrap",
                    files={
                        "file": (
                            file.filename,
                            file.file,
                            file.content_type or "application/octet-stream",
                        )
                    },
                    data={"setup_code": setup_code},
                )
            if r.status_code != 200:
                ct = r.headers.get("content-type", "")
                if ct.startswith("application/json"):
                    detail = api.error_text(r, t("auth.import_failed_unreadable"))
                else:
                    detail = r.text[:300] or t("auth.import_failed")
                return auth_shell(
                    _setup_import_form(
                        error=detail, setup_code_required=code_required
                    ),
                    title=page_title("system_recovery.title"),
                )
            # Success - surface missing-module warnings (if any) on the form
            warnings: list[str] = []
            restart_scheduled = False
            try:
                payload = r.json()
                warnings = list(payload.get("warnings") or [])
                restart_scheduled = bool(payload.get("restart_scheduled"))
            except Exception:
                warnings = []
            if warnings:
                # Show a non-blocking warning page with "Continue anyway" link
                parts: list[str] = [str(w) for w in warnings]
                if restart_scheduled:
                    parts.append(t("auth.restore_restart_note"))
                warn_msg = t("auth.restore_complete_but") + " ".join(parts)
                return auth_shell(
                    _setup_import_form(
                        warning=warn_msg,
                        continue_to="/login?imported=1",
                    ),
                    title=page_title("system_recovery.title"),
                )
        except httpx.TimeoutException:
            return auth_shell(
                _setup_import_form(
                    error=t("auth.import_timed_out"),
                    setup_code_required=code_required,
                ),
                title=page_title("system_recovery.title"),
            )
        except Exception as exc:
            return auth_shell(
                _setup_import_form(
                    error=t("auth.connection_error", exc=exc),
                    setup_code_required=code_required,
                ),
                title=page_title("system_recovery.title"),
            )
        return RedirectResponse("/login?imported=1", status_code=302)

    @app.post("/setup")
    async def setup_submit(request: Request):
        from ui.routes.setup import apply_company_setup, company_setup_error
        try:
            bootstrapped = await bootstrap_status()
        except APIError as e:
            return auth_shell(_api_error_page(str(e.detail)), title=page_title("page.api_unavailable"))
        if bootstrapped:
            # A second submit after setup finished: the signed-in owner goes on to the
            # dashboard, anyone else signs in.
            return RedirectResponse("/dashboard" if request.cookies.get(COOKIE_NAME) else "/login",
                                    status_code=302)
        form = await request.form()
        values = {k: str(form.get(k, "")).strip() for k in _SETUP_KEPT_FIELDS}
        password = str(form.get("password", ""))
        confirm = str(form.get("confirm_password", ""))

        from ui.api_client import setup_code_required as _code_req
        code_required = await _code_req()

        async def _fail(msg: str, field: str):
            return await _setup_page(request, values, code_required=code_required, error=msg, error_field=field)

        missing = next((f for f in ("company_name", "name", "email") if not values[f]), None)
        if missing or not password:
            return await _fail(t("settings.all_fields_required"), missing or "password")
        if password != confirm:
            return await _fail(t("settings.passwords_do_not_match"), "confirm_password")
        if len(password) < MIN_PASSWORD_LENGTH:
            return await _fail(t("settings.password_min_length"), "password")
        if code_required and not values["setup_code"]:
            return await _fail(t("auth.setup_code_required"), "setup_code")
        # The browser only suggests; the server checks every choice before anything
        # is created.
        if invalid := company_setup_error(values["currency"], values["vertical"]):
            return await _fail(invalid[1], invalid[0])
        try:
            access_token, refresh_token = await api_register(values["company_name"], values["email"],
                                                             values["name"], password,
                                                             setup_code=values["setup_code"] or None)
        except APIError as e:
            return await _fail(e.detail, "password")
        except Exception as e:
            return await _fail(t("auth.server_error", e=e), "password")
        # The account exists from here on, so every outcome signs the user in.
        try:
            nxt = await apply_company_setup(access_token, values["currency"], str(form.get("timezone", "")),
                                            values["vertical"])
        except Exception:
            # The retry page shows a fixed message; the reason stays in the server log.
            logger.exception("First-run setup could not apply the business type")
            nxt = "/setup/company?failed=1"
        resp = RedirectResponse(nxt, status_code=302)
        set_session_cookies(resp, access_token, refresh_token, request)
        return resp

    # ── Post-login landing ──────────────────────────────────────────────────

    @app.get("/")
    async def root(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            bootstrapped = await bootstrap_status()
            return RedirectResponse("/setup" if not bootstrapped else "/login", status_code=302)
        # Validate token - stale cookies (e.g. after init --force) must not
        # skip setup when the DB has been wiped.
        try:
            await api_get_company(token)
        except APIError as e:
            if e.status == 401:
                bootstrapped = await bootstrap_status()
                resp = RedirectResponse("/setup" if not bootstrapped else "/login", status_code=302)
                clear_session_cookies(resp, request)
                return resp
            elif e.status == 404:
                return RedirectResponse("/setup", status_code=302)
            elif e.status == 403 and (staged := await _staged_run_redirect(token)):
                return staged
            # Any other API error: let them through to dashboard (transient failure)
            return RedirectResponse("/dashboard", status_code=302)
        return RedirectResponse("/dashboard", status_code=302)

    # ── Company switcher (HTMX partial) ─────────────────────────────────────

    @app.get("/switch-company/{company_id}")
    async def do_switch_company_get(request: Request, company_id: str):
        """GET handler so the topbar <select> onchange can use location= directly."""
        return await do_switch_company(request, company_id)

    @app.post("/switch-company/{company_id}")
    async def do_switch_company(request: Request, company_id: str):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        from ui.api_client import switch_company as api_switch
        try:
            new_access, new_refresh = await api_switch(token, company_id)
        except APIError as e:
            return RedirectResponse(f"/?error={e.detail}", status_code=302)
        resp = RedirectResponse("/", status_code=302)
        set_session_cookies(resp, new_access, new_refresh, request)
        return resp

    # ── Logout ───────────────────────────────────────────────────────────────

    @app.post("/logout")
    async def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
        if token or refresh_token:
            await api_logout(token, refresh_token)
        resp = RedirectResponse("/login", status_code=302)
        clear_session_cookies(resp, request)
        return resp

    @app.get("/logout")
    async def logout_get(request: Request):
        """GET fallback for no-JS clients and the idle-timer. Clears tokens and redirects."""
        token = request.cookies.get(COOKIE_NAME)
        refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
        if token or refresh_token:
            await api_logout(token, refresh_token)
        from urllib.parse import urlencode
        params = {k: v for k, v in (("reason", request.query_params.get("reason", "")),
                                    ("next", request.query_params.get("next", ""))) if v}
        dest = f"/login?{urlencode(params)}" if params else "/login"
        resp = RedirectResponse(dest, status_code=302)
        clear_session_cookies(resp, request)
        return resp

    @app.get("/health")
    async def health_proxy():
        """Proxy /health to the API so version checks work from the UI port."""
        from starlette.responses import JSONResponse
        try:
            async with api._local_client(timeout=3.0, follow_redirects=False) as c:
                r = await c.get("/health")
                return JSONResponse(r.json(), status_code=r.status_code)
        except Exception:
            return JSONResponse({"status": "degraded", "version": ""}, status_code=503)

    @app.get("/health/system")
    async def health_system_proxy(request: Request):
        """Proxy /health/system to the API so the UI health banner works on any port.

        The API endpoint reports host resources and is authenticated, so forward
        the caller's session token. Without a token, or on any transient API
        failure, the banner degrades to a neutral state rather than surfacing an
        error - it is chrome, and the host data stays protected at the API."""
        from starlette.responses import JSONResponse
        token = request.cookies.get(COOKIE_NAME)
        try:
            if not token:
                raise RuntimeError("no session token")
            async with api._api_client(token, timeout=3.0) as c:
                r = await c.get("/health/system")
                return JSONResponse(r.json(), status_code=r.status_code)
        except Exception:
            return JSONResponse({"overall": "degraded", "api": "unreachable"}, status_code=503)

    # ── Password reset ───────────────────────────────────────────────────────

    @app.get("/forgot-password")
    async def forgot_password_page(request: Request):
        # Email-capable installs (SMTP or the paid relay) get the email-reset form. A
        # self-hosted install with no email transport resets via the CLI *by design*:
        # a browser on localhost can't prove machine ownership, but running the CLI does.
        # Rather than take the user to a full page, we surface the instruction as a
        # persistent toast and keep them on the login screen (clicked via HTMX).
        has_email = bool(_settings.gateway_token or _settings.smtp_host)
        is_htmx = request.headers.get("HX-Request") == "true"
        if not has_email:
            if is_htmx:
                msg = t("auth.reset_password_cli")
                return Response("", headers=toast_header(msg, "info", persist=True))
            # Direct URL entry with no JS: send back to login (the link there toasts).
            return RedirectResponse("/login", status_code=302)
        if is_htmx:
            # HTMX click on an email-capable install: full-navigate to render the form.
            return Response("", headers={"HX-Redirect": "/forgot-password"})
        return auth_shell(_forgot_password_form(), title=page_title("page.forgot_password"))

    @app.post("/forgot-password")
    async def forgot_password_submit(request: Request):
        form = await request.form()
        email = str(form.get("email", "")).strip()
        try:
            async with api._local_client(timeout=5.0, follow_redirects=False) as c:
                await c.post("/auth/password-reset/request", json={"email": email})
        except Exception:
            pass
        return auth_shell(
            _forgot_password_sent(),
            title=page_title("page.forgot_password"),
        )

    @app.get("/reset-password")
    async def reset_password_page(request: Request):
        token = request.query_params.get("token", "")
        return auth_shell(_reset_password_form(token=token), title=page_title("page.reset_password"))

    @app.post("/reset-password")
    async def reset_password_submit(request: Request):
        form = await request.form()
        token = str(form.get("token", ""))
        new_password = str(form.get("new_password", ""))
        confirm = str(form.get("confirm_password", ""))
        if new_password != confirm:
            return auth_shell(_reset_password_form(token=token, error=t("settings.passwords_do_not_match")), title=page_title("page.reset_password"))
        try:
            async with api._local_client(timeout=5.0, follow_redirects=False) as c:
                r = await c.post("/auth/password-reset/confirm", json={"token": token, "new_password": new_password})
            if r.status_code == 200:
                return RedirectResponse("/login", status_code=302)
            detail = api.error_text(r, t("auth.reset_failed"))
            return auth_shell(_reset_password_form(token=token, error=detail), title=page_title("page.reset_password"))
        except Exception as e:
            return auth_shell(_reset_password_form(token=token, error=t("auth.server_error", e=e)), title=page_title("page.reset_password"))


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def _sign_in_error(e: APIError) -> str:
    """Refused credentials read in the visitor's language; any other refusal shows the API's own message."""
    return t("auth.invalid_credentials") if e.status == 401 else e.detail


def _login_form(email: str = "", error: str | None = None, notice: str = "", next_url: str = "/") -> FT:
    return Div(
        auth_header(t("page.sign_in_to_celerp")),
        notice,
        Form(
            flash(error) if error else "",
            # Carries the page the user was bounced from, so signing back in
            # returns there instead of the dashboard.
            Input(type="hidden", name="next", value=next_url) if next_url != "/" else "",
            Div(Label(t("label.email"), For="email", cls="form-label"),
                Input(type="email", id="email", name="email", value=email,
                      placeholder=t("account.email_placeholder"), required=True, autofocus=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.password"), For="password", cls="form-label"),
                Input(type="password", id="password", name="password",
                      placeholder="••••••••", required=True, cls="form-input"),
                cls="form-group"),
            Button(t("btn.sign_in"), type="submit", cls="btn btn--primary btn--full"),
            P(A(t("auth.forgot_password"), href="/forgot-password", hx_get="/forgot-password",
                hx_swap="none", cls="auth-link"), cls="auth-footer-text"),
            method="post", action="/login", cls="auth-form",
        ),
        cls="auth-card",
    )


def _start_company_form(email: str = "", company_name: str = "", error: str | None = None) -> FT:
    """Sign in and name a new company, move one in from another system, or restore a
    company backup: the ways back in for a login whose last company was reset."""
    return Div(
        auth_header(t("setup.start_company_title"), t("setup.start_company_subtitle")),
        P(t("setup.start_company_explain"), cls="form-hint"),
        Form(
            flash(error) if error else "",
            Div(Label(t("label.email"), For="email", cls="form-label"),
                Input(type="email", id="email", name="email", value=email,
                      required=True, autofocus=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.password"), For="password", cls="form-label"),
                Input(type="password", id="password", name="password", required=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.company_name"), For="company_name", cls="form-label"),
                Input(type="text", id="company_name", name="company_name", value=company_name,
                      required=True, cls="form-input"),
                cls="form-group"),
            Button(t("setup.start_company_title"), type="submit", cls="btn btn--primary btn--full"),
            P(A(t("setup.card_move"), href=START_COMPANY_MIGRATE, cls="auth-link"), cls="auth-alt-action"),
            P(A(t("setup.card_restore"), href=START_COMPANY_RESTORE, cls="auth-link"), cls="auth-alt-action"),
            P(A(t("auth.back_to_login"), href="/login", cls="auth-link"), cls="auth-footer-text"),
            method="post", action=START_COMPANY, cls="auth-form",
        ),
        cls="auth-card",
    )


async def _staged_run_redirect(token: str) -> RedirectResponse | None:
    """A session on a company still being moved in lands on that company's migration run."""
    try:
        run = await api_migration_staged_run(token)
    except APIError:
        return None
    return RedirectResponse(f"/migrations/{run['id']}", status_code=302)


async def _unbootstrapped_gate(request: Request):
    """The response for a request that may not use first-run setup, else None."""
    if request.cookies.get(COOKIE_NAME):
        return RedirectResponse("/", status_code=302)
    try:
        bootstrapped = await bootstrap_status()
    except APIError as e:
        return auth_shell(_api_error_page(str(e.detail)), title=page_title("page.api_unavailable"))
    if bootstrapped:
        return RedirectResponse("/login", status_code=302)
    return None


# What a failed submit renders back: everything the user typed except the passwords.
_SETUP_KEPT_FIELDS = ("company_name", "name", "email", "setup_code", "vertical", "currency")


async def _setup_page(request: Request, values: dict, *, code_required: bool,
                      error: str | None = None, error_field: str = "") -> FT:
    from ui.components.start_options import supported_sources
    return auth_shell(
        *client_scripts(get_lang(request)),
        _setup_form(values, error=error, error_field=error_field, setup_code_required=code_required,
                    sources=await supported_sources()),
        title=t("page.setup"),
    )


# Keeps the typed values (never the passwords) while the user looks at an
# additional option and comes back, and makes Create a single submit. Runs before
# the dropdowns initialise, so a restored choice is the one they show.
_SETUP_FORM_JS = """
(function () {
  var form = document.getElementById('setup-form');
  var KEY = 'celerp-setup-form';
  var KEPT = %s;
  function field(name) { return form.querySelector('[name="' + name + '"]'); }
  function save() {
    var data = {};
    KEPT.forEach(function (name) { var el = field(name); if (el && el.value) data[name] = el.value; });
    try { sessionStorage.setItem(KEY, JSON.stringify(data)); } catch (e) {}
  }
  var saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(KEY) || '{}') || {}; } catch (e) {}
  KEPT.forEach(function (name) {
    var el = field(name);
    if (!el || el.value || !saved[name]) return;
    var wrap = el.closest('.combobox-wrap');
    if (!wrap) { el.value = saved[name]; return; }
    var opt = wrap.querySelector('.combobox-option[data-value="' + CSS.escape(saved[name]) + '"]');
    if (!opt) return;
    el.value = saved[name];
    wrap.querySelector('.combobox-input').value = opt.textContent;
  });
  save();
  form.addEventListener('input', save);
  form.addEventListener('change', save);
  window.addEventListener('pagehide', function () { if (!form.dataset.sent) save(); });
  var button = form.querySelector('button[type="submit"]');
  // A password mismatch is caught here so the typed passwords stay; the server
  // checks again and says the same thing under the same field.
  var confirm = field('confirm_password');
  var confirmError = document.getElementById('confirm_password-error');
  function markConfirm(message) {
    confirmError.textContent = message;
    confirmError.hidden = !message;
    if (message) {
      confirm.setAttribute('aria-invalid', 'true');
      confirm.setAttribute('aria-describedby', confirmError.id);
    } else {
      confirm.removeAttribute('aria-invalid');
      confirm.removeAttribute('aria-describedby');
    }
  }
  [field('password'), confirm].forEach(function (el) {
    el.addEventListener('input', function () { markConfirm(''); });
  });
  form.addEventListener('submit', function (e) {
    if (field('password').value !== confirm.value) {
      e.preventDefault();
      markConfirm(form.dataset.mismatch);
      confirm.focus();
      return;
    }
    form.dataset.sent = '1';
    try { sessionStorage.removeItem(KEY); } catch (e) {}
    button.disabled = true;
  });
  // Coming back with the browser's Back button shows the page from cache.
  window.addEventListener('pageshow', function () { button.disabled = false; delete form.dataset.sent; });
  var focus = form.dataset.focus && field(form.dataset.focus);
  if (focus) {
    var wrap = focus.closest('.combobox-wrap');
    (wrap ? wrap.querySelector('.combobox-input') : focus).focus();
  }
})();
""" % json.dumps(list(_SETUP_KEPT_FIELDS))


def _setup_form(
    values: dict, error: str | None = None, error_field: str = "",
    setup_code_required: bool = False, sources: list[str] | None = None,
) -> FT:
    from ui.components.start_options import start_options
    from ui.routes.company_backup import BOOTSTRAP as RESTORE
    from ui.routes.migrations import BOOTSTRAP as MIGRATE
    from ui.routes.setup import company_choice_fields, company_choice_script
    v = {k: values.get(k, "") for k in _SETUP_KEPT_FIELDS}

    # Text fields whose error is shown under the field rather than at the top.
    at_field: list[str] = []

    def _text(label_key: str, name: str, *, type: str = "text", autocomplete: str, hint: str = "") -> FT:
        bad = bool(error) and error_field == name
        if bad:
            at_field.append(name)
        return Div(
            Label(t(label_key), For=name, cls="form-label"),
            Input(type=type, id=name, name=name, value=v.get(name, "") if type != "password" else "",
                  required=True, autocomplete=autocomplete, cls="form-input",
                  aria_invalid="true" if bad else None, aria_describedby=f"{name}-error" if bad else None,
                  # The first field takes focus on a fresh page; after an error the
                  # script focuses the field the error is about.
                  autofocus=(name == "company_name" and not error)),
            P(hint, cls="form-hint") if hint else "",
            P(error if bad else "", id=f"{name}-error", cls="form-field-error", hidden=not bad),
            cls="form-group",
        )

    texts = (
        _text("label.company_name", "company_name", autocomplete="organization"),
        _text("label.your_name", "name", autocomplete="name"),
        _text("label.email", "email", type="email", autocomplete="email"),
        _text("label.password", "password", type="password", autocomplete="new-password",
              hint=t("setup.password_hint", n=MIN_PASSWORD_LENGTH)),
        _text("label.confirm_password", "confirm_password", type="password", autocomplete="new-password"),
    )

    code_field = ""
    if setup_code_required:
        from celerp.config import config_path as _cp
        code_field = Div(
            Label(t("label.setup_code"), For="setup_code", cls="form-label"),
            Input(type="text", id="setup_code", name="setup_code", value=v["setup_code"],
                  required=True, autocomplete="off", cls="form-input"),
            P(t("msg.setup_code_hint", path=str(_cp().parent / "setup-code")), cls="form-hint"),
            cls="form-group",
        )
    return Div(
        auth_header(t("page.set_up_your_workspace"), t("msg.you_are_first_admin")),
        Form(
            flash(error) if error and not at_field else "",
            *texts,
            code_field,
            *company_choice_fields(v["currency"], v["vertical"]),
            Button(t("btn.create_workspace"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action="/setup", id="setup-form", cls="auth-form",
            data_focus=error_field if error else "",
            data_mismatch=t("settings.passwords_do_not_match"),
        ),
        Script(_SETUP_FORM_JS),
        company_choice_script(),
        Div(
            H2(t("setup.options_heading"), cls="setup-options-heading"),
            start_options(restore_href=RESTORE.base, move_href=MIGRATE.base, sources=sources or []),
            P(t("setup.not_sure_yet"), cls="setup-options-note"),
            P(t("setup.later_from_dashboard"), cls="setup-options-note"),
            cls="setup-options",
        ),
        cls="auth-card setup-card",
    )


def _setup_import_form(
    error: str | None = None,
    warning: str | None = None,
    continue_to: str | None = None,
    setup_code_required: bool = False,
) -> FT:
    # When a warning is present, show a non-blocking "Continue" button instead
    # of re-rendering the form. The user can decide to proceed (GDR - never
    # restrict the UI; warn-and-continue is the rule).
    if warning:
        return Div(
            auth_header(t("auth.restore_complete"), warning),
            Div(
                A(
                    t("auth.continue_to_login"),
                    href=continue_to or "/login?imported=1",
                    cls="btn btn--primary btn--full",
                ),
                A(
                    t("btn.cancel"),
                    href="/setup",
                    cls="btn btn--secondary btn--full mt-sm",
                ),
                cls="auth-actions",
            ),
            cls="auth-card",
        )
    return Div(
        auth_header(t("system_recovery.title"), t("auth.upload_backup_desc")),
        Form(
            flash(error) if error else "",
            P(t("system_recovery.scope"), cls="flash flash--warning"),
            Div(
                Label(t("auth.backup_file_label"), For="backup_file", cls="form-label"),
                Input(type="file", id="backup_file", name="backup_file",
                      accept=".celerp-backup", required=True, cls="form-input"),
                cls="form-group",
            ),
            Div(
                Label(t("label.setup_code"), For="setup_code", cls="form-label"),
                Input(type="text", id="setup_code", name="setup_code",
                      required=True, cls="form-input"),
                cls="form-group",
            ) if setup_code_required else "",
            Button(t("auth.restore_backup_btn"), type="submit", id="restore-btn",
                   data_loading_label=t("auth.restoring"), cls="btn btn--primary btn--full"),
            Script("""
document.querySelector('#restore-btn').closest('form').addEventListener('submit', function() {
  var btn = document.getElementById('restore-btn');
  btn.disabled = true;
  btn.textContent = btn.getAttribute('data-loading-label');
  btn.classList.add('btn--loading');
});
"""),
            method="post", action="/setup/import-backup",
            enctype="multipart/form-data", cls="auth-form",
        ),
        P(
            A(t("auth.return_to_setup"), href="/setup", cls="auth-link"),
            cls="auth-alt-action",
        ),
        cls="auth-card",
    )


def _direct_connection_gate(email: str, password: str) -> FT:
    """Shown when a second user tries to log in without relay connected."""
    from celerp.gateway.state import (
        build_public_acquisition_url,
        get_commercial_mode,
        get_partner_identity,
        safe_support_email,
        safe_support_url,
    )
    from ui.components.cloud_gate import direct_price
    # Pre-auth surface: no authenticated app session is guaranteed here, so this
    # resolves through the public acquisition resolver rather than the in-app
    # mint route. It fails closed like the mint route (never a direct checkout
    # under partner_managed or an unknown mode) and, on a direct install, always
    # returns the anonymous celerp.com/subscribe URL with no instance_id, since
    # this pre-auth path can never mint the handoff token a named checkout needs.
    handoff_url = build_public_acquisition_url("cloud")
    # Keep the label in lockstep with that destination: the direct price/trial
    # label only when the URL is the direct subscribe page, a partner-support
    # label when it resolves to a partner support URL or mailto, and a
    # Contact-Celerp label on the Enterprise fallback (partner with no contact,
    # or any unknown mode). Mirrors build_public_acquisition_url's own ladder.
    _mode = get_commercial_mode()
    if _mode == "partner_managed":
        _identity = get_partner_identity() or {}
        if safe_support_url(_identity.get("support_url")) \
                or safe_support_email(_identity.get("support_email")):
            cta_label = t("cloud.partner_support")
        else:
            cta_label = t("cloud.contact_celerp")
    elif _mode == "celerp_direct":
        cta_label = t("btn.get_connect")
    else:
        cta_label = t("cloud.contact_celerp")

    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H2(t("page.direct_connections_are_one_at_a_time"),
               style="font-size:18px;"),
            P(
                t("auth.direct_connection_gate_body"),
                cls="auth-subtitle",
                style="text-align:left;",
            ),
            Div(
                A(cta_label,
                  href=handoff_url, target="_blank",
                  cls="btn btn--primary"),
                Form(
                    Input(type="hidden", name="email", value=email),
                    Input(type="hidden", name="password", value=password),
                    Button(t("btn.continue_sign_out_the_other_user"),
                           type="submit",
                           cls="btn btn--secondary"),
                    action="/login-force",
                    method="post",
                    style="display:inline;",
                ),
                style="display:flex;gap:12px;align-items:center;justify-content:center;margin-top:20px;flex-wrap:wrap;",
            ),
            cls="auth-header",
        ),
        cls="onboarding-card",
    )


def _api_error_page(message: str) -> FT:
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("error.api_unavailable"), cls="auth-title"),
            P(message, cls="auth-subtitle text-danger"),
            P(t("msg.api_server_not_running"), cls="auth-subtitle"),
            Pre(
                "uvicorn celerp.main:app --reload",
                cls="error-detail-box mt-sm",
            ),
            A(t("btn.retry"), href="/login", cls="btn btn--primary mt-md"),
            cls="auth-header",
        ),
        cls="auth-card",
    )


def _company_picker_panel(companies: list[dict]) -> FT:
    company_items = [
        Form(
            Button(
                c.get("company_name", ""),
                Span(c.get("role", ""), cls="picker-role"),
                type="submit",
                cls="company-picker-btn",
            ),
            method="post",
            action=f"/switch-company/{c['company_id']}",
            cls="company-picker-item",
        )
        for c in companies
    ]
    return Div(*company_items, cls="company-picker")


def _forgot_password_form(error: str | None = None) -> FT:
    return Div(
        auth_header(t("auth.forgot_password"), t("auth.enter_your_email_and_well_send_a_reset_link")),
        Form(
            flash(error) if error else "",
            Div(Label(t("th.email"), For="email", cls="form-label"),
                Input(type="email", id="email", name="email",
                      placeholder=t("account.email_placeholder"), required=True, autofocus=True, cls="form-input"),
                cls="form-group"),
            Button(t("btn.send_reset_link"), type="submit", cls="btn btn--primary btn--full"),
            P(A(t("auth.back_to_login"), href="/login", cls="auth-link"), cls="auth-footer-text"),
            method="post", action="/forgot-password", cls="auth-form",
        ),
        cls="auth-card",
    )


def _forgot_password_sent() -> FT:
    return Div(
        auth_header(t("page.check_your_email"), t("auth.if_that_email_exists_youll_receive_a_reset_link_sh")),
        Div(
            A(t("auth.back_to_login"), href="/login", cls="btn btn--primary"),
            cls="text-center mt-md",
        ),
        cls="auth-card",
    )


def _reset_password_form(token: str = "", error: str | None = None) -> FT:
    return Div(
        auth_header(t("page.reset_your_password"), t("auth.enter_your_new_password_below")),
        Form(
            flash(error) if error else "",
            Input(type="hidden", name="token", value=token),
            Div(Label(t("label.new_password"), For="new_password", cls="form-label"),
                Input(type="password", id="new_password", name="new_password",
                      placeholder=t("auth.ph_min_8_chars"), required=True, autofocus=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.confirm_password"), For="confirm_password", cls="form-label"),
                Input(type="password", id="confirm_password", name="confirm_password",
                      placeholder="••••••••", required=True, cls="form-input"),
                cls="form-group"),
            Button(t("btn.set_new_password"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action="/reset-password", cls="auth-form",
        ),
        cls="auth-card",
    )
