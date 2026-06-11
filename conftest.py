# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import os

# Must be set before celerp.config is imported (JWT guard fires at module load).
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

# ── Postgres for the whole test suite ──────────────────────────────────────────
# Both production targets (the server and the Electron embedded-postgres build)
# run Postgres, so the tests run on Postgres too. A pre-set DATABASE_URL (e.g. a
# CI services container) is honored; otherwise we start a throwaway Postgres via
# testcontainers (Docker), tuned for throwaway speed (fsync/synchronous_commit/
# full_page_writes off — safe because the data is discarded). Each pytest-xdist
# worker gets its own database. MUST run before celerp.db / test_helpers import.
_PG_CONTAINER = None


def _provision_test_database() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql"):
        worker = os.environ.get("PYTEST_XDIST_WORKER")
        if worker:
            url = _create_worker_db(url, worker)
    else:
        from testcontainers.postgres import PostgresContainer
        global _PG_CONTAINER
        # Throwaway DB → turn off durability guards for speed (data is discarded).
        _PG_CONTAINER = PostgresContainer("postgres:16-alpine").with_command(
            "-c fsync=off -c synchronous_commit=off -c full_page_writes=off")
        _PG_CONTAINER.start()
        host = _PG_CONTAINER.get_container_host_ip()
        port = _PG_CONTAINER.get_exposed_port(5432)
        url = (f"postgresql+asyncpg://{_PG_CONTAINER.username}:{_PG_CONTAINER.password}"
               f"@{host}:{port}/{_PG_CONTAINER.dbname}")
    os.environ["DATABASE_URL"] = url
    if url.startswith("postgresql"):
        # Make the app's own engine (celerp.db.engine) use NullPool too: a pooled
        # asyncpg connection is bound to the event loop that created it, but
        # pytest-asyncio gives each test a fresh loop — so a pooled connection
        # reused/closed in a later test's loop raises "attached to a different
        # loop" / "Event loop is closed". NullPool opens+closes a connection per
        # use within the current loop, sidestepping it entirely.
        os.environ["CELERP_TEST_NULLPOOL"] = "1"


def _create_worker_db(url: str, worker: str) -> str:
    """CREATE DATABASE <base>_<worker> on the shared server; return its asyncpg URL."""
    import re
    from urllib.parse import urlsplit, urlunsplit
    import psycopg2

    parts = urlsplit(url.replace("+asyncpg", ""))
    base_db = parts.path.lstrip("/") or "postgres"
    worker_db = f"{base_db}_{re.sub(r'[^a-zA-Z0-9]', '', worker)}"
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=base_db)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (worker_db,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{worker_db}"')
    finally:
        conn.close()
    return urlunsplit(parts._replace(path=f"/{worker_db}")).replace(
        "postgresql://", "postgresql+asyncpg://")


_provision_test_database()

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker


def pytest_unconfigure(config):
    """Stop the throwaway Postgres container at the end of the session."""
    global _PG_CONTAINER
    if _PG_CONTAINER is not None:
        try:
            _PG_CONTAINER.stop()
        finally:
            _PG_CONTAINER = None

from celerp.db import get_session
from celerp.main import app
from ui.app import app as _ui_app

import sys as _sys, os as _os
from pathlib import Path

from test_helpers import REPO_ROOT, DATABASE_URL, make_test_token, authed_cookies, _crm_available  # noqa: F401

# Register inventory module routes onto the test app.
_inv_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-inventory")
if _os.path.abspath(_inv_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_inv_src))
from celerp_inventory.routes import setup_api_routes as _setup_inv
from celerp_inventory.ui_routes import setup_ui_routes as _setup_inv_ui
_setup_inv(app)
_setup_inv_ui(_ui_app)

# Register Contacts module routes onto the test app.
_contacts_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-contacts")
if _os.path.abspath(_contacts_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_contacts_src))
from celerp_contacts.routes import setup_api_routes as _setup_contacts
from celerp_contacts.ui_routes import setup_ui_routes as _setup_contacts_ui
_setup_contacts(app)
_setup_contacts_ui(_ui_app)

# Register sales-funnel (deals) module routes onto the test app (if available).
_crm_src = _os.path.join(_os.path.dirname(__file__), "premium_modules", "celerp-sales-funnel")
_crm_available = _os.path.isfile(_os.path.join(_crm_src, "celerp_sales_funnel", "__init__.py"))
if _crm_available:
    if _os.path.abspath(_crm_src) not in [_os.path.abspath(p) for p in _sys.path]:
        _sys.path.insert(0, _os.path.abspath(_crm_src))
    from celerp_sales_funnel.routes import setup_api_routes as _setup_crm
    from celerp_sales_funnel.ui_routes import setup_ui_routes as _setup_crm_ui
    _setup_crm(app)
    _setup_crm_ui(_ui_app)

