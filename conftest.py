# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import os

from conftest_support import is_own_test_config, resolve_worker_config

# Must be set before celerp.config is imported (JWT guard fires at module load).
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

# Point config.toml at a per-worker temp file. Otherwise every xdist worker shares
# ~/.config/celerp/config.toml, which ensure_instance_id() reads+writes on the
# cloud-relay endpoints: concurrent access across workers corrupts the file
# mid-write (a torn read raises tomllib.TOMLDecodeError) and leaks one test's
# instance_id/token into the next. resolve_worker_config is the shared rule (see
# its docstring for why a plain setdefault does not achieve this under xdist).
_worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
os.environ["CELERP_CONFIG"] = resolve_worker_config(os.environ.get("CELERP_CONFIG"), _worker)

# Start from a clean config: a temp file left by a previous local run (e.g. a
# persisted [cloud] disconnected = true from the disconnect endpoint) must not
# leak into this session. Scoped to our own temp path so a real CELERP_CONFIG
# passed in by CI is never touched.
_cfg_start = os.environ["CELERP_CONFIG"]
if is_own_test_config(_cfg_start) and os.path.exists(_cfg_start):
    os.remove(_cfg_start)

# ── Postgres for the whole test suite ──────────────────────────────────────────
# Both production targets (the server and the Electron embedded-postgres build)
# run Postgres, so the tests run on Postgres too. A pre-set DATABASE_URL (e.g. a
# CI services container) is honored; otherwise we start a throwaway Postgres via
# testcontainers (Docker), tuned for throwaway speed (fsync/synchronous_commit/
# full_page_writes off — safe because the data is discarded). Each pytest-xdist
# worker gets its own database. MUST run before celerp.db / test_helpers import.
_PG_CONTAINER = None


def _tune_pg_server(url: str) -> None:
    """Turn off durability guards on a preset (CI) Postgres — mirrors the testcontainers
    tuning. The test DB is throwaway, so fsync / synchronous_commit / full_page_writes off is
    safe and removes the per-commit disk syncs that dominate a commit-heavy suite on CI's stock
    Postgres service. Server-wide (covers every xdist worker DB) and applied via pg_reload_conf
    (all three GUCs are reloadable — no restart). Best-effort: a no-op if the role lacks the
    superuser needed for ALTER SYSTEM, which only costs speed, never correctness."""
    from urllib.parse import urlsplit
    import psycopg2

    parts = urlsplit(url.replace("+asyncpg", ""))
    try:
        conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                                password=parts.password, dbname=parts.path.lstrip("/") or "postgres")
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("ALTER SYSTEM SET fsync = off")
                cur.execute("ALTER SYSTEM SET synchronous_commit = off")
                cur.execute("ALTER SYSTEM SET full_page_writes = off")
                cur.execute("SELECT pg_reload_conf()")
        finally:
            conn.close()
    except Exception:
        pass


def _provision_test_database() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql"):
        _tune_pg_server(url)
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
from celerp_labels.ui_routes import setup_ui_routes as _setup_labels_ui
_setup_labels(app)
_setup_labels_ui(_ui_app)

# Register warehousing module path so its UI components can be imported in tests.
_wh_src = _os.path.join(_os.path.dirname(__file__), "premium_modules", "celerp-warehousing")
_wh_available = _os.path.isfile(_os.path.join(_wh_src, "celerp_warehousing", "__init__.py"))
if _wh_available:
    if _os.path.abspath(_wh_src) not in [_os.path.abspath(p) for p in _sys.path]:
        _sys.path.insert(0, _os.path.abspath(_wh_src))
    # Register warehousing API routes onto the test app.
    from celerp_warehousing.routes import router as _wh_router
    app.include_router(_wh_router, prefix="/warehousing", tags=["warehousing"])

def _nav_slot_contributions() -> list[dict]:
    """Nav entries read from the modules' own manifests, as the loader reads them.

    They used to be hand-copied into this file, which put a second copy of every
    manifest in the test harness and left it free to drift: a nav entry added to a
    module did not exist for any test until someone remembered to mirror it here.
    A nav test written against the copy proves only that the copy is intact.
    """
    from celerp.modules.loader import read_manifest

    out: list[dict] = []
    for pkg in sorted((Path(__file__).parent / "default_modules").iterdir()):
        manifest = read_manifest(pkg)
        name = manifest.get("name")
        nav = (manifest.get("slots") or {}).get("nav")
        if not name or not nav:
            continue
        for item in (nav if isinstance(nav, list) else [nav]):
            out.append({"slot": "nav", "contrib": {**item, "_module": name}})
    return out


