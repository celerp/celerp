# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Bytecode goes to a cache outside the source tree. Every xdist worker shares the module
# folders, and loading a module removes the bytecode in its folder and then checks none is
# left: a worker that imported module code and wrote bytecode there at that moment made
# another worker's load fail. The processes the tests start use the same cache.
os.environ.setdefault("PYTHONPYCACHEPREFIX", os.path.join(tempfile.gettempdir(), "celerp-test-pycache"))
sys.pycache_prefix = sys.pycache_prefix or os.environ["PYTHONPYCACHEPREFIX"]

# The tests and every process they start import this checkout's code. An environment
# shared between checkouts can carry an editable install of another one, which a script
# run as `python scripts/x.py` (its own folder first on the path, not the checkout) would
# import instead.
_CHECKOUT = os.path.dirname(os.path.abspath(__file__))
os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, (_CHECKOUT, os.environ.get("PYTHONPATH"))))
import celerp  # noqa: E402

assert os.path.dirname(os.path.dirname(os.path.abspath(celerp.__file__))) == _CHECKOUT, (
    f"the tests import celerp from {celerp.__file__}, not from this checkout ({_CHECKOUT})")

from conftest_support import (
    is_own_test_config, provision_worker_db, resolve_worker_config, resolve_worker_data_dir, skip_relay_check_in,
    worker_label)

# No event loop is current until a test makes one. Otherwise the first async test's
# runner asks for the current loop and asyncio makes one on the spot; the runner puts
# it back as current afterwards, and the next sync test that calls asyncio.run()
# replaces it without closing it, so its sockets are reported as unclosed in whichever
# later test the garbage collector happens to run in.
asyncio.set_event_loop(None)

# Must be set before celerp.config is imported (JWT guard fires at module load).
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
# The suite wires the module routes it needs onto the apps itself, so the apps load no
# module trees of their own (an unset MODULE_DIR would mean the bundled trees).
os.environ.setdefault("MODULE_DIR", "")
# CELERP_DATA_DIR is also the packaged-build switch (ui/routes/settings_cloud.py), so an
# inherited one would run every test as the desktop build. Keep the data location under
# its plain name; the tests of packaged behavior set CELERP_DATA_DIR themselves.
_inherited_data_dir = os.environ.pop("CELERP_DATA_DIR", None)
if _inherited_data_dir:
    os.environ.setdefault("DATA_DIR", _inherited_data_dir)

# Point config.toml at a per-worker temp file. Otherwise every xdist worker shares
# ~/.config/celerp/config.toml, which ensure_instance_id() reads+writes on the
# cloud-relay endpoints: concurrent access across workers corrupts the file
# mid-write (a torn read raises tomllib.TOMLDecodeError) and leaks one test's
# instance_id/token into the next. resolve_worker_config is the shared rule (see
# its docstring for why a plain setdefault does not achieve this under xdist).
# The file is this run's alone (worker_label), so it starts empty and another suite
# running at once on this machine never reads or writes it.
_worker = worker_label(os.environ)
os.environ["CELERP_CONFIG"] = resolve_worker_config(os.environ.get("CELERP_CONFIG"), _worker)

# Per-worker data directory, for the same reason: the default ./data is shared with
# every other worker and with any app booted from this checkout.
os.environ["DATA_DIR"] = resolve_worker_data_dir(os.environ.get("DATA_DIR"), _worker)


_own_config, _own_data = os.environ["CELERP_CONFIG"], os.environ["DATA_DIR"]


@atexit.register
def _remove_own_temp_files() -> None:
    """Remove this run's config and data directory, never a real CELERP_CONFIG or DATA_DIR passed in."""
    if is_own_test_config(_own_config):
        Path(_own_config).unlink(missing_ok=True)
    if _own_data == resolve_worker_data_dir(None, _worker):
        shutil.rmtree(_own_data, ignore_errors=True)


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
            url = provision_worker_db(url, worker)
    else:
        from testcontainers.community.postgres import PostgresContainer
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


_provision_test_database()

import uuid

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
import mutation_baseline  # noqa: E402
import mutation_observer  # noqa: E402 - installs the write observer's listeners

# The `client` fixture patches celerp.gateway.state.get_session_token. The session
# gate keeps its own reference from import time, so it is imported here, before any
# patch is active, and checks the session a test actually seats.
import celerp.session_gate  # noqa: E402,F401
from ui.app import app as _ui_app