# Register manufacturing module routes + projection handler onto the test app.
_mfg_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-manufacturing")
if _os.path.abspath(_mfg_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_mfg_src))
from celerp_manufacturing.routes import setup_api_routes as _setup_mfg
from celerp_manufacturing.ui_routes import setup_ui_routes as _setup_mfg_ui
_setup_mfg(app)
_setup_mfg_ui(_ui_app)

# Register connectors module routes onto the test app.
_conn_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-connectors")
if _os.path.abspath(_conn_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_conn_src))
from celerp_connectors.routes import setup_api_routes as _setup_connectors
_setup_connectors(app)

# Register docs module routes onto the test app.
_docs_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-docs")
if _os.path.abspath(_docs_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_docs_src))
from celerp_docs.api_setup import setup_api_routes as _setup_docs
from celerp_docs.ui_routes import setup_ui_routes as _setup_docs_ui
_setup_docs(app)
_setup_docs_ui(_ui_app)

# Register accounting module routes onto the test app.
_acc_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-accounting")
if _os.path.abspath(_acc_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_acc_src))
from celerp_accounting.api_setup import setup_api_routes as _setup_accounting
from celerp_accounting.ui_routes import setup_ui_routes as _setup_accounting_ui
_setup_accounting(app)
_setup_accounting_ui(_ui_app)

# Register reconciliation UI routes onto the test app.
from ui.routes.reconciliation import setup_routes as _setup_recon_ui
_setup_recon_ui(_ui_app)

# Register subscriptions module routes onto the test app.
_subs_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-subscriptions")
if _os.path.abspath(_subs_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_subs_src))
from celerp_subscriptions.routes import setup_api_routes as _setup_subs
from celerp_subscriptions.ui_routes import setup_ui_routes as _setup_subs_ui
_setup_subs(app)
_setup_subs_ui(_ui_app)

# Register reports module routes onto the test app.
_rep_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-reports")
if _os.path.abspath(_rep_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_rep_src))
from celerp_reports.api_setup import setup_api_routes as _setup_reports
from celerp_reports.ui_routes import setup_ui_routes as _setup_reports_ui
_setup_reports(app)
_setup_reports_ui(_ui_app)

# Register verticals module routes onto the test app.
_vert_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-verticals")
if _os.path.abspath(_vert_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_vert_src))
from celerp_verticals.routes import setup_api_routes as _setup_verticals
_setup_verticals(app)

# Register dashboard module routes onto the test app.
_dash_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-dashboard")
if _os.path.abspath(_dash_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_dash_src))
from celerp_dashboard.setup import setup_api_routes as _setup_dashboard
_setup_dashboard(app)

# Register AI module routes onto the test app.
_ai_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-ai")
if _os.path.abspath(_ai_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_ai_src))
from celerp_ai.setup import setup_api_routes as _setup_ai
_setup_ai(app)

# Register backup module routes onto the test app.
_backup_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-backup")
if _os.path.abspath(_backup_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_backup_src))
from celerp_backup.setup import setup_api_routes as _setup_backup
_setup_backup(app)

# Register admin module routes onto the test app.
_admin_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-admin")
if _os.path.abspath(_admin_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_admin_src))
from celerp_admin.setup import setup_api_routes as _setup_admin
_setup_admin(app)

# Register labels module routes onto the test app.
_labels_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-labels")
if _os.path.abspath(_labels_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_labels_src))
from celerp_labels.routes import setup_api_routes as _setup_labels
_setup_labels(app)

# Register warehousing module path so its UI components can be imported in tests.
_wh_src = _os.path.join(_os.path.dirname(__file__), "premium_modules", "celerp-warehousing")
_wh_available = _os.path.isfile(_os.path.join(_wh_src, "celerp_warehousing", "__init__.py"))
if _wh_available:
    if _os.path.abspath(_wh_src) not in [_os.path.abspath(p) for p in _sys.path]:
        _sys.path.insert(0, _os.path.abspath(_wh_src))
    # Register warehousing API routes onto the test app.
    from celerp_warehousing.routes import router as _wh_router
    app.include_router(_wh_router, prefix="/warehousing", tags=["warehousing"])

