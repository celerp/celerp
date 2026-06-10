# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Noah Severs. All rights reserved.
"""
Browser test conftest.
Starts FastAPI (port 18000) + FastHTML UI (port 18080) in background threads.
Seeds one admin user + company. Provides a Playwright browser context with auth cookie.

Run: pytest tests/test_browser/ -m browser --tb=short
Skip from default suite: pytest -m "not browser"
"""
from __future__ import annotations

import os
import socket
import threading
import time

import httpx
import pytest

# Must set env vars BEFORE importing any celerp modules (they read on import).
# DATABASE_URL is provided by the root conftest (Postgres via testcontainers or a
# preset URL); the browser servers run against that same database.
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("MODULE_DIR", "default_modules,premium_modules")
_ALL_MODULES = (
    "celerp-accounting,celerp-ai,celerp-connectors,celerp-contacts,celerp-sales-funnel,celerp-dashboard,celerp-docs,celerp-inventory,"
    "celerp-labels,celerp-manufacturing,celerp-reports,celerp-subscriptions,celerp-verticals"
)
os.environ.setdefault("ENABLED_MODULES", _ALL_MODULES)

_API_PORT = 18000
_UI_PORT = 18080
_API_BASE = f"http://127.0.0.1:{_API_PORT}"
_UI_BASE = f"http://127.0.0.1:{_UI_PORT}"

_TEST_EMAIL = "browser_test@celerp.test"
_TEST_PASSWORD = "BrowserTest123!"
_TEST_COMPANY = "Browser Test Co"


# ── Port helpers ──────────────────────────────────────────────────────────────

def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Port {port} did not open within {timeout}s")


