# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from contextlib import asynccontextmanager
import asyncio
import logging
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from celerp.db import engine, mask_db_credentials
from celerp.inventory_codes import BarcodeConflictError
from celerp.config import settings, assert_secure_jwt, ensure_instance_id, load_cloud_config, load_backup_config
from celerp.gateway.state import load_commercial_context
load_cloud_config()
load_backup_config()
load_commercial_context()
assert_secure_jwt()
ensure_instance_id()
from celerp.middleware import DrainMiddleware, MaxBodySizeMiddleware, SecurityHeadersMiddleware, SlidingTokenRefreshMiddleware, log_unhandled_exception
from celerp.models.base import Base
from fastapi.staticfiles import StaticFiles

from celerp.routers import auth, companies, ledger
from celerp.routers import health, notifications, system, events as events_router_mod
from celerp.routers import stars as stars_router_mod

import celerp.models  # noqa: F401 - ensures kernel models (UserCompany, ImportBatch, DocShareToken) are registered

# ---------------------------------------------------------------------------
# Suppress CancelledError tracebacks from uvicorn on graceful shutdown.
# When the graceful-shutdown timeout fires, uvicorn cancels in-flight SSE
# tasks. Starlette's listen_for_disconnect coroutine raises CancelledError
# which uvicorn logs as "Exception in ASGI application". This is harmless
# but noisy - filter it out.
# ---------------------------------------------------------------------------
class _SuppressShutdownCancelledError(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[0] is asyncio.CancelledError:
            return False
        return True

logging.getLogger("uvicorn.error").addFilter(_SuppressShutdownCancelledError())

# Suppress all API-process access logs (this port is internal; UI process is
# what users connect to). Also suppress httpx relay noise and port 8000 startup
# line so the visible startup message is only the UI's port 8080.
_orig_logger_handle = logging.Logger.handle

def _filtered_logger_handle(self, record):
    try:
        msg = record.getMessage()
        # Drop all uvicorn.access lines (internal API, not user-facing)
        if self.name == "uvicorn.access":
            return
        # Drop httpx logs for relay/billing calls (chatty 401s etc.)
        if self.name in ("httpx", "httpcore") or self.name.startswith("httpx.") or self.name.startswith("httpcore."):
            return
        # Drop "Uvicorn running on http://0.0.0.0:8000" startup line
        if self.name == "uvicorn.error" and "0.0.0.0:8000" in msg:
            return
    except Exception:
        pass
    _orig_logger_handle(self, record)

logging.Logger.handle = _filtered_logger_handle

# Module system (opt-in: no-op if MODULE_DIR not set). Correct a MODULE_DIR whose
# first entry is the bundled default_modules/ tree so imports land in a writable
# drop-in, never among first-party modules (the dev/bare-run footgun).
import os as _os
from pathlib import Path as _Path
from celerp.modules.loader import with_writable_module_dir as _with_writable_module_dir
_os.environ["MODULE_DIR"] = _with_writable_module_dir(_os.environ.get("MODULE_DIR", ""))
_MODULE_DIR = _os.environ["MODULE_DIR"]


async def _try_auto_activate() -> None:
    """Probe the relay for an existing subscription and auto-connect if found.

    Called at startup when gateway_token is empty. Silent on any failure.
    Skipped entirely after an explicit Cloud disconnect: staying disconnected
    is the user's recorded choice, and reconnecting is always explicit.
    """
    _log = logging.getLogger(__name__)
    try:
        import asyncio

        import httpx
        from celerp.config import settings as _s, ensure_instance_id, config_path, persist_cloud_settings
        if _s.cloud_disconnected:
            return
        first_boot = not config_path().exists()
        iid = ensure_instance_id()
        from celerp.gateway.state import activate_payload, relay_http_url as _rhu
        relay_base = _rhu()
        _httpx_log = logging.getLogger("httpx")
        _prev_level = _httpx_log.level
        _httpx_log.setLevel(logging.WARNING)
        try:
            payload = activate_payload(iid, first_boot=first_boot)
            # Retry transient transport failures (slow first network, relay
            # restarting); an HTTP response of any status is final.
            r = None
            for delay in (0, 5, 30):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    async with httpx.AsyncClient(timeout=10.0) as c:
                        r = await c.post(f"{relay_base}/auth/activate", json=payload)
                    break
                except httpx.HTTPError:
                    continue
        finally:
            _httpx_log.setLevel(_prev_level)
        if r is None or r.status_code != 200:
            return
        data = r.json()
        token = data.get("gateway_token", "")
        if not token:
            return
        public_url = data.get("public_url")
        tos_version = data.get("tos_version")
        # Apply in-process
        _s.gateway_token = token
        _s.gateway_instance_id = iid
        if public_url:
            _s.celerp_public_url = public_url
        # Auto-generate backup encryption key
        if not _s.backup_encryption_key:
            import base64, secrets as _secrets
            _s.backup_encryption_key = base64.b64encode(_secrets.token_bytes(32)).decode()
        # Persist to config.toml (best-effort; the WS client must still start)
        try:
            persist_cloud_settings(
                token=token,
                instance_id=iid,
                public_url=public_url,
                tos_version=tos_version,
                backup_encryption_key=_s.backup_encryption_key,
            )
        except Exception:
            pass
        # Start gateway WS client, but only where the tunnel has something to serve:
        # a paid instance (public_url granted) or a free instance with a live share.
        # A free instance holds no persistent gateway connection; a later share-create
        # brings the tunnel up on demand through the relay_share seam.
        from celerp.gateway import ensure_running, has_active_share
        if public_url or await has_active_share():
            ensure_running()
            _log.info("Auto-activated cloud relay (instance_id=%s)", iid)
        # Start backup scheduler - paid tiers only (public_url is the paid signal;
        # a free instance is not entitled to backups at all).
        if public_url and _s.backup_enabled and _s.backup_encryption_key:
            from celerp.services import backup_scheduler
            backup_scheduler.start()
    except Exception as exc:
        logging.getLogger(__name__).debug("Auto-activate probe failed (expected for self-hosted): %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    (settings.data_dir / "static" / "attachments").mkdir(parents=True, exist_ok=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        masked_url = mask_db_credentials(settings.database_url)
        print(
            f"\nFATAL: Cannot connect to database at {masked_url}\n"
            f"  → {type(exc).__name__}: {exc}\n\n"
            "Fix: check DATABASE_URL in .env and make sure Postgres is running.\n"
            "  Ubuntu: sudo systemctl start postgresql\n"
            "  macOS:  brew services start postgresql@15\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load external modules (opt-in: no-op if MODULE_DIR not set)
    _loaded_modules = []
    if _MODULE_DIR:
        from celerp.modules.loader import load_all, register_api_routes
        from celerp.config import read_config as _read_config
        _enabled_env = _os.environ.get("ENABLED_MODULES", "")
        if _enabled_env:
            _enabled: set[str] = set(_enabled_env.split(","))
        else:
            # Fall back to config.toml (written by setup wizard apply-preset)
            _cfg = _read_config()
            _enabled = set(_cfg.get("modules", {}).get("enabled") or [])
        if _enabled:
            # Apply each enabled module's runtime migrations before importing it,
            # under the shared migration advisory lock. A third-party module whose
            # migration fails is dropped from this boot and its error held to
            # surface after load_all (which clears the load-error map on entry);
            # a first-party failure re-raises. No-op on non-Postgres.
            from celerp.modules.migrations_runner import run_migration_phase
            from celerp.modules.loader import record_load_error
            _enabled, _migration_errors = await run_migration_phase(engine, _enabled)
            _loaded_modules = load_all(_MODULE_DIR, _enabled)
            for _mname, _merr in _migration_errors.items():
                record_load_error(_mname, _merr)
            register_api_routes(_app, _loaded_modules)
            # Module models register on Base.metadata at import time.
            # Run create_all again so module tables are created (idempotent).
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            # Allow modules to backfill data for existing companies (e.g. seed
            # chart of accounts when accounting module is first enabled on an
            # instance that already has companies).
            from celerp.modules.slots import fire_lifecycle as _fire
            from celerp.db import SessionLocal as _SessionLocal
            # Best-effort, like the two sibling blocks below: a hook that fails
            # during flush poisons the shared session, so the commit raises.
            # Roll back and log at ERROR rather than let that crash boot - the
            # manufacturing seed hook, for one, must never be able to take the
            # app down.
            async with _SessionLocal() as _sess:
                try:
                    await _fire("on_modules_ready", session=_sess)
                    await _sess.commit()
                except Exception:
                    await _sess.rollback()
                    logging.getLogger(__name__).exception(
                        "on_modules_ready hooks failed (non-fatal); their data was rolled back"
                    )

            # A bundled default whose content no longer matches the first-party
            # lock is demoted to untrusted. Surface that in the notification bell
            # (deduped, company-wide) so it is visible from any page rather than
            # only on /modules. Best-effort - a notify failure must never block boot.
            try:
                from celerp.modules.loader import demoted_first_party
                _demoted = demoted_first_party(_enabled)
                if _demoted:
                    from celerp.modules.demotion import notify_demoted_modules
                    async with _SessionLocal() as _dsess:
                        await notify_demoted_modules(_dsess, _demoted)
                        await _dsess.commit()
            except Exception:
                logging.getLogger(__name__).debug(
                    "Demoted-module notification skipped (non-fatal)", exc_info=True)

    # Register kernel projection handler for sys.* events (not module-owned)
    from celerp.modules.slots import register as register_slot
    register_slot("projection_handler", {
        "prefix": "sys.",
        "handler": "celerp.projections.handlers.system:apply_system_event",
        "_module": "_kernel",
    })

    # Develop→release guard: on a version change, rebuild projections with the
    # release's handlers (now that all handlers are registered). Gated by a
    # marker so it runs once per version. Non-fatal: a failure must not block
    # boot — the marker stays unset and a later boot retries.
    try:
        from celerp.db import SessionLocal as _GuardSession
        from celerp.services.dev_release_guard import run_upgrade_guard
        async with _GuardSession() as _guard_sess:
            await run_upgrade_guard(_guard_sess)
            await _guard_sess.commit()
    except Exception:
        logging.getLogger(__name__).exception(
            "Develop→release upgrade guard failed (non-fatal); projections may be "
            "stale until rebuilt via doctor or /ledger/rebuild"
        )

    # One-time backfill: stamp the status→document pairing on items sold, memo'd,
    # or consigned in before that field shipped, so their inventory status links
    # to its document. Marker-gated (runs once); non-fatal like the guard above.
    try:
        from celerp.db import SessionLocal as _BackfillSession
        from celerp.services.status_doc_backfill import run_status_doc_backfill
        async with _BackfillSession() as _bf_sess:
            await run_status_doc_backfill(_bf_sess)
            await _bf_sess.commit()
    except Exception:
        logging.getLogger(__name__).exception(
            "Status-doc backfill failed (non-fatal); pre-existing sold/memo items "
            "may show their status without a document link until a later boot"
        )

    # One-time backfill: post the missing COGS JE for invoices finalized before
    # COGS moved into the finalize JE and never fulfilled since. Marker-gated
    # (runs once; retries stragglers while any doc is locked or errored);
    # non-fatal like the backfill above.
    try:
        from celerp.db import SessionLocal as _CogsSession
        from celerp.services.cogs_backfill import run_cogs_backfill
        async with _CogsSession() as _cogs_sess:
            await run_cogs_backfill(_cogs_sess)
            await _cogs_sess.commit()
    except Exception:
        logging.getLogger(__name__).exception(
            "COGS backfill failed (non-fatal); affected invoices keep their "
            "missing COGS until a later boot retries"
        )

    # A partner-packaged install with an unconsumed deployment credential
    # associates with its partner through the explicit relay seam before the
    # gateway starts. No-op for a direct install or one already associated.
    from celerp.gateway.bootstrap import associate_partner_deployment
    await associate_partner_deployment()

    # Bring up the relay tunnel per the lazy free-tier lifecycle (3.1). A token-holder
    # is past first activation and never re-enters it. Paid instances (public_url set)
    # keep the tunnel always-on; a free instance opens it at boot only when it already
    # has a live share to serve, and otherwise stays down until a share is created.
    if settings.gateway_token:
        from celerp.gateway import ensure_running, has_active_share
        if settings.celerp_public_url or await has_active_share():
            ensure_running()
    else:
        # Auto-activate: probe relay for an existing subscription (silent, no-op on failure)
        asyncio.create_task(_try_auto_activate())

    # Start backup scheduler - paid tiers only (public_url is the paid signal;
    # a free instance is not entitled to backups at all).
    if settings.celerp_public_url and settings.backup_encryption_key and settings.backup_enabled:
        from celerp.services import backup_scheduler
        backup_scheduler.start()
        log.debug("Backup scheduler started")

    # Start AI file cleanup background task
    from celerp.ai.cleanup import run_cleanup_loop
    cleanup_task = asyncio.create_task(run_cleanup_loop())

    # Start JTI cleanup background task (runs hourly, advisory lock prevents duplicates)
    from celerp.services.session_tracker import run_jti_cleanup_loop
    jti_cleanup_task = asyncio.create_task(run_jti_cleanup_loop())

    # Connector reconciliation scheduler: a daily incremental sync per connector,
    # backstopping any realtime webhooks missed while offline. No-op without a
    # relay session (self-hosted instances skip token fetch).
    from celerp.connectors.daily_scheduler import scheduler_loop_all
    from celerp.connectors.relay_token import fetch_context as _connector_token_fetcher
    connector_sched_task = asyncio.create_task(scheduler_loop_all(token_fetcher=_connector_token_fetcher))

    # Reorder low-stock alert scheduler: a daily per-company scan that notifies
    # once per dip when items reach their reorder point (no-op for companies with
    # alerts disabled or no reorder points set).
    from celerp.services.reorder import reorder_alert_loop
    reorder_alert_task = asyncio.create_task(reorder_alert_loop())

    yield

    # Terminate all active SSE connections so Uvicorn doesn't hang on shutdown
    from celerp.notifications.sse import shutdown_all as _sse_shutdown
    _sse_shutdown()

    # Stop background tasks
    cleanup_task.cancel()
    jti_cleanup_task.cancel()
    connector_sched_task.cancel()
    reorder_alert_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    # Stop backup scheduler
    try:
        from celerp.services import backup_scheduler
        backup_scheduler.stop()
    except Exception:
        pass

    # Close the tunnel and its run task, whoever started it (boot gate, auto-activate,
    # or a runtime share-create) - the gateway package owns that lifecycle now.
    from celerp.gateway import shutdown as _gateway_shutdown
    await _gateway_shutdown()


logging.basicConfig(level=settings.log_level.upper())
log = logging.getLogger(__name__)

_storage_uri = settings.redis_url or "memory://"
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"], storage_uri=_storage_uri)

_OPENAPI_TAGS = [
    {"name": "auth", "description": "Sign in, sessions, and access tokens."},
    {"name": "companies", "description": "Companies, members, and roles."},
    {"name": "items", "description": "Inventory items, stock, and valuation."},
    {"name": "docs", "description": "Invoices, purchase orders, quotations, and credit notes."},
    {"name": "lists", "description": "Shipping documents and packing lists."},
    {"name": "accounting", "description": "Chart of accounts and journal entries."},
    {"name": "ledger", "description": "Event-sourced ledger and projections."},
    {"name": "reports", "description": "Financial statements and aging reports."},
    {"name": "manufacturing", "description": "Bills of materials and production orders."},
    {"name": "labels", "description": "Label templates and barcode printing."},
    {"name": "crm", "description": "Contacts, pipeline, and activity."},
    {"name": "subscriptions", "description": "Recurring billing and auto-invoicing."},
    {"name": "connectors", "description": "External integrations."},
    {"name": "payments", "description": "Payment collection."},
    {"name": "backup", "description": "Data export and import."},
    {"name": "notifications", "description": "In-app notifications."},
    {"name": "events", "description": "Server-sent event stream for live updates."},
    {"name": "system", "description": "Health, status, and instance metadata."},
]

_OPENAPI_DESCRIPTION = "REST API for Celerp, the self-hosted business management platform."


def _openapi_settings() -> dict:
    """FastAPI OpenAPI kwargs for the current settings. Production keeps the schema
    unserved (openapi_url=None); the press-kit capture harness turns
    expose_openapi_schema on to publish it for the API reference shot, and that
    non-default state is logged so it is never silent. Isolated so the exposure
    boundary is unit-testable without reconstructing the whole app."""
    expose = settings.expose_openapi_schema
    if expose:
        log.warning("expose_openapi_schema is on: serving the public OpenAPI schema at /openapi.json")
    return {
        "description": _OPENAPI_DESCRIPTION,
        "openapi_tags": _OPENAPI_TAGS,
        "openapi_url": "/openapi.json" if expose else None,
    }


app = FastAPI(title="Celerp", docs_url=None, redoc_url=None, lifespan=lifespan, **_openapi_settings())
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(DrainMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SlidingTokenRefreshMiddleware)
app.add_middleware(MaxBodySizeMiddleware, max_body_size_bytes=10 * 1024 * 1024)

if settings.celerp_public_url:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.celerp_public_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(404)
async def not_found_handler(request: Request, exc) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Not found"})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log_unhandled_exception(request, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(_request: Request, _exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


@app.exception_handler(BarcodeConflictError)
async def barcode_conflict_handler(_request: Request, exc: BarcodeConflictError):
    # The projection applier raises this when the barcode unique index rejects a write
    # that bypassed the allocation lock (imports, connectors). A more specific handler
    # than the Exception catch-all, so it maps to 409 instead of a masked 500.
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# Kernel routes — always present regardless of module configuration
app.include_router(health.router, tags=["system"])
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(ledger.router, prefix="/ledger", tags=["ledger"])
app.include_router(companies.router, prefix="/companies", tags=["companies"])
app.include_router(system.router, prefix="/system", tags=["system"])
app.include_router(stars_router_mod.router, prefix="/stars", tags=["stars"])
app.include_router(notifications.router)
app.include_router(events_router_mod.router)

# Backup router — always registered; individual endpoints gate on cloud connection.
# celerp_backup lives in default_modules/celerp-backup/ which is not on sys.path
# until the module loader runs during lifespan. We add it explicitly here so the
# router is registered at app-construction time (not lifespan), which is required
# for FastAPI to include the routes in its route table before any request arrives.
_backup_pkg = _Path(__file__).parent.parent / "default_modules" / "celerp-backup"
if _backup_pkg.exists() and str(_backup_pkg) not in sys.path:
    sys.path.insert(0, str(_backup_pkg))
from celerp_backup.setup import setup_api_routes as _setup_backup  # noqa: E402
_setup_backup(app)

# AI and Connectors are proprietary cloud-gated core (not pluggable modules): wire them directly at
# app-construction time and register their slots, the same way backup is wired. They are not loaded via
# the module loader, so they cannot be replaced by a user-supplied module of the same name.
_ai_pkg = _Path(__file__).parent.parent / "default_modules" / "celerp-ai"
if _ai_pkg.exists() and str(_ai_pkg) not in sys.path:
    sys.path.insert(0, str(_ai_pkg))
from celerp_ai.setup import setup_api_routes as _setup_ai  # noqa: E402
_setup_ai(app)

_connectors_pkg = _Path(__file__).parent.parent / "default_modules" / "celerp-connectors"
if _connectors_pkg.exists() and str(_connectors_pkg) not in sys.path:
    sys.path.insert(0, str(_connectors_pkg))
from celerp_connectors.routes import setup_api_routes as _setup_connectors  # noqa: E402
_setup_connectors(app)
# Connectors' marketplace projection handler is registered directly (its handler lives in the kernel).
from celerp.modules.slots import register as _register_slot  # noqa: E402
_register_slot("projection_handler", {
    "prefix": "mp.",
    "handler": "celerp.projections.handlers.marketplace:apply_marketplace_event",
    "_module": "celerp-connectors",
})

# Debug router — only active when CELERP_DEBUG=1 (never in production by default)
if _os.environ.get("CELERP_DEBUG") == "1":
    from celerp.routers import debug as _debug
    _debug.install_pool_listeners()
    app.add_middleware(_debug.DebugMiddleware)
    app.include_router(_debug.router)

app.mount("/static", StaticFiles(directory=str(settings.data_dir / "static"), check_dir=False), name="static")