_SLOT_CONTRIBUTIONS = [
    # --- nav slots (mirrors what modules register at load time) ---
    {"slot": "nav", "contrib": {"group": None, "key": "dashboard", "href": "/dashboard", "label": "Dashboard", "order": 1, "_module": "celerp-dashboard"}},
    {"slot": "nav", "contrib": {"group": "Sales Documents", "key": "docs", "href": "/docs", "label": "Documents", "order": 20, "_module": "celerp-docs"}},
    {"slot": "nav", "contrib": {"group": "Sales Documents", "key": "lists", "href": "/lists", "label": "Lists", "order": 21, "_module": "celerp-docs"}},
    {"slot": "nav", "contrib": {"group": "Subscriptions", "key": "subscriptions_sales", "href": "/subscriptions?direction=sales", "label": "Sales Subscriptions", "label_key": "nav.subscriptions_sales", "order": 25, "min_role": "operator", "_module": "celerp-subscriptions"}},
    {"slot": "nav", "contrib": {"group": "Subscriptions", "key": "subscriptions_purchasing", "href": "/subscriptions?direction=purchasing", "label": "Purchasing Subscriptions", "label_key": "nav.subscriptions_purchasing", "order": 26, "min_role": "operator", "_module": "celerp-subscriptions"}},
    {"slot": "nav", "contrib": {"group": "Inventory", "key": "inventory", "href": "/inventory", "label": "Inventory", "order": 30, "settings_href": "/settings/inventory", "_module": "celerp-inventory"}},
    {"slot": "nav", "contrib": {"group": "Inventory", "key": "inventory_sold", "href": "/inventory?status=sold", "label": "Sold Inventory", "order": 31, "_module": "celerp-inventory"}},
    {"slot": "nav", "contrib": {"group": "Inventory", "key": "inventory_archived", "href": "/inventory?status=archived", "label": "Archived Inventory", "order": 32, "_module": "celerp-inventory"}},
    {"slot": "nav", "contrib": {"group": "Inventory", "key": "scanning", "href": "/scanning", "label": "Scanning", "order": 33, "_module": "celerp-inventory"}},
    {"slot": "nav", "contrib": {"group": "Finance", "key": "accounting", "href": "/accounting", "label": "Accounting", "order": 50, "settings_href": "/settings/accounting", "_module": "celerp-accounting"}},
    {"slot": "nav", "contrib": {"group": "Finance", "key": "reconcile", "href": "/accounting/reconcile/start", "label": "Reconcile", "order": 52, "_module": "celerp-accounting"}},
    {"slot": "nav", "contrib": {"group": "Finance", "key": "reports", "href": "/reports", "label": "Reports", "order": 53, "_module": "celerp-reports"}},
    {"slot": "nav", "contrib": {"group": "Inventory", "key": "manufacturing", "href": "/manufacturing", "label": "Manufacturing", "order": 40, "_module": "celerp-manufacturing"}},
    # --- projection_handler slots ---
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "doc.",
            "handler": "celerp_docs.doc_projections:apply_documents_event",
            "_module": "celerp-docs",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "list.",
            "handler": "celerp_docs.doc_projections:apply_documents_event",
            "_module": "celerp-docs",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "item.",
            "handler": "celerp_inventory.projections:apply_item_event",
            "_module": "celerp-inventory",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "crm.contact.",
            "handler": "celerp_contacts.projections:apply_contact_event",
            "_module": "celerp-contacts",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "acc.",
            "handler": "celerp_accounting.projections:apply_accounting_event",
            "_module": "celerp-accounting",
        },
    },
    {
        "slot": "on_company_created",
        "contrib": {
            "handler": "celerp_accounting.routes:seed_chart_of_accounts_hook",
            "_module": "celerp-accounting",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "mfg.",
            "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event",
            "_module": "celerp-manufacturing",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "bom.",
            "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event",
            "_module": "celerp-manufacturing",
        },
    },
]

# If celerp-sales-funnel is installed, add the deal projection handler.
if _crm_available:
    _SLOT_CONTRIBUTIONS.append({
        "slot": "projection_handler",
        "contrib": {
            "prefix": "crm.deal.",
            "handler": "celerp_sales_funnel.projections:apply_deal_event",
            "_module": "celerp-sales-funnel",
        },
    })


def _ensure_slots() -> None:
    """Ensure all module projection handler slots are registered.

    Called before every test so that tests which call slots.clear() don't
    leave the projection engine unable to handle events.
    """
    from celerp.modules.slots import get, register
    for entry in _SLOT_CONTRIBUTIONS:
        slot = entry["slot"]
        contrib = entry["contrib"]
        registered = get(slot)
        dedup_key = contrib.get("prefix") or contrib.get("href")
        existing_keys = {c.get("prefix") or c.get("href") for c in registered}
        if dedup_key not in existing_keys:
            register(slot, contrib)


