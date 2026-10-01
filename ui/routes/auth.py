# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Auth + onboarding routes.

State machine:
    bootstrapped=false  → /setup           (first-admin + company wizard)
    bootstrapped=true   → /login           (normal login)
    logged in, company setup pending and role can set it up
                        → /onboarding      (getting-started hub)
    logged in, otherwise → /dashboard

/register is disabled at the public URL once bootstrapped.
"""

from __future__ import annotations

import json

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError, bootstrap_status
from ui.api_client import login as api_login, login_force as api_login_force, logout as api_logout, register as api_register
from ui.api_client import my_companies as api_my_companies
from ui.api_client import get_company as api_get_company
from ui.api_client import migration_staged_run as api_migration_staged_run
from ui.components.shell import auth_shell, flash, page_title, star_supporter_card, toast_header
from ui.config import COOKIE_NAME, REFRESH_COOKIE_NAME, get_role, set_session_cookies, clear_session_cookies
from ui.i18n import t, get_lang
from ui.routes.csv_import import ONBOARDING_MARKER
from ui.security import is_app_local_path
from celerp.config import settings as _settings
from celerp.services.auth import MIN_PASSWORD_LENGTH
from celerp.services.permissions import role_has_permission


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
    from celerp.services.backup_import import missing_modules_sentence
    company = notice.get("company_name") or "backup"
    parts = [t("auth.restore_notice_message", company=company)]
    if notice.get("schema_warning"):
        parts.append(str(notice["schema_warning"]))
    warnings = list(notice.get("warnings") or [])
    if warnings:
        parts.append(missing_modules_sentence(warnings))
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
                           kind="warning" if (restore_notice.get("warnings") or restore_notice.get("schema_warning")) else "success")
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
            return auth_shell(_login_form(email=email, error=e.detail, next_url=nxt), title=page_title("btn.sign_in"))
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
            return auth_shell(_login_form(email=email, error=e.detail, next_url=nxt), title=page_title("btn.sign_in"))
        except Exception as e:
            return auth_shell(_login_form(email=email, error=t("auth.server_error", e=e), next_url=nxt), title=page_title("btn.sign_in"))
        resp = RedirectResponse(nxt, status_code=302)
        set_session_cookies(resp, access_token, refresh_token, request)
        return resp

    # ── Bootstrap wizard: first-admin + company setup ───────────────────────

    @app.get("/setup")
    async def setup_page(request: Request):
        if (gate := await _unbootstrapped_gate(request)) is not None:
            return gate
        from ui.api_client import setup_code_required as _code_req
        return auth_shell(_setup_chooser(code_required=await _code_req()), title=t("page.setup"))

    @app.get("/setup/fresh")
    async def setup_fresh_page(request: Request):
        if (gate := await _unbootstrapped_gate(request)) is not None:
            return gate
        from ui.api_client import setup_code_required as _code_req
        return auth_shell(_setup_form(setup_code_required=await _code_req()), title=t("page.setup"))

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
            title=page_title("page.restore_from_backup"),
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
            return auth_shell(_setup_import_form(error=t("auth.select_backup_file")), title=page_title("page.restore_from_backup"))
        code_required = await api.setup_code_required()
        if code_required and not setup_code:
            return auth_shell(
                _setup_import_form(
                    error=t("auth.setup_code_required"),
                    setup_code_required=True,
                ),
                title=page_title("page.restore_from_backup"),
            )
        if getattr(file, "size", None) == 0:
            return auth_shell(
                _setup_import_form(
                    error=t("auth.file_empty"),
                    setup_code_required=code_required,
                ),
                title=page_title("page.restore_from_backup"),
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
                    try:
                        detail = r.json().get("detail", t("auth.import_failed"))
                    except Exception:
                        detail = t("auth.import_failed_unreadable")
                else:
                    detail = r.text[:300] or t("auth.import_failed")
                return auth_shell(
                    _setup_import_form(
                        error=detail, setup_code_required=code_required
                    ),
                    title=page_title("page.restore_from_backup"),
                )
            # Success - surface missing-module / schema warnings (if any) on the form
            warnings: list[str] = []
            schema_warning: str | None = None
            restart_scheduled = False
            try:
                payload = r.json()
                warnings = list(payload.get("warnings") or [])
                schema_warning = payload.get("schema_warning") or None
                restart_scheduled = bool(payload.get("restart_scheduled"))
            except Exception:
                warnings = []
            if warnings or schema_warning:
                # Show a non-blocking warning page with "Continue anyway" link
                parts: list[str] = []
                if schema_warning:
                    parts.append(schema_warning)
                if warnings:
                    from celerp.services.backup_import import missing_modules_sentence
                    parts.append(missing_modules_sentence(warnings))
                if restart_scheduled:
                    parts.append(t("auth.restore_restart_note"))
                warn_msg = t("auth.restore_complete_but") + " ".join(parts)
                return auth_shell(
                    _setup_import_form(
                        warning=warn_msg,
                        continue_to="/login?imported=1",
                    ),
                    title=page_title("page.restore_from_backup"),
                )
        except httpx.TimeoutException:
            return auth_shell(
                _setup_import_form(
                    error=t("auth.import_timed_out"),
                    setup_code_required=code_required,
                ),
                title=page_title("page.restore_from_backup"),
            )
        except Exception as exc:
            return auth_shell(
                _setup_import_form(
                    error=t("auth.connection_error", exc=repr(exc)),
                    setup_code_required=code_required,
                ),
                title=page_title("page.restore_from_backup"),
            )
        return RedirectResponse("/login?imported=1", status_code=302)

    @app.post("/setup")
    async def setup_submit(request: Request):
        try:
            bootstrapped = await bootstrap_status()
        except APIError as e:
            return auth_shell(_api_error_page(str(e.detail)), title=page_title("page.api_unavailable"))
        if bootstrapped:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        company_name = str(form.get("company_name", "")).strip()
        name = str(form.get("name", "")).strip()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        confirm = str(form.get("confirm_password", ""))
        setup_code = str(form.get("setup_code", "")).strip()

        from ui.api_client import setup_code_required as _code_req
        code_required = await _code_req()

        def _fail(msg):
            return auth_shell(_setup_form(company_name=company_name, name=name, email=email, error=msg,
                                          setup_code_required=code_required), title=t("page.setup"))

        if not all([company_name, name, email, password]):
            return _fail(t("settings.all_fields_required"))
        if password != confirm:
            return _fail(t("settings.passwords_do_not_match"))
        if len(password) < MIN_PASSWORD_LENGTH:
            return _fail(t("settings.password_min_length"))
        if code_required and not setup_code:
            return _fail(t("auth.setup_code_required"))
        try:
            access_token, refresh_token = await api_register(company_name, email, name, password,
                                                             setup_code=setup_code or None)
        except APIError as e:
            return _fail(e.detail)
        except Exception as e:
            return _fail(t("auth.server_error", e=e))
        resp = RedirectResponse("/setup/company", status_code=302)
        set_session_cookies(resp, access_token, refresh_token, request)
        return resp

    # ── Post-login landing: company picker or onboarding/dashboard ──────────

    @app.get("/")
    async def root(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            bootstrapped = await bootstrap_status()
            return RedirectResponse("/setup" if not bootstrapped else "/login", status_code=302)
        # Validate token - stale cookies (e.g. after init --force) must not
        # skip setup when the DB has been wiped.
        try:
            company = await api_get_company(token)
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
        # A company still being set up resumes its getting-started hub, but only for
        # someone who can set it up; the hub is a landing page, never a gate.
        if _resumes_onboarding(company, request):
            return RedirectResponse("/onboarding", status_code=302)
        return RedirectResponse("/dashboard", status_code=302)

    # ── Onboarding / data integration landing ───────────────────────────────

    @app.get("/onboarding")
    async def onboarding_page(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api_get_company(token)
        except APIError:
            return RedirectResponse("/login", status_code=302)
        return _onboarding_page(request)

    @app.post("/onboarding/complete")
    async def onboarding_complete(request: Request):
        """Finish the getting-started hub: clear the company's pending flag (when the
        role may change company settings) and go to the dashboard. The dashboard is
        only reported once the flag is stored; otherwise the hub is shown again with
        the reason, so the user can retry."""
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=303)
        try:
            company = await api_get_company(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=303)
            return _onboarding_page(request, error=t("onboarding.complete_failed", detail=e.detail))
        if _can_set_up(company, request):
            try:
                await api.patch_company(token, {"onboarding_pending": False})
            except APIError as e:
                return _onboarding_page(request, error=t("onboarding.complete_failed", detail=e.detail))
        return RedirectResponse("/dashboard", status_code=303)

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
            detail = r.json().get("detail", t("auth.reset_failed"))
            return auth_shell(_reset_password_form(token=token, error=detail), title=page_title("page.reset_password"))
        except Exception as e:
            return auth_shell(_reset_password_form(token=token, error=t("auth.server_error", e=e)), title=page_title("page.reset_password"))


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def _login_form(email: str = "", error: str | None = None, notice: str = "", next_url: str = "/") -> FT:
    lang = "en"
    return Div(
        auth_header(t("page.sign_in_to_celerp")),
        notice,
        Form(
            flash(error) if error else "",
            # Carries the page the user was bounced from, so signing back in
            # returns there instead of the dashboard.
            Input(type="hidden", name="next", value=next_url) if next_url != "/" else "",
            Div(Label(t("label.email", lang), For="email", cls="form-label"),
                Input(type="email", id="email", name="email", value=email,
                      placeholder="you@example.com", required=True, autofocus=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.password", lang), For="password", cls="form-label"),
                Input(type="password", id="password", name="password",
                      placeholder="••••••••", required=True, cls="form-input"),
                cls="form-group"),
            Button(t("btn.sign_in", lang), type="submit", cls="btn btn--primary btn--full"),
            P(A(t("auth.forgot_password"), href="/forgot-password", hx_get="/forgot-password",
                hx_swap="none", cls="auth-link"), cls="auth-footer-text"),
            method="post", action="/login", cls="auth-form",
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


def _setup_chooser(code_required: bool) -> FT:
    """First-run landing: every way to start, one card each."""
    from ui.routes.migrations import BOOTSTRAP, chooser, choice_card
    # The sample run needs the setup code when one is configured; the migration
    # source page asks for it next to its sample button.
    sample = (
        choice_card(t("setup.card_sample"), t("setup.card_sample_desc"), href=BOOTSTRAP.base)
        if code_required else
        choice_card(t("setup.card_sample"), t("setup.card_sample_desc"), post_to=f"{BOOTSTRAP.base}/sample")
    )
    return chooser(
        t("page.set_up_your_workspace"),
        t("msg.you_are_first_admin"),
        [
            choice_card(t("setup.card_new"), t("setup.card_new_desc"), href="/setup/fresh"),
            choice_card(t("setup.card_move"), t("setup.card_move_desc"), href=BOOTSTRAP.base),
            sample,
            choice_card(t("setup.card_restore"), t("setup.card_restore_desc"), href="/setup/import-backup"),
        ],
    )


def _setup_form(
    company_name: str = "", name: str = "", email: str = "", error: str | None = None,
    setup_code_required: bool = False,
) -> FT:
    lang = "en"
    _code_field = ""
    if setup_code_required:
        from celerp.config import config_path as _cp
        _code_field = Div(
            Label(t("label.setup_code"), For="setup_code", cls="form-label"),
            Input(type="text", id="setup_code", name="setup_code",
                  placeholder="", required=True, cls="form-input"),
            P(t("msg.setup_code_hint", path=str(_cp().parent / "setup-code")), cls="form-hint"),
            cls="form-group",
        )
    return Div(
        auth_header(t("page.set_up_your_workspace"), t("msg.you_are_first_admin", lang)),
        Form(
            flash(error) if error else "",
            Div(Label(t("label.company_name", lang), For="company_name", cls="form-label"),
                Input(type="text", id="company_name", name="company_name", value=company_name,
                      placeholder="Acme Corp", required=True, autofocus=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.your_name", lang), For="name", cls="form-label"),
                Input(type="text", id="name", name="name", value=name,
                      placeholder="Jane Smith", required=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.email", lang), For="email", cls="form-label"),
                Input(type="email", id="email", name="email", value=email,
                      placeholder="you@example.com", required=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.password", lang), For="password", cls="form-label"),
                Input(type="password", id="password", name="password",
                      placeholder=t("auth.ph_min_8_chars"), required=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.confirm_password", lang), For="confirm_password", cls="form-label"),
                Input(type="password", id="confirm_password", name="confirm_password",
                      placeholder="••••••••", required=True, cls="form-input"),
                cls="form-group"),
            _code_field,
            Button(t("btn.create_workspace", lang), type="submit", cls="btn btn--primary btn--full"),
            method="post", action="/setup", cls="auth-form",
        ),
        P(A(t("auth.back_to_setup"), href="/setup", cls="auth-link"), cls="auth-alt-action"),
        cls="auth-card",
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
        auth_header(t("page.restore_from_backup"), t("auth.upload_backup_desc")),
        Form(
            flash(error) if error else "",
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
            A(t("auth.back_to_setup"), href="/setup", cls="auth-link"),
            cls="auth-alt-action",
        ),
        cls="auth-card",
    )


def _can_set_up(company: dict, request: Request) -> bool:
    return role_has_permission(company.get("settings") or {}, get_role(request), "manage_company_settings")


def _resumes_onboarding(company: dict, request: Request) -> bool:
    return (company.get("settings") or {}).get("onboarding_pending") is True and _can_set_up(company, request)


# Getting-started actions: (page the action opens, title key, description key, is an
# import). Import pages open with the onboarding marker so their result offers a way back.
_ONBOARDING_ACTIONS: tuple[tuple[str, str, str, bool], ...] = (
    ("/inventory/import", "onboarding.products", "onboarding.file_desc", True),
    ("/crm/import/contacts", "onboarding.contacts", "onboarding.file_desc", True),
    ("/docs/import", "onboarding.documents", "onboarding.file_desc", True),
    ("/settings/cloud", "onboarding.connect", "onboarding.connect_desc", False),
)


def _onboarding_page(request: Request, error: str | None = None) -> FT:
    registered = {getattr(r, "path", None) for r in request.app.routes}
    return auth_shell(
        _onboarding_view(registered, error=error),
        title=page_title("page.get_started"),
    )


def _onboarding_view(registered: set[str], error: str | None = None) -> FT:
    """The getting-started hub. Only actions whose page is registered in this
    installation are offered."""
    cards = [
        A(
            Strong(t(title)),
            P(t(desc), cls="quick-link-desc"),
            href=f"{path}?{ONBOARDING_MARKER}=1" if is_import else path,
            cls="quick-link-card",
        )
        for path, title, desc, is_import in _ONBOARDING_ACTIONS
        if path in registered
    ]
    return Div(
        auth_header(t("onboarding.title"), t("onboarding.subtitle")),
        H2(t("onboarding.bring_in_data"), cls="section-title"),
        Div(*cards, cls="quick-links-grid"),
        Div(
            flash(error) if error else "",
            P(t("onboarding.start_working_desc"), cls="auth-subtitle"),
            Form(
                Button(t("onboarding.start_working"), type="submit", cls="btn btn--secondary"),
                method="post",
                action="/onboarding/complete",
            ),
            cls="mt-lg text-center",
        ),
        star_supporter_card("onboarding"),
        cls="onboarding-card",
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
                      placeholder="you@example.com", required=True, autofocus=True, cls="form-input"),
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
