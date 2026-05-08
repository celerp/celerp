# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Celerp UI entrypoint.

Run:
    cd core && PYTHONPATH=. uvicorn ui.app:app --reload --port 8080
"""

from __future__ import annotations

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Suppress noisy SSE access log lines.
# Patch logging.Logger.handle directly on the base class - survives any
# logger reconfiguration uvicorn does after app import.
_SSE_SUPPRESSED = frozenset(["/notifications/stream"])
_orig_logger_handle = logging.Logger.handle

def _filtered_logger_handle(self, record):
    if self.name == "uvicorn.access":
        try:
            if any(p in record.getMessage() for p in _SSE_SUPPRESSED):
                return
        except Exception:
            pass
    _orig_logger_handle(self, record)

logging.Logger.handle = _filtered_logger_handle

from fasthtml.common import FastHTML, Beforeware, RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

from ui.config import COOKIE_NAME, REFRESH_COOKIE_NAME, cookie_domain
from ui.routes import (
    auth, setup, search, settings, settings_import,
    settings_general, settings_sales, settings_purchasing, settings_inventory, settings_accounting,
    settings_contacts, settings_cloud, settings_connectors, notifications,
)
from fasthtml.common import *
from starlette.responses import HTMLResponse

# Public paths that don't require auth
_PUBLIC = {"/login", "/setup", "/logout", "/static", "/health", "/api/labels/preview"}

def _auth_guard(req: Request):
    """Redirect unauthenticated requests to login/setup before they reach any route."""
    path = req.url.path
    if any(path == p or path.startswith(p + "/") for p in _PUBLIC):
        return None
    if req.cookies.get(COOKIE_NAME):
        return None
    return RedirectResponse("/login", status_code=302)


def _token_needs_refresh(access_token: str) -> bool:
    """Return True if the access token has consumed more than half its lifetime.

    Decodes the JWT payload without signature verification (the API already
    verified it). If decoding fails for any reason, returns False (safe default:
    let the route handler deal with it).
    """
    import base64
    import json as _json
    import time

    try:
        # JWT: header.payload.signature — payload is base64url-encoded
        payload_b64 = access_token.split(".")[1]
        # Pad to a multiple of 4
        padding = 4 - len(payload_b64) % 4
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "=" * (padding % 4))
        claims = _json.loads(payload_bytes)
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        now = time.time()
        # iat is not always present; estimate iss time from settings TTL
        from celerp.config import settings as _settings
        total_ttl = int(_settings.access_token_expire_minutes) * 60
        issued_at = exp - total_ttl
        elapsed = now - issued_at
        return elapsed > total_ttl / 2
    except Exception:
        return False


class TokenRefreshMiddleware:
    """Pure ASGI sliding-window JWT refresh middleware.

    Two refresh scenarios handled on every authenticated request:

    1. Access token absent, refresh token present:
       Exchange immediately before the route handler runs (proactive).
       Patches the cookie header in-scope so the route sees a valid token.

    2. Access token present but past half its lifetime (sliding window):
       Exchange after the route handler returns. The route succeeds with the
       current token; the response silently carries fresh cookies.

    In both cases, new cookies are set on the response.
    """

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in _PUBLIC):
            await self._app(scope, receive, send)
            return

        access_token = request.cookies.get(COOKIE_NAME)
        refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
        new_access: str | None = None
        new_refresh: str | None = None

        # Case 1: no access token — try proactive exchange before route
        if not access_token and refresh_token:
            from ui.api_client import refresh_access_token, APIError as _APIError
            try:
                new_access, new_refresh = await refresh_access_token(refresh_token)
                existing = dict(request.cookies)
                existing[COOKIE_NAME] = new_access
                existing[REFRESH_COOKIE_NAME] = new_refresh
                cookie_header = "; ".join(f"{k}={v}" for k, v in existing.items())
                scope["headers"] = [
                    (k, v) for k, v in scope.get("headers", [])
                    if k.lower() != b"cookie"
                ] + [(b"cookie", cookie_header.encode())]
            except Exception:
                pass

        # Case 2: access token present but past half-life
        if not new_access and access_token and refresh_token and _token_needs_refresh(access_token):
            from ui.api_client import refresh_access_token, APIError as _APIError
            try:
                new_access, new_refresh = await refresh_access_token(refresh_token)
            except Exception:
                pass

        if new_access and new_refresh:
            from celerp.config import settings as _settings
            max_age = int(_settings.access_token_expire_minutes) * 60
            domain = cookie_domain(request)

            def _make_set_cookie(name, value, http_only, max_age_, secure, samesite):
                parts = [f"{name}={value}", f"Max-Age={max_age_}", f"SameSite={samesite}", "Path=/"]
                if http_only:
                    parts.append("HttpOnly")
                if secure:
                    parts.append("Secure")
                if domain:
                    parts.append(f"Domain={domain}")
                return "; ".join(parts)

            extra_cookies = [
                _make_set_cookie(COOKIE_NAME, new_access, True, max_age, _settings.cookie_secure, "lax"),
                _make_set_cookie(REFRESH_COOKIE_NAME, new_refresh, True, 86400 * 30, _settings.cookie_secure, "lax"),
            ]

            async def send_with_cookies(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    for cookie_val in extra_cookies:
                        headers.append((b"set-cookie", cookie_val.encode()))
                    message = {**message, "headers": headers}
                await send(message)

            await self._app(scope, receive, send_with_cookies)
        else:
            await self._app(scope, receive, send)


app = FastHTML(
    before=Beforeware(_auth_guard, skip=[r"/login", r"/setup.*", r"/logout", r"/static/.*", r"/health"]),
    secret_key=os.environ.get("JWT_SECRET", "dev-secret"),
)

app.add_middleware(TokenRefreshMiddleware)


# ── i18n middleware: set context language per request ───────────────────────────

class I18nMiddleware:
    """Pure ASGI middleware: sets context language per request."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from ui.i18n import get_lang, set_lang
            request = Request(scope, receive)
            set_lang(get_lang(request))
        await self._app(scope, receive, send)