@pytest.fixture(autouse=True)
def _reset_hot_path_caches():
    """Bust the in-process nonce and drain caches before each test.

    These module-level caches are correct at runtime (single process, explicit
    bust on mutation).  In tests, each test rolls back its database changes, so
    the cache must be cleared to avoid leaking state between tests.
    """
    from celerp.services.session_tracker import _nonce_cache_bust_all
    from celerp.services.runtime_state import _drain_cache_bust
    _nonce_cache_bust_all()
    _drain_cache_bust()
    yield
    _nonce_cache_bust_all()
    _drain_cache_bust()


@pytest.fixture(autouse=True)
def _disable_rate_limits():
    from celerp.routers.auth import limiter as auth_limiter

    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    auth_limiter.enabled = False
    auth_limiter._storage.reset()
    yield
    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    auth_limiter.enabled = False
    auth_limiter._storage.reset()


@pytest.fixture(autouse=True)
def _ensure_slot_registration():
    """Re-register module projection handler slots before each test."""
    _ensure_slots()
    yield
    _ensure_slots()


@pytest.fixture(autouse=True)
def _mock_get_modules_default():
    """Default get_modules mock — returns empty list so settings page always has a valid response."""
    from unittest.mock import patch, AsyncMock
    with patch("ui.api_client.get_modules", new=AsyncMock(return_value=[])):
        yield


@pytest.fixture(autouse=True)
def _reset_loaded_modules(request):
    """Clear the loader's in-process registry around each unit test. A test (or
    a test module's import) that calls load_all() populates
    celerp.modules.loader._loaded; without this it leaks into later tests in the
    same worker (e.g. a module shows running=True), which surfaces under xdist's
    test distribution.

    Browser tests are exempt: their server runs in-process and legitimately owns
    a populated _loaded (module-gated UI like the credit-note "Receive Returns"
    button reads loaded_modules()), so wiping it would hide that UI."""
    if request.node.get_closest_marker("browser"):
        yield
        return
    from celerp.modules.loader import _loaded
    _loaded.clear()
    yield
    _loaded.clear()


from celerp.models.base import Base

# Session-scoped engine: created once, shared across all tests to avoid OOM from
# 1000+ engine create/dispose cycles when test_ui.py + test_routers/ run together.
@pytest_asyncio.fixture(scope="session")
async def _db_engine():
    # NullPool: a fresh asyncpg connection per use, so the session-scoped engine
    # can be driven from each test's own (function-scoped) event loop without
    # cross-loop "another operation in progress" errors.
    from sqlalchemy.pool import NullPool
    # lock_timeout: if a test ever poisons a connection and leaves a lock, the
    # per-test TRUNCATE fails fast instead of hanging for the whole pytest timeout.
    engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool,
        connect_args={"server_settings": {"lock_timeout": "10000"}})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(_db_engine) -> AsyncSession:
    """Per-test isolation by transaction rollback — nothing commits to disk. The
    test runs inside an outer transaction; the app's session.commit() calls become
    SAVEPOINT releases (join_transaction_mode='create_savepoint'); the outer
    transaction is rolled back at teardown."""
    conn = await _db_engine.connect()
    trans = await conn.begin()
    factory = async_sessionmaker(
        bind=conn, class_=AsyncSession, expire_on_commit=False,
        join_transaction_mode="create_savepoint")
    sess = factory()
    try:
        yield sess
    finally:
        await sess.close()
        if trans.is_active:
            await trans.rollback()
        await conn.close()


@pytest_asyncio.fixture
async def client(session: AsyncSession):
    from httpx import ASGITransport, AsyncClient
    from celerp.services.session_tracker import clear as _clear_tracker
    from celerp.gateway.state import set_session_token as _set_session_token
    from unittest.mock import patch, MagicMock

    await _clear_tracker(session)
    _saved_token = None
    try:
        from celerp.gateway.state import get_session_token
        _saved_token = get_session_token()
    except Exception:
        pass
    _set_session_token("")  # ensure clean gateway state
    # Set a mock relay client (used by relay-dependent code paths in tests).
    # The concurrent login gate is bypassed in tests because the tracker is
    # cleared at setup/teardown, so each test starts with an empty registry.
    app.dependency_overrides[get_session] = lambda: session
    with patch("celerp.gateway.client._client", MagicMock()), \
         patch("celerp.gateway.state.get_session_token", return_value="test-session-token"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    app.dependency_overrides.clear()
    await _clear_tracker(session)
    _set_session_token(_saved_token or "")