_SLOT_CONTRIBUTIONS = _nav_slot_contributions() + [
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
    # The retired bom.* prefix is intentionally not registered — historical bom.* events fall
    # through to the projection engine's default merge handler on replay.
    {
        "slot": "doc_finalize_hook",
        "contrib": {
            "handler": "celerp_manufacturing.routes:auto_create_work_orders_on_finalize",
            "_module": "celerp-manufacturing",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "shop.sync.",
            "handler": "celerp.projections.handlers.shopify:apply_shop_sync_event",
            "_module": "celerp-connectors",
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
        # Keyed by module as well as target: accounting and reports both offer
        # /reports, and collapsing them here would hide which one the nav kept.
        def _key(c: dict) -> tuple:
            return (c.get("_module"), c.get("prefix") or c.get("href") or c.get("handler"))

        if _key(contrib) not in {_key(c) for c in registered}:
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
def _reset_gateway_state():
    """Restore the gateway-state module globals around each test.

    The commercial resolver reads `_instance_id` (set by the gateway client on
    connect) which OVERRIDES settings.gateway_instance_id. A test that connects
    the client leaks `_instance_id` into later tests in other modules — e.g. the
    AI subscribe-URL tests, which patch settings.gateway_instance_id and would
    otherwise see the leaked global instead. Snapshot + restore both globals.
    """
    from celerp.gateway import state as _gw
    _iid, _tok = _gw.get_instance_id(), _gw.get_session_token()
    # Relay-pushed commercial state (feature flags + commercial context) also
    # lives in gateway-state module globals. A test that pushes flags or a
    # commercial context leaks them into later tests on the same worker. The
    # setter for commercial context rejects a non-newer version, so restore its
    # global directly rather than through set_commercial_context.
    _flags, _ctx = _gw.get_feature_flags(), _gw.get_commercial_context()
    yield
    _gw.set_instance_id(_iid)
    _gw.set_session_token(_tok)
    _gw.set_feature_flags(_flags)
    _gw._commercial_context = _ctx
    # Gateway-lifecycle globals: a test that patches asyncio.create_task while the
    # real ensure_running() runs leaves celerp.gateway._run_task a MagicMock (and
    # can leave a stray client). A later test's gateway shutdown() would then await
    # that MagicMock and raise. Reset both to the down baseline after every test.
    import celerp.gateway as _gwpkg
    from celerp.gateway import client as _gwc
    _gwpkg._run_task = None
    _gwc.set_client(None)


# Process-global settings fields that the cloud-activation endpoints mutate
# in place (gateway_token etc. via _apply_gateway_token_api, instance_id via
# ensure_instance_id). Snapshot+restore so a test that activates/claims/applies
# a token cannot leak the values into a later test on the same xdist worker.
_CLOUD_SETTINGS_FIELDS = (
    "gateway_token", "gateway_instance_id", "gateway_http_url",
    "celerp_public_url", "backup_encryption_key", "backup_enabled",
    "cookie_secure", "cloud_disconnected",
)


@pytest.fixture(autouse=True)
def _reset_cloud_settings():
    """Restore the mutable cloud/backup `settings` fields around each test.

    The cloud-relay endpoints write straight onto the process-global
    `celerp.config.settings` object (e.g. _apply_gateway_token_api sets
    gateway_token / celerp_public_url and auto-generates backup_encryption_key;
    ensure_instance_id sets gateway_instance_id). Nothing else reset these, so a
    successful activate/claim/apply-token test leaked a non-empty gateway_token
    into later tests on the same worker — order-dependent flakiness that
    surfaced as cloud-claim timeouts in one shard and passed on rerun. This is
    the settings-side counterpart to _reset_gateway_state above.
    """
    from celerp.config import settings as _s
    snapshot = {f: getattr(_s, f) for f in _CLOUD_SETTINGS_FIELDS}
    # The disconnect endpoint also PERSISTS to the config file
    # ([cloud] disconnected = true via write_config). That file is shared per
    # xdist worker, so a persisted disconnect leaks into a later test whose app
    # lifespan calls load_cloud_config() and re-reads it - which re-sets
    # cloud_disconnected and defeats the in-memory restore below. Snapshot and
    # restore the file too, symmetric with the settings restore.
    _cfg_path = os.environ.get("CELERP_CONFIG")
    _cfg_before = None
    if _cfg_path and os.path.exists(_cfg_path):
        with open(_cfg_path, "rb") as _fh:
            _cfg_before = _fh.read()
    yield
    for f, v in snapshot.items():
        setattr(_s, f, v)
    if _cfg_path:
        if _cfg_before is None:
            try:
                os.remove(_cfg_path)
            except FileNotFoundError:
                pass
        elif _cfg_before != (open(_cfg_path, "rb").read() if os.path.exists(_cfg_path) else None):
            with open(_cfg_path, "wb") as _fh:
                _fh.write(_cfg_before)


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
    celerp.modules.loader._loaded AND registers every module's lifecycle hooks
    into celerp.modules.slots._slots; without resetting both they leak into
    later tests in the same worker (e.g. a module shows running=True, or the
    manufacturing on_company_created hook seeds a default work center for a test
    that expects none), which surfaces under xdist's test distribution.

    The slot registry is snapshotted and restored around the test rather than
    cleared, so the canonical unit-harness contributions (_ensure_slots) survive
    while anything a load_all() adds during the test is torn down.

    Browser tests are exempt: their server runs in-process and legitimately owns
    a populated _loaded (module-gated UI like the credit-note "Receive Returns"
    button reads loaded_modules()) and slot registry, so wiping it would hide
    that UI."""
    if request.node.get_closest_marker("browser"):
        yield
        return
    from celerp.modules.loader import _loaded
    from celerp.modules import slots as _slots_mod
    _loaded.clear()
    _slot_snapshot = {k: list(v) for k, v in _slots_mod._slots.items()}
    yield
    _loaded.clear()
    _slots_mod._slots.clear()
    _slots_mod._slots.update({k: list(v) for k, v in _slot_snapshot.items()})


@pytest.fixture(scope="session", autouse=True)
def _refuse_live_database():
    """Refuse a connection to the developer's own `celerp` database.

    The CLI's init/start/migrate paths reach Postgres through psycopg2. A test that
    exercises one of them without patching the migrate step connects to whatever
    database the config names, which on a development machine is the live `celerp`
    database: the test passes locally, applies migrations to real data on the way
    past, and fails only in CI, where no such database exists. Refusing here makes
    the omission fail the same way in both places.
    """
    import psycopg2

    real_connect = psycopg2.connect

    def _guarded(*args, **kwargs):
        dbname = kwargs.get("dbname") or kwargs.get("database") or ""
        host = kwargs.get("host") or ""
        if dbname == "celerp" and host in ("", "localhost", "127.0.0.1", "::1"):
            raise AssertionError(
                "test opened a connection to the live 'celerp' database; patch the "
                "CLI step under test (celerp.cli._migrate_to_head) instead"
            )
        return real_connect(*args, **kwargs)

    psycopg2.connect = _guarded
    yield
    psycopg2.connect = real_connect


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

    # Middleware (DrainMiddleware, SlidingTokenRefreshMiddleware) opens its own
    # session via get_session_ctx() rather than the injected request session. Each
    # test runs inside one uncommitted outer transaction on a single connection
    # (the `session` fixture), so a middleware session on a *separate* connection
    # blocks on that transaction's row locks - e.g. DrainMiddleware's _get_or_create
    # INSERT of the SystemRuntimeState singleton the request session is already
    # holding uncommitted - and hangs until the per-test timeout. Route middleware
    # to the same shared session (the patch point middleware.py imports for this
    # purpose) so it joins the one transaction instead of deadlocking against it.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _shared_session_ctx():
        yield session

    with patch("celerp.gateway.client._client", MagicMock()), \
         patch("celerp.gateway.state.get_session_token", return_value="test-session-token"), \
         patch("celerp.middleware.get_session_ctx", _shared_session_ctx):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    app.dependency_overrides.clear()
    await _clear_tracker(session)
    _set_session_token(_saved_token or "")