def _is_port_free(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return False
    except OSError:
        return True


def _reset_pg_database() -> None:
    """Drop + recreate the public schema so the browser session starts clean.
    The app rebuilds all tables via create_all when the server boots."""
    import psycopg2
    from urllib.parse import urlsplit

    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        conn.close()


# ── Server fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def api_server():
    """Start FastAPI on port 18000 against the (Postgres) test database."""
    if not _is_port_free(_API_PORT):
        # Already running (e.g. re-run within same process) - skip restart
        yield _API_BASE
        return

    import uvicorn
    # Start from a clean schema; the app rebuilds tables on startup (create_all).
    _reset_pg_database()

    from celerp.main import app
    config = uvicorn.Config(app, host="127.0.0.1", port=_API_PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port(_API_PORT)
    yield _API_BASE
    server.should_exit = True
    thread.join(timeout=3)


@pytest.fixture(scope="session")
def ui_server(api_server):
    """Start FastHTML UI on port 18080 pointing at the API server."""
    if not _is_port_free(_UI_PORT):
        yield _UI_BASE
        return

    import uvicorn
    os.environ["API_URL"] = api_server
    os.environ["CELERP_API_URL"] = api_server

    # Patch API_BASE in-place so all existing route module references pick up the
    # correct test URL without a reload (reload would create a new APIError class,
    # breaking "except APIError" in already-imported route modules).
    import ui.config as ui_cfg
    ui_cfg.API_BASE = api_server
    import ui.api_client as ui_ac
    ui_ac.API_BASE = api_server

    from ui.app import app as ui_app

    # The root conftest imports ui.app before MODULE_DIR is set, so
    # module-level load_all / register_ui_routes runs with MODULE_DIR=""
    # and skips external modules. Re-register any missing module UI routes.
    # Use absolute paths so load_all resolves correctly regardless of cwd.
    from celerp.modules.loader import load_all, register_ui_routes
    import pathlib as _pl
    _repo_root = _pl.Path(__file__).resolve().parents[2]
    _abs_module_dirs = ",".join(
        str(_repo_root / d.strip())
        for d in os.environ.get("MODULE_DIR", "default_modules").split(",")
        if d.strip()
    )
    _enabled = {m.strip() for m in os.environ.get("ENABLED_MODULES", "").split(",") if m.strip()}
    if _abs_module_dirs and _enabled:
        _loaded = load_all(_abs_module_dirs, _enabled)
        register_ui_routes(ui_app, _loaded)

    config = uvicorn.Config(ui_app, host="127.0.0.1", port=_UI_PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port(_UI_PORT)
    yield _UI_BASE
    server.should_exit = True
    thread.join(timeout=3)


# ── User + company seed ───────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def seeded_user(api_server):
    """
    Register an admin user + seed a company via the API.
    Returns {"email": str, "password": str, "access_token": str}.
    """
    with httpx.Client(base_url=api_server, timeout=10) as client:
        # Bootstrap the app first (creates DB tables)
        r = client.get("/health")
        assert r.status_code == 200, f"API not healthy: {r.text}"

        # Register user
        r = client.post("/auth/register", json={
            "email": _TEST_EMAIL,
            "password": _TEST_PASSWORD,
            "name": "Browser Test User",
            "company_name": _TEST_COMPANY,
        })
        if r.status_code in (403, 409):
            # Already registered / bootstrapped from a previous run (port reused)
            r = client.post("/auth/login", json={
                "email": _TEST_EMAIL,
                "password": _TEST_PASSWORD,
            })
        assert r.status_code == 200, f"Auth failed: {r.text}"
        data = r.json()
        access_token = data["access_token"]

    return {
        "email": _TEST_EMAIL,
        "password": _TEST_PASSWORD,
        "access_token": access_token,
    }


# ── Playwright fixtures ────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def browser_context(playwright, ui_server, seeded_user):
    """Chromium session context with auth cookie pre-set."""
    browser = playwright.chromium.launch(headless=True)
    ctx = browser.new_context(base_url=ui_server)
    ctx.add_cookies([{
        "name": "celerp_token",
        "value": seeded_user["access_token"],
        "domain": "127.0.0.1",
        "path": "/",
    }])
    yield ctx
    browser.close()


@pytest.fixture
def page(browser_context):
    """Fresh page per test. Closes after test."""
    p = browser_context.new_page()
    yield p
    if not p.is_closed():
        p.close()


@pytest.fixture(scope="session")
def unauthed_context(playwright, ui_server):
    """Browser context WITHOUT any auth cookie (for auth-wall tests)."""
    browser = playwright.chromium.launch(headless=True)
    ctx = browser.new_context(base_url=ui_server)
    yield ctx
    browser.close()


@pytest.fixture
def unauthed_page(unauthed_context):
    p = unauthed_context.new_page()
    yield p
    if not p.is_closed():
        p.close()


# ── Failure screenshot hook ────────────────────────────────────────────────────

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" and rep.failed:
        page = item.funcargs.get("page") or item.funcargs.get("unauthed_page")
        if page and not page.is_closed():
            import pathlib
            fail_dir = pathlib.Path("/tmp/playwright_failures")
            fail_dir.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(fail_dir / f"{item.name}.png"))
            except Exception:
                pass


# ── API helper (for seeding in tests) ────────────────────────────────────────

@pytest.fixture(scope="session")
def api(api_server, seeded_user):
    """Synchronous httpx client pre-authed against the API."""
    headers = {"Authorization": f"Bearer {seeded_user['access_token']}"}
    with httpx.Client(base_url=api_server, headers=headers, timeout=10) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def _reset_event_loop_after_playwright():
    """Playwright's sync API creates and closes its own asyncio event loop.

    After pw.stop() closes that loop, the asyncio policy still references a
    closed/dead loop. Any subsequent pytest-asyncio fixtures then fail with
    'Runner.run() cannot be called from a running event loop'.

    This fixture yields first (so all browser tests run), then installs a
    fresh event loop so unit tests that run after browser tests get a clean
    slate. We do NOT take ``playwright`` as a dep - that would tear down
    before playwright stops, still leaving a closed loop.
    """
    import asyncio
    yield
    # All browser tests finished. Install a new loop so pytest-asyncio can
    # create its own runner for any tests that follow.
    asyncio.get_event_loop_policy().set_event_loop(asyncio.new_event_loop())
