# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Auth + onboarding routes.

State machine:
    bootstrapped=false  → /setup           (first-admin + company wizard)
    bootstrapped=true   → /login           (normal login)
    logged in, no data  → /onboarding      (data integration landing)
    logged in, has data → /                (dashboard)
    
/register is disabled at the public URL once bootstrapped.
"""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from ui.api_client import APIError, bootstrap_status
from ui.api_client import login as api_login, login_force as api_login_force, register as api_register
from ui.api_client import my_companies as api_my_companies
from ui.api_client import get_company as api_get_company
from ui.components.shell import auth_shell, flash
from ui.config import COOKIE_NAME, REFRESH_COOKIE_NAME, cookie_domain
from ui.i18n import t, get_lang
from celerp.config import settings as _settings
from ui.config import API_BASE


def setup_routes(app):

    # ── Pre-auth gate: check bootstrap state ────────────────────────────────

    @app.get("/login")
    async def login_page(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            # Validate before trusting - stale tokens (e.g. after init --force) must not
            # redirect back to dashboard and cause an infinite redirect loop.
            try:
                await api_get_company(token)
                return RedirectResponse("/", status_code=302)
            except APIError as e:
                if e.status == 401:
                    # Token invalid - clear it and fall through to login page
                    pass
                elif e.status == 404:
                    # Valid token but no company - redirect to setup
                    return RedirectResponse("/setup", status_code=302)
                else:
                    pass  # Any other error: show login page with cookie intact
        try:
            bootstrapped = await bootstrap_status()
        except APIError as e:
            return auth_shell(_api_error_page(str(e.detail)), title="API Unavailable - Celerp")
        if not bootstrapped:
            return RedirectResponse("/setup", status_code=302)
        deactivated = request.query_params.get("deactivated")
        msg = flash("This company has been deactivated. Contact your administrator to reactivate it.") if deactivated else ""
        resp = auth_shell(_login_form(notice=msg), title="Sign in - Celerp")
        if token:
            # Clear the invalid token so the browser doesn't keep sending it
            from starlette.responses import Response as _Resp
            from fasthtml.common import to_xml
            html_resp = _Resp(content=to_xml(resp), media_type="text/html")
            html_resp.delete_cookie(COOKIE_NAME)
            html_resp.delete_cookie(REFRESH_COOKIE_NAME)
            return html_resp
        return resp

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        if not email or not password:
            return auth_shell(_login_form(email=email, error="Email and password required"), title="Sign in - Celerp")
        try:
            access_token, refresh_token = await api_login(email, password)
        except APIError as e:
            if e.status == 409 and e.detail == "direct_connection_limit":
                return auth_shell(
                    _direct_connection_gate(email, password),
                    title="Sign in - Celerp",
                )
            return auth_shell(_login_form(email=email, error=e.detail), title="Sign in - Celerp")
        except Exception as e:
            return auth_shell(_login_form(email=email, error=f"Server error: {e}"), title="Sign in - Celerp")
        resp = RedirectResponse("/", status_code=302)
        _set_tokens(resp, access_token, refresh_token, request)
        return resp

    @app.post("/login-force")
    async def login_force_submit(request: Request):
        form = await request.form()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        if not email or not password:
            return auth_shell(_login_form(email=email, error="Email and password required"), title="Sign in - Celerp")
        try:
            access_token, refresh_token = await api_login_force(email, password)
        except APIError as e:
            return auth_shell(_login_form(email=email, error=e.detail), title="Sign in - Celerp")
        except Exception as e:
            return auth_shell(_login_form(email=email, error=f"Server error: {e}"), title="Sign in - Celerp")
        resp = RedirectResponse("/", status_code=302)
        _set_tokens(resp, access_token, refresh_token, request)
        return resp

    # ── Bootstrap wizard: first-admin + company setup ───────────────────────

    @app.get("/setup")
    async def setup_page(request: Request):
        if request.cookies.get(COOKIE_NAME):
            return RedirectResponse("/", status_code=302)
        try:
            bootstrapped = await bootstrap_status()
        except APIError as e:
            return auth_shell(_api_error_page(str(e.detail)), title="API Unavailable - Celerp")
        if bootstrapped:
            return RedirectResponse("/login", status_code=302)
        return auth_shell(_setup_form(), title="Set up Celerp")

    @app.post("/setup")
    async def setup_submit(request: Request):
        try:
            bootstrapped = await bootstrap_status()
        except APIError as e:
            return auth_shell(_api_error_page(str(e.detail)), title="API Unavailable - Celerp")
        if bootstrapped:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        company_name = str(form.get("company_name", "")).strip()
        name = str(form.get("name", "")).strip()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        confirm = str(form.get("confirm_password", ""))

        def _fail(msg):
            return auth_shell(_setup_form(company_name=company_name, name=name, email=email, error=msg), title="Set up Celerp")

        if not all([company_name, name, email, password]):
            return _fail("All fields are required")
        if password != confirm:
            return _fail("Passwords do not match")
        if len(password) < 8:
            return _fail("Password must be at least 8 characters")
        try:
            access_token, refresh_token = await api_register(company_name, email, name, password)
        except APIError as e:
            return _fail(e.detail)
        except Exception as e:
            return _fail(f"Server error: {e}")
        resp = RedirectResponse("/setup/company", status_code=302)
        _set_tokens(resp, access_token, refresh_token, request)
        return resp

    # ── Post-login landing: company picker or onboarding/dashboard ──────────

    @app.get("/")
    async def root(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            bootstrapped = await bootstrap_status()
            return RedirectResponse("/setup" if not bootstrapped else "/login", status_code=302)
        # Validate token — stale cookies (e.g. after init --force) must not
        # skip setup when the DB has been wiped.
        try:
            await api_get_company(token)
            return RedirectResponse("/dashboard", status_code=302)
        except APIError as e:
            if e.status == 401:
                bootstrapped = await bootstrap_status()
                resp = RedirectResponse("/setup" if not bootstrapped else "/login", status_code=302)
                resp.delete_cookie(COOKIE_NAME)
                resp.delete_cookie(REFRESH_COOKIE_NAME)
                return resp
            elif e.status == 404:
                return RedirectResponse("/setup", status_code=302)
            # Any other API error: let them through to dashboard (transient failure)
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
        return auth_shell(
            _onboarding_view(),
            title="Get started - Celerp",
        )

    @app.get("/onboarding/upload/items")
    async def onboarding_upload_items(request: Request):
        return RedirectResponse("/inventory/import", status_code=302)

    @app.get("/onboarding/upload/contacts")
    async def onboarding_upload_contacts(request: Request):
        return RedirectResponse("/crm/import/contacts", status_code=302)

    @app.get("/onboarding/upload/invoices")
    async def onboarding_upload_invoices(request: Request):
        return RedirectResponse("/docs/import", status_code=302)

    @app.get("/onboarding/upload/cif")
    async def onboarding_upload_cif(request: Request):
        return RedirectResponse("/onboarding", status_code=302)

    # ── Company switcher (HTMX partial) ─────────────────────────────────────

    @app.get("/switch-company")
    async def switch_company_picker(request: Request):
        """HTMX: render company picker dropdown panel."""
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return P(t("auth.not_authenticated"), cls="cell-error")
        try:
            companies_resp = await api_my_companies(token)
            companies = companies_resp.get("items", []) if isinstance(companies_resp, dict) else companies_resp
        except APIError as e:
            return Div(P(f"Error: {e.detail}", cls="cell-error"), cls="company-picker")
        return _company_picker_panel(companies)

    @app.post("/switch-company/{company_id}")
    async def do_switch_company(request: Request, company_id: str):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        from ui.api_client import switch_company as api_switch
        try:
            new_token = await api_switch(token, company_id)
        except APIError as e:
            return RedirectResponse(f"/?error={e.detail}", status_code=302)
        resp = RedirectResponse("/", status_code=302)
        # switch_company only returns access_token; keep existing refresh token
        resp.set_cookie(COOKIE_NAME, new_token, httponly=True, samesite="lax", max_age=900, secure=_settings.cookie_secure, domain=cookie_domain(request))
        return resp

    @app.post("/create-company")
    async def create_company_ui(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        company_name = str(form.get("company_name", "")).strip()
        if not company_name:
            return RedirectResponse("/?error=Company+name+required", status_code=302)
        from ui.api_client import create_company as api_create
        try:
            new_token = await api_create(token, company_name)
        except APIError as e:
            return RedirectResponse(f"/?error={e.detail}", status_code=302)
        resp = RedirectResponse("/setup/company", status_code=302)
        resp.set_cookie(COOKIE_NAME, new_token, httponly=True, samesite="lax", max_age=900, secure=_settings.cookie_secure, domain=cookie_domain(request))
        return resp

    # ── Logout ───────────────────────────────────────────────────────────────

    @app.post("/logout")
    async def logout(request: Request):
        resp = RedirectResponse("/login", status_code=302)
        _clear_tokens(resp)
        return resp

    @app.get("/logout")
    async def logout_get(request: Request):
        """GET fallback for no-JS clients. Clears tokens and redirects."""
        resp = RedirectResponse("/login", status_code=302)
        _clear_tokens(resp)
        return resp

    @app.get("/health/system")
    async def health_system_proxy():
        """Proxy /health/system to the API so the UI health banner works on any port."""
        from starlette.responses import JSONResponse
        import httpx
        try:
            async with httpx.AsyncClient(base_url=API_BASE, timeout=3.0) as c:
                r = await c.get("/health/system")
                return JSONResponse(r.json(), status_code=r.status_code)
        except Exception:
            return JSONResponse({"overall": "degraded", "api": "unreachable"}, status_code=503)

    # ── Password reset ───────────────────────────────────────────────────────

    @app.get("/forgot-password")
    async def forgot_password_page(request: Request):
        has_email = bool(_settings.gateway_token or _settings.smtp_host)
        return auth_shell(
            _forgot_password_form() if has_email else _forgot_password_cli(),
            title="Forgot password - Celerp",
        )

    @app.post("/forgot-password")
    async def forgot_password_submit(request: Request):
        form = await request.form()
        email = str(form.get("email", "")).strip()
        import httpx
        try:
            async with httpx.AsyncClient(base_url=API_BASE, timeout=5.0) as c:
                await c.post("/auth/password-reset/request", json={"email": email})
        except Exception:
            pass
        return auth_shell(
            _forgot_password_sent(),
            title="Forgot password - Celerp",
        )

    @app.get("/reset-password")
    async def reset_password_page(request: Request):
        token = request.query_params.get("token", "")
        return auth_shell(_reset_password_form(token=token), title="Reset password - Celerp")

    @app.post("/reset-password")
    async def reset_password_submit(request: Request):
        form = await request.form()
        token = str(form.get("token", ""))
        new_password = str(form.get("new_password", ""))
        confirm = str(form.get("confirm_password", ""))
        if new_password != confirm:
            return auth_shell(_reset_password_form(token=token, error="Passwords do not match"), title="Reset password - Celerp")
        import httpx
        try:
            async with httpx.AsyncClient(base_url=API_BASE, timeout=5.0) as c:
                r = await c.post("/auth/password-reset/confirm", json={"token": token, "new_password": new_password})
            if r.status_code == 200:
                return RedirectResponse("/login", status_code=302)
            detail = r.json().get("detail", "Reset failed")
            return auth_shell(_reset_password_form(token=token, error=detail), title="Reset password - Celerp")
        except Exception as e:
            return auth_shell(_reset_password_form(token=token, error=f"Server error: {e}"), title="Reset password - Celerp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_tokens(resp, access_token: str, refresh_token: str, request=None) -> None:
    domain = cookie_domain(request) if request is not None else None
    resp.set_cookie(COOKIE_NAME, access_token, httponly=True, samesite="lax", max_age=900, secure=_settings.cookie_secure, domain=domain)
    resp.set_cookie(REFRESH_COOKIE_NAME, refresh_token, httponly=True, samesite="lax", max_age=86400 * 30, secure=_settings.cookie_secure, domain=domain)


def _clear_tokens(resp) -> None:
    resp.delete_cookie(COOKIE_NAME)
    resp.delete_cookie(REFRESH_COOKIE_NAME)


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def _login_form(email: str = "", error: str | None = None, notice: str = "") -> FT:
    lang = "en"
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("page.sign_in_to_celerp"), cls="auth-title"),
            cls="auth-header",
        ),
        notice,
        Form(
            flash(error) if error else "",
            Div(Label(t("label.email", lang), For="email", cls="form-label"),
                Input(type="email", id="email", name="email", value=email,
                      placeholder="you@company.com", required=True, autofocus=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.password", lang), For="password", cls="form-label"),
                Input(type="password", id="password", name="password",
                      placeholder="••••••••", required=True, cls="form-input"),
                cls="form-group"),
            Button(t("btn.sign_in", lang), type="submit", cls="btn btn--primary btn--full"),
            P(A(t("auth.forgot_password"), href="/forgot-password", cls="auth-link"), cls="auth-footer-text"),
            method="post", action="/login", cls="auth-form",
        ),
        cls="auth-card",
    )


def _setup_form(
    company_name: str = "", name: str = "", email: str = "", error: str | None = None
) -> FT:
    lang = "en"
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("page.set_up_your_workspace"), cls="auth-title"),
            P(t("msg.you_are_first_admin", lang), cls="auth-subtitle"),
            cls="auth-header",
        ),
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
                      placeholder="you@company.com", required=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.password", lang), For="password", cls="form-label"),
                Input(type="password", id="password", name="password",
                      placeholder="Min 8 characters", required=True, cls="form-input"),
                cls="form-group"),
            Div(Label(t("label.confirm_password", lang), For="confirm_password", cls="form-label"),
                Input(type="password", id="confirm_password", name="confirm_password",
                      placeholder="••••••••", required=True, cls="form-input"),
                cls="form-group"),
            Button(t("btn.create_workspace", lang), type="submit", cls="btn btn--primary btn--full"),
            method="post", action="/setup", cls="auth-form",
        ),
        cls="auth-card",
    )


def _onboarding_view() -> FT:
    integrations = [
        ("/onboarding/upload/items", "Import Inventory", "Upload CSV or JSON", "items"),
        ("/onboarding/upload/contacts", "Import Customers", "Upload CSV or connect CRM", "crm"),
        ("/onboarding/upload/invoices", "Import Invoices", "Historical sales data", "docs"),
        ("/onboarding/upload/cif", "Import from CIF", "Celerp Import Format bundle", "cif"),
    ]
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("page.welcome_lets_load_your_data"), cls="auth-title"),
            P(t("msg.onboarding_subtitle"), cls="auth-subtitle"),
            cls="auth-header",
        ),
        Div(
            *[
                A(
                    Strong(label),
                    P(desc, cls="quick-link-desc"),
                    href=href,
                    cls="quick-link-card",
                )
                for href, label, desc, _ in integrations
            ],
            cls="quick-links-grid",
        ),
        Div(
            P(t("msg.onboarding_skip"), cls="auth-subtitle"),
            A(t("btn.go_to_dashboard"), href="/dashboard", cls="btn btn--secondary"),
            cls="mt-lg text-center",
        ),
        cls="onboarding-card",
    )


def _direct_connection_gate(email: str, password: str) -> FT:
    """Shown when a second user tries to log in without relay connected."""
    subscribe_url = "https://celerp.com/subscribe"
    try:
        from celerp.config import ensure_instance_id
        subscribe_url += f"?instance_id={ensure_instance_id()}#cloud"
    except Exception:
        pass

    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H2("Direct connections are one at a time",
               style="font-size:18px;"),
            P(
                "Direct connections can only serve one authenticated user at a time. "
                "If you require simultaneous multiple user access, Celerp Cloud can "
                "route your connections through a persistent relay allowing any number "
                "of users to access the system simultaneously.",
                cls="auth-subtitle",
                style="text-align:left;",
            ),
            Div(
                A("Get Celerp Cloud - USD $29/mo",
                  href=subscribe_url, target="_blank",
                  cls="btn btn--primary"),
                Form(
                    Input(type="hidden", name="email", value=email),
                    Input(type="hidden", name="password", value=password),
                    Button("Continue (sign out the other user)",
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
    new_company_form = Form(
        Input(
            type="text", name="company_name", placeholder="Company name",
            required=True, cls="form-input picker-new-input",
        ),
        Button(t("btn._create"), type="submit", cls="btn btn--primary btn--sm"),
        method="post", action="/create-company", cls="company-picker-new",
    )
    return Div(*company_items, Hr(cls="picker-divider"), new_company_form, cls="company-picker")


def _forgot_password_form(error: str | None = None) -> FT:
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("auth.forgot_password"), cls="auth-title"),
            P(t("auth.enter_your_email_and_well_send_a_reset_link"), cls="auth-subtitle"),
            cls="auth-header",
        ),
        Form(
            flash(error) if error else "",
            Div(Label(t("th.email"), For="email", cls="form-label"),
                Input(type="email", id="email", name="email",
                      placeholder="you@company.com", required=True, autofocus=True, cls="form-input"),
                cls="form-group"),
            Button(t("btn.send_reset_link"), type="submit", cls="btn btn--primary btn--full"),
            P(A(t("auth.back_to_login"), href="/login", cls="auth-link"), cls="auth-footer-text"),
            method="post", action="/forgot-password", cls="auth-form",
        ),
        cls="auth-card",
    )


def _forgot_password_sent() -> FT:
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("page.check_your_email"), cls="auth-title"),
            P(t("auth.if_that_email_exists_youll_receive_a_reset_link_sh"), cls="auth-subtitle"),
            cls="auth-header",
        ),
        Div(
            A(t("auth.back_to_login"), href="/login", cls="btn btn--primary"),
            cls="text-center mt-md",
        ),
        cls="auth-card",
    )


def _forgot_password_cli() -> FT:
    """Forgot-password page for self-hosted installs without email transport."""
    from celerp.config import ensure_instance_id
    iid = ensure_instance_id()
    subscribe_url = f"https://celerp.com/subscribe?instance_id={iid}#cloud"
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("page.reset_your_password"), cls="auth-title"),
            P(t("auth.open_your_terminal_and_run"), cls="auth-subtitle"),
            cls="auth-header",
        ),
        Div(
            Pre(
                Code("celerp reset-password --email you@company.com"),
                cls="auth-code-block",
            ),
            P(t("auth.youll_be_prompted_to_enter_a_new_password"), cls="auth-hint"),
            cls="auth-cli-section",
        ),
        Hr(cls="auth-divider"),
        Div(
            H3(t("page.want_emailbased_password_resets"), cls="auth-upsell-title"),
            P(
                "Celerp Web Access gives you a secure public URL, email workflows, "
                "automatic backups, and more. Your data stays on your machine - "
                "we just relay the connection.",
                cls="auth-upsell-text",
            ),
            A(t("auth.subscribe_for_29mo_u2192"), href=subscribe_url, target="_blank",
              cls="btn btn--accent btn--full"),
            cls="auth-upsell",
        ),
        P(A(t("auth.back_to_login"), href="/login", cls="auth-link"), cls="auth-footer-text"),
        cls="auth-card",
    )


def _reset_password_form(token: str = "", error: str | None = None) -> FT:
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("page.reset_your_password"), cls="auth-title"),
            P(t("auth.enter_your_new_password_below"), cls="auth-subtitle"),
            cls="auth-header",
        ),
        Form(
            flash(error) if error else "",
            Input(type="hidden", name="token", value=token),
            Div(Label(t("label.new_password"), For="new_password", cls="form-label"),
                Input(type="password", id="new_password", name="new_password",
                      placeholder="Min 8 characters", required=True, autofocus=True, cls="form-input"),
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