app.add_middleware(I18nMiddleware)

# ── Error handlers ─────────────────────────────────────────────────────────────

async def ui_404_handler(request: Request, exc) -> HTMLResponse:
    from ui.components.shell import base_shell, page_header
    from fasthtml.common import A, Div, P
    page = base_shell(
        page_header("Page Not Found"),
        Div(
            P("The page you requested does not exist.", cls="flash flash--error"),
            A("← Go to Settings", href="/settings", cls="btn btn--primary"),
            cls="content-area",
        ),
        title="404 - Not Found",
    )
    from fasthtml.common import to_xml
    return HTMLResponse(to_xml(page), status_code=404)

app.exception_handlers[404] = ui_404_handler


async def ui_500_handler(request: Request, exc) -> HTMLResponse:
    import logging as _logging
    _logging.getLogger(__name__).error("UI 500: %s", exc, exc_info=True)
    from ui.components.shell import base_shell, page_header
    from fasthtml.common import A, Div, P
    page = base_shell(
        page_header("Something Went Wrong"),
        Div(
            P("An unexpected error occurred. Please try again.", cls="flash flash--error"),
            A("← Back to Dashboard", href="/dashboard", cls="btn btn--primary"),
            cls="content-area",
        ),
        title="500 - Server Error",
    )
    from fasthtml.common import to_xml
    return HTMLResponse(to_xml(page), status_code=500)

app.exception_handlers[500] = ui_500_handler


_static_dir = os.path.join(os.path.dirname(__file__), "static")

# Proxy /static/attachments/* to the API server (API and UI serve /static from different dirs)
@app.route("/static/attachments/{path:path}")
async def proxy_attachment(request: Request, path: str) -> Response:
    if not request.cookies.get(COOKIE_NAME):
        return RedirectResponse("/login", status_code=302)
    from ui.config import API_BASE
    import httpx
    url = f"{API_BASE}/static/attachments/{path}"
    async with httpx.AsyncClient() as c:
        r = await c.get(url)
    return Response(content=r.content, media_type=r.headers.get("content-type", "application/octet-stream"), status_code=r.status_code)

app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# In dev mode (MODULE_DIR not set), default_modules live next to the repo root.
# Add each default module package dir to sys.path so _CONDITIONAL_UI imports work.
# In production, the module loader (load_all) handles sys.path itself.
_DEFAULT_MODULES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "default_modules")
if not os.environ.get("MODULE_DIR") and os.path.isdir(_DEFAULT_MODULES_DIR):
    import sys as _sys
    for _dm in os.listdir(_DEFAULT_MODULES_DIR):
        _dm_path = os.path.join(_DEFAULT_MODULES_DIR, _dm)
        if os.path.isdir(_dm_path) and _dm_path not in _sys.path:
            _sys.path.insert(0, _dm_path)

# Determine enabled modules from env (set by cli.py _config_to_env).
# Fall back to config.toml when env is absent (e.g. Electron binary restart).
_ENABLED_MODULES: set[str] = set(
    m.strip() for m in os.environ.get("ENABLED_MODULES", "").split(",") if m.strip()
)
if not _ENABLED_MODULES and os.environ.get("MODULE_DIR"):
    try:
        from celerp.config import read_config as _read_config
        _cfg = _read_config()
        _ENABLED_MODULES = set(_cfg.get("modules", {}).get("enabled") or [])
    except Exception:
        pass

# Kernel UI routes — always registered
for mod in (auth, setup, search, settings, settings_import,
            settings_general, settings_sales, settings_purchasing, settings_inventory, settings_accounting,
            settings_contacts, settings_cloud, settings_connectors, notifications):
    mod.setup_routes(app)

# Module-conditional UI routes
# Import order matters: import/* routes must precede their parent /{entity_id} routes
_CONDITIONAL_UI: list[tuple[str, str]] = [
    # (backend_module_name, ui_route_module_dotted_path)
    ("celerp-docs",        "ui.routes.docs_import"),
    ("celerp-docs",        "ui.routes.lists_import"),
    ("celerp-accounting",  "ui.routes.accounting_import"),
    ("celerp-docs",        "ui.routes.documents"),
    # ui.routes.lists omitted: list routes are registered by ui.routes.documents
    ("celerp-labels",      "celerp_labels.ui_routes"),
    ("celerp-accounting",  "ui.routes.reconciliation"),
    ("celerp-dashboard",   "ui.routes.dashboard"),
]

import importlib as _importlib
for _backend_mod, _ui_mod_path in _CONDITIONAL_UI:
    if _backend_mod in _ENABLED_MODULES or not os.environ.get("MODULE_DIR"):
        try:
            _ui_mod = _importlib.import_module(_ui_mod_path)
            _ui_mod.setup_routes(app)
        except ImportError:
            pass  # UI route module not present — skip silently

# Register UI routes from external loaded modules (opt-in: no-op if MODULE_DIR not set)
_MODULE_DIR = os.environ.get("MODULE_DIR", "")
if _MODULE_DIR and _ENABLED_MODULES:
    from celerp.modules.loader import load_all, register_ui_routes
    _ui_loaded = load_all(_MODULE_DIR, _ENABLED_MODULES)
    register_ui_routes(app, _ui_loaded)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ui.app:app", host="0.0.0.0", port=8080, reload=True)