# Every request the apps serve, through any test's transport, is request scope for the
# write observer (mutation_observer.py): the outermost layer of each app's own stack.
for _label, _observed_app in (("api", app), ("ui", _ui_app)):
    _observed_app.build_middleware_stack = (
        lambda built=_observed_app.build_middleware_stack, label=_label: mutation_observer.observed(built(), label))
    _observed_app.middleware_stack = None

# A request the apps make over HTTP (the UI's to the API in the browser suite's servers)
# names the request it is made for, so a write made for a page load is judged as the page load's.
import httpx as _httpx  # noqa: E402
_httpx_send = _httpx.AsyncClient.send


async def _observed_send(self, request, **kwargs):
    request.headers.update(mutation_observer.outgoing_headers())
    return await _httpx_send(self, request, **kwargs)


_httpx.AsyncClient.send = _observed_send

import sys as _sys, os as _os
from pathlib import Path

from test_helpers import REPO_ROOT, DATABASE_URL, make_test_token, authed_cookies, _crm_available, company_auth, settle_background  # noqa: F401

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
from celerp_dashboard.ui_routes import setup_ui_routes as _setup_dashboard_ui
_setup_dashboard(app)
_setup_dashboard_ui(_ui_app)

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

# Register fixed assets module routes onto the test app.
_fa_src = _os.path.join(_os.path.dirname(__file__), "default_modules", "celerp-fixed-assets")
if _os.path.abspath(_fa_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_fa_src))
from celerp_fixed_assets.routes import setup_api_routes as _setup_fa
from celerp_fixed_assets.ui_routes import setup_ui_routes as _setup_fa_ui
_setup_fa(app)
_setup_fa_ui(_ui_app)

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
            "prefix": "received_doc.",
            "handler": "celerp_docs.received:apply_received_event",
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
        "slot": "item_lineage_guard",
        "contrib": {
            "handler": "celerp_manufacturing.movements:guard_output_lineage",
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
            "prefix": "fa.",
            "handler": "celerp_fixed_assets.projections:apply_fixed_asset_event",
            "_module": "celerp-fixed-assets",
        },
    },
    {
        "slot": "demo_records",
        "contrib": {
            "handler": "celerp_fixed_assets.demo:seed_demo_assets_hook",
            "_module": "celerp-fixed-assets",
            "_first_party": True,
        },
    },
    {
        "slot": "doc_asset_lines",
        "contrib": {
            "handler": "celerp_fixed_assets.sales:on_asset_lines",
            "_module": "celerp-fixed-assets",
            "_first_party": True,
        },
    },
    {
        "slot": "import_target",
        "contrib": {
            "kind": "fixed_asset_register",
            "href": "/fixed-assets/import",
            "handler": "celerp_fixed_assets.imports:import_register",
            "_module": "celerp-fixed-assets",
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


@pytest.fixture(scope="session", autouse=True)
def _one_current_event_loop():
    """The test thread's current event loop, held for the whole run and closed at its end.
    pytest-asyncio restores the current loop after each async test, making one the first
    time it finds none; a plain test that calls asyncio.run() then replaces it, and the
    dropped loop warns as unclosed in whichever test is running when it is collected."""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


_CENSUS = mutation_observer.Totals()


@pytest.fixture(autouse=True)
def _mutation_census(request):
    """One write-observer record per test, kept for the census file
    (CELERP_MUTATION_CENSUS names its folder). A write the observer judges unprotected,
    out of order, for another company or that it cannot classify fails the test that made
    it, unless it is exactly one the baseline accepts (mutation_baseline.py)."""
    mutation_observer.reset(request.node.nodeid)
    yield
    seen = mutation_observer.reset()
    if request.node.get_closest_marker("observer_control"):
        return  # the observer's own controls make their findings on purpose
    _CENSUS.add(seen)
    refused = [f for f in seen.findings if not mutation_baseline.accepted(f)]
    if refused:
        pytest.fail(mutation_observer.explain(refused), pytrace=False)


def pytest_sessionfinish(session, exitstatus):
    path = mutation_observer.census_path()
    if path:
        _CENSUS.write(f"{path}.json")
        mutation_observer.write_routes(f"{path}.routes.json", [app, _ui_app])


@pytest.fixture(autouse=True)
def _reset_hot_path_caches():
    """Bust the in-process drain cache and relay methods cache before each test.

    The module-level caches are correct at runtime (single process, explicit
    bust on mutation, or a time-limited relay answer). In tests, each test rolls
    back its database changes and mocks its own relay, so the caches must be
    cleared to avoid leaking state between tests.
    """
    import asyncio
    from celerp.routers import health
    from celerp.services.runtime_state import _drain_cache_bust
    _drain_cache_bust()
    health._relay_methods.clear()
    health._relay_methods_lock = asyncio.Lock()
    yield
    _drain_cache_bust()
    health._relay_methods.clear()


@pytest.fixture(autouse=True)
def _forget_temp_imports(tmp_path_factory):
    """Drop every package a test imported from a pytest temp directory.

    Module fixtures live in temp directories. Once imported, a package stays in
    sys.modules, and admission treats an imported name as taken by its folder,
    so a later test's module of the same name in a fresh directory would be
    refused. Forgetting them keeps each test's modules its own.
    """
    before = set(_sys.modules)
    yield
    root = _os.path.realpath(tmp_path_factory.getbasetemp())
    for key in set(_sys.modules) - before:
        mod = _sys.modules.get(key)
        location = (getattr(mod, "__file__", None)
                    or next(iter(getattr(mod, "__path__", None) or []), None))
        if location and _os.path.realpath(location).startswith(root + _os.sep):
            _sys.modules.pop(key, None)


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
    _sub = _gw.get_subscription_state()
    yield
    _gw.set_instance_id(_iid)
    _gw.set_session_token(_tok)
    _gw.set_feature_flags(_flags)
    _gw.set_subscription_state(*_sub)
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
        _cfg_before = Path(_cfg_path).read_bytes()
    yield
    for f, v in snapshot.items():
        setattr(_s, f, v)
    if _cfg_path:
        if _cfg_before is None:
            try:
                os.remove(_cfg_path)
            except FileNotFoundError:
                pass
        elif _cfg_before != (Path(_cfg_path).read_bytes() if os.path.exists(_cfg_path) else None):
            Path(_cfg_path).write_bytes(_cfg_before)


@pytest.fixture(autouse=True)
def _disable_rate_limits():
    from celerp.routers.auth import limiter as auth_limiter
    from celerp.services.auth import password_attempts

    auth_limiter.enabled = False
    auth_limiter._storage.reset()
    password_attempts.reset()
    yield
    auth_limiter.enabled = False
    auth_limiter._storage.reset()
    password_attempts.reset()


@pytest.fixture(autouse=True)
def _ensure_slot_registration():
    """Re-register module projection handler slots before each test."""
    _ensure_slots()
    yield
    _ensure_slots()


@pytest.fixture(autouse=True)
def _restore_import_path():
    """Restore sys.path after each test.

    Loading a module puts its folder on sys.path. Tests that load modules from
    temporary folders left hundreds of dead entries behind on a worker, and every
    later import then searched them all.
    """
    before = list(_sys.path)
    yield
    _sys.path[:] = before


@pytest.fixture(autouse=True)
def _restore_bytecode_settings():
    """Restore the process-wide bytecode settings after each test. Loading a module
    turns bytecode writing off for the rest of the process; each test starts from
    the settings the test run began with."""
    before = (_sys.dont_write_bytecode, _sys.pycache_prefix)
    yield
    _sys.dont_write_bytecode, _sys.pycache_prefix = before


@pytest.fixture(autouse=True)
def _ui_api_not_running(request, monkeypatch):
    """Unit tests run the UI with no API server behind it. A request the UI sends to the
    API fails as a refused connection does, without opening a socket: no test reaches a
    server that happens to listen on the API's port, and no connection attempt outlives
    its test. Tests that answer as the API patch the transport or client themselves.
    Browser tests run a real API."""
    if request.node.get_closest_marker("browser"):
        return
    import httpx
    from ui import api_client

    async def _refused(self, api_request):
        raise httpx.ConnectError("The API is not running in unit tests.", request=api_request)

    monkeypatch.setattr(api_client._SharedTransport, "handle_async_request", _refused)


@pytest.fixture(autouse=True)
def _held_ai_limits():
    """Test relays model a current AI service: the app holds its published limits, so
    a model call does not first fetch them (tests/test_ai_deadlines.py forgets them
    to test the fetch itself). Set and restored without monkeypatch: an autouse
    fixture that requests it sets it up ahead of the fixtures that restore shared
    state, which would then tear down while a test's patches are still in place
    (tests/test_conftest_fixture_order.py)."""
    from celerp.ai import deadlines
    before = deadlines._held
    deadlines._held = deadlines.CURRENT_RELAY_DEFAULTS
    yield
    deadlines._held = before


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
    celerp.modules.loader._loaded and _module_routes AND registers every
    module's lifecycle hooks into celerp.modules.slots._slots; without
    resetting them they leak into
    later tests in the same worker (e.g. a module shows running=True, or the
    manufacturing on_company_created hook seeds a default work center for a test
    that expects none, or a later UI request is answered by the per-company
    module gate), which surfaces under xdist's test distribution.

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
    from celerp.modules.loader import _loaded, _module_routes
    from celerp.modules import outcome as _outcome
    from celerp.modules import slots as _slots_mod
    _loaded.clear()
    _module_routes.clear()
    _slot_snapshot = {k: list(v) for k, v in _slots_mod._slots.items()}
    yield
    _loaded.clear()
    _module_routes.clear()
    _outcome._awaiting_ui = False  # a lifespan that loaded modules waits for a UI
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
import celerp.models.marketplace  # noqa: F401,E402 - registers marketplace_configs, as celerp/migrations/env.py does

# Session-scoped engine: created once, shared across all tests to avoid OOM from
# 1000+ engine create/dispose cycles when test_ui.py + test_routers/ run together.
@pytest_asyncio.fixture(scope="session", loop_scope="session")
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


def _admin_conn(dbname: str):
    """A psycopg2 autocommit connection to an existing database on the same server,
    for issuing CREATE/DROP DATABASE against a different database."""
    from urllib.parse import urlsplit
    import psycopg2

    parts = urlsplit(DATABASE_URL.replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=dbname)
    conn.autocommit = True
    return conn


@pytest_asyncio.fixture
async def committed_engine():
    """An engine on a database of the test's own, for row-lock race tests."""
    # Race tests need REAL cross-connection contention, so they COMMIT seed rows
    # to disk rather than rolling back like the shared `session` fixture. Running
    # them against the ambient per-xdist-worker database would leak that committed
    # state into every other test that shares the worker DB - e.g. an empty-DB
    # bootstrap or inventory assertion later on the same worker. Each race test
    # therefore gets its OWN database, created here and dropped on teardown, so
    # nothing leaks.
    import uuid
    from urllib.parse import urlsplit, urlunsplit

    from sqlalchemy.pool import NullPool

    parts = urlsplit(DATABASE_URL.replace("+asyncpg", ""))
    base_db = parts.path.lstrip("/") or "postgres"
    race_db = f"{base_db}_race_{uuid.uuid4().hex[:8]}"

    conn = _admin_conn(base_db)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{race_db}"')
    finally:
        conn.close()

    race_url = urlunsplit(parts._replace(path=f"/{race_db}")).replace(
        "postgresql://", "postgresql+asyncpg://")
    eng = create_async_engine(race_url, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()
        admin = _admin_conn(base_db)
        try:
            # dispose() closes the pool, but a session a test left open can outlive
            # it, and a freshly-created database also briefly attracts an autovacuum
            # worker. We do NOT use DROP DATABASE ... WITH (FORCE): FORCE terminates
            # every backend including that autovacuum worker, which is owned by the
            # bootstrap superuser and cannot be terminated by the unprivileged test
            # role. Instead we terminate only the client backends this role itself
            # owns (same-user termination is always permitted) and let the plain
            # DROP handle the rest.
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid() "
                    "AND usename = current_user",
                    (race_db,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{race_db}"')
        finally:
            admin.close()


@pytest_asyncio.fixture
async def _background_work_finishes():
    """Background work a test's requests started finishes on the test's own event
    loop. Left running, it would be cut off when the loop closes, and a database
    connection it held would only be reported unclosed in some later test."""
    yield
    await settle_background()


def pytest_collection_modifyitems(items):
    # Every async test, and only those: a sync test has no loop of its own to settle.
    for item in items:
        if pytest_asyncio.is_async_test(item) and "_background_work_finishes" not in item.fixturenames:
            item.fixturenames.append("_background_work_finishes")


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


@pytest.fixture(autouse=True)
def core_sample_records_only(monkeypatch, _ensure_slot_registration):
    """Companies registered in a test start with the core sample stock only, without the
    sample records modules add on top (Fixed assets seeds asset bills). A module whose tests
    need its own sample records overrides this fixture in its test folder."""
    from celerp.modules import slots

    monkeypatch.setitem(slots._slots, "demo_records", [])


@pytest.fixture
def ids():
    """Fresh company and user ids for ``auth``."""
    return {"company_id": uuid.uuid4(), "user_id": uuid.uuid4()}


@pytest_asyncio.fixture
async def auth(session, ids):
    """A company with its books and an admin (test_helpers.company_auth), and the admin's request headers."""
    return await company_auth(session, ids["company_id"], ids["user_id"])


@pytest_asyncio.fixture
async def drained_jobs():
    """Drains the AI jobs a test started (``test_helpers.drain_jobs``); request it
    after ``client`` and ``session`` so it is torn down before them."""
    from test_helpers import drain_jobs
    yield
    await drain_jobs()


@pytest_asyncio.fixture
async def client(session: AsyncSession):
    import asyncio
    from httpx import ASGITransport, AsyncClient
    from celerp.services import background
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
    # The module boundary reads a company's settings the same way, so it sees the
    # companies a test registered.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _shared_session_ctx():
        yield session

    # get_lifecycle_session_ctx() runs maintenance work (the projection rebuild,
    # the Doctor) on the unbounded lifecycle_engine in production, because a full
    # rebuild can exceed the request statement_timeout. In tests that would open a
    # SEPARATE connection that cannot see this transaction's uncommitted seed data,
    # so fix-mode counts come back empty. LifecycleSessionLocal is resolved from
    # celerp.db at call time, so binding a savepoint-joined sessionmaker to the one
    # shared connection routes every lifecycle session (each a fresh session that
    # closes on exit) into this transaction: it sees the seeded data and its commits
    # stay inside the rolled-back outer transaction.
    lifecycle_factory = async_sessionmaker(
        bind=await session.connection(), class_=AsyncSession,
        expire_on_commit=False, join_transaction_mode="create_savepoint")

    # AI job tasks run beside the requests that start them, on the same connection.
    # Their sessions join the outer transaction without savepoints of their own, so
    # a job's commit can never release a savepoint a request session still holds.
    job_factory = async_sessionmaker(
        bind=await session.connection(), class_=AsyncSession,
        expire_on_commit=False, join_transaction_mode="rollback_only")
    # The model gate's condition binds to the event loop that first waits on it, and
    # every test runs on its own loop: each test gets a gate of its own.
    from celerp.ai import jobs

    with patch("celerp.ai.jobs.gate", jobs.ModelGate(jobs.gate.size)), \
         patch("celerp.gateway.client._client", MagicMock()), \
         patch("celerp.gateway.state.get_session_token", return_value="test-session-token"), \
         patch("celerp.middleware.get_session_ctx", _shared_session_ctx), \
         patch("celerp.modules.boundary.get_session_ctx", _shared_session_ctx), \
         patch("celerp.db.LifecycleSessionLocal", lifecycle_factory), \
         patch("celerp.ai.jobs.SessionLocal", job_factory):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
        # Work a request left running (an email send and its receipt) finishes on this
        # test's loop: cut off by the loop closing, it would leave its connection open.
        await asyncio.gather(*list(background._BG_TASKS), return_exceptions=True)
    app.dependency_overrides.clear()
    # A test that started on held-back records leaves neither the flag nor its cause behind.
    app.state.data_current, app.state.held_back = True, None
    await _clear_tracker(session)
    _set_session_token(_saved_token or "")


@pytest_asyncio.fixture
async def owner_ui(client):
    """The UI app signed in as the owner of a newly registered company, talking to the
    real API in process (the `client` fixture's database and transaction)."""
    import uuid
    from unittest.mock import patch
    from httpx import ASGITransport, AsyncClient

    r = await client.post("/auth/register", json={
        "company_name": "Owner UI Co", "email": f"owner-ui-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    def _bridged(tok, timeout=10.0):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {tok}"}, follow_redirects=True)

    with patch("ui.api_client._client", _bridged):
        async with AsyncClient(transport=ASGITransport(app=_ui_app), base_url="http://ui",
                               follow_redirects=False, cookies={"celerp_token": token}) as ui:
            ui.api = _bridged(token)
            yield ui
            await ui.api.aclose()


_DISABLED_MODULE_TABLES = {"label_": "label_templates", "marketplace_": "marketplace_configs",
                           "bank_": "bank_accounts"}


@pytest.fixture
def bundled_modules_unloaded(monkeypatch):
    """The table metadata of a process that runs no bundled module: core tables only,
    since a module that is turned off never has its models imported. Yields
    {prefix: table} for module tables such a process does not declare."""
    import sqlalchemy as sa
    from celerp.models.base import Base

    module_tables = {m.local_table.name for m in Base.registry.mappers
                     if m.class_.__module__.startswith("celerp_")}
    assert set(_DISABLED_MODULE_TABLES.values()) <= module_tables
    core = sa.MetaData()
    for table in Base.metadata.tables.values():
        if table.name not in module_tables:
            table.to_metadata(core)
    monkeypatch.setattr(Base, "metadata", core)
    return dict(_DISABLED_MODULE_TABLES)


@pytest.fixture
def files_unchecked(monkeypatch):
    """Skip the load-time check that a module's files are those admission read, so a
    test can change them after admission and reach the checks that follow it."""
    from celerp.modules import loader
    monkeypatch.setattr(loader, "check_unchanged", lambda module: None)


@pytest.fixture(autouse=True)
def docs_running(monkeypatch):
    """Record Documents as loaded: the harness mounts its routes, as a running installation does.
    A test of a process where it has not started asks for ``docs_not_started``."""
    from test_helpers import mark_running
    mark_running(monkeypatch, "celerp-docs")


@pytest.fixture
def docs_not_started(monkeypatch, docs_running):
    """This process has not started Documents: the loader lists it as not running."""
    from celerp.modules import loader
    monkeypatch.setattr(loader, "_loaded", [m for m in loader._loaded if m["name"] != "celerp-docs"])


@pytest.fixture
def connector_modules_running(monkeypatch):
    """Record the modules connectors write into as loaded: Inventory, Documents and Contacts."""
    from test_helpers import mark_running
    mark_running(monkeypatch, "celerp-inventory", "celerp-docs", "celerp-contacts")


@pytest.fixture(autouse=True)
def no_connection_off_this_machine(monkeypatch):
    """Refuse, and fail the test for, any connection to a host but this machine or the
    test database. Code left unstubbed would otherwise reach the live relay, and its
    socket outlives the test and fails a later one; refused, it takes its offline path.
    Yields the connections refused so far, for a test about the guard itself."""
    import errno
    import ipaddress
    import socket
    import traceback
    from sqlalchemy.engine import make_url
    from celerp.config import settings

    db_host = make_url(settings.database_url).host
    allowed = {socket.gethostbyname(db_host)} if db_host else set()
    reached = []

    def _off_machine(address):
        try:
            return address[0] not in allowed and not ipaddress.ip_address(address[0]).is_loopback
        except (TypeError, ValueError, IndexError):
            return False

    def _refusing(real, refuse):
        def _connect(sock, address):
            if not _off_machine(address):
                return real(sock, address)
            # Where it was opened from, so the failure names the code to stub.
            reached.append((address, "".join(traceback.format_stack(limit=25))))
            return refuse(address)
        return _connect

    def _raise(address):
        raise ConnectionRefusedError(errno.ECONNREFUSED, f"tests do not connect off this machine: {address}")

    monkeypatch.setattr(socket.socket, "connect", _refusing(socket.socket.connect, _raise))
    monkeypatch.setattr(socket.socket, "connect_ex", _refusing(socket.socket.connect_ex, lambda _: errno.ECONNREFUSED))
    yield reached
    assert not reached, "connected off this machine:\n" + "\n".join(f"{a} from\n{stack}" for a, stack in reached)


@pytest.fixture(autouse=True)
def relay_backed_reads_offline(monkeypatch):
    """Answer the screens' requests whose API answer comes from the relay (account
    methods, the AI offer) as the API does when the relay is unreachable. Opening a
    document refreshes the free-send quota in a task that outlives the test, and the
    dashboard reads the AI offer. A test about an answer patches it again itself."""
    import ui.api_client as api

    async def _methods_offline(token):
        return {"google": False, "free_email_quota": 0, "google_start_url": ""}

    async def _offer_offline(token):
        return {"paid_ai": False, "free_ai_questions": None, "plan_ai_questions": None}

    monkeypatch.setattr(api, "account_methods", _methods_offline)
    monkeypatch.setattr(api, "ai_offer", _offer_offline)


@pytest.fixture
def relay_not_called_at_boot(no_connection_off_this_machine, monkeypatch):
    """Boot without checking in with the relay (skip_relay_check_in). A test that wants the
    check-in patches it again itself."""
    skip_relay_check_in(monkeypatch.setattr)
