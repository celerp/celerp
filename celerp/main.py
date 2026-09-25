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

from celerp import __version__
from celerp.db import engine, lifecycle_engine, mask_db_credentials
from celerp.inventory_codes import CodeConflictError
from celerp.config import settings, assert_secure_jwt, ensure_instance_id, load_cloud_config, load_backup_config
from celerp.gateway.state import load_commercial_context
load_cloud_config()
load_backup_config()
load_commercial_context()
assert_secure_jwt()
ensure_instance_id()
from celerp.middleware import DrainMiddleware, MaxBodySizeMiddleware, SecurityHeadersMiddleware, SlidingTokenRefreshMiddleware, log_unhandled_exception
from celerp.models.base import Base

from celerp.routers import auth, companies, ledger
from celerp.routers import health, notifications, system, events as events_router_mod
from celerp.routers import stars as stars_router_mod
from celerp.routers import search as search_router_mod

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
    """Recover a challenge-approved activation, otherwise only check in.

    The verifier is durable, so retrying it after response loss returns the same
    credential. UUID-only activation is intentionally not retried at startup.
    """
    _log = logging.getLogger(__name__)
    try:
        import httpx
        from celerp.config import (
            settings as _s, ensure_instance_id, config_path)
        if _s.cloud_disconnected:
            return
        first_boot = not config_path().exists()
        iid = await asyncio.to_thread(ensure_instance_id)
        from celerp.gateway.state import activate_payload, relay_http_url as _rhu
        relay_base = _rhu()
        verifier = _s.activation_verifier or ""

        if not verifier:
            async def _checkin():
                async with httpx.AsyncClient(timeout=6.0) as c:
                    return await c.post(
                        f"{relay_base}/auth/checkin",
                        json=activate_payload(iid, first_boot=first_boot),
                    )

            try:
                await asyncio.wait_for(_checkin(), timeout=6.0)
            except (httpx.HTTPError, asyncio.TimeoutError):
                pass
            return
        else:
            # Challenge redemption is idempotent for this verifier, so transient
            # transport retries are safe here.
            from celerp.gateway.state import relay_post_with_retry
            r = await relay_post_with_retry(
                f"{relay_base}/auth/activate",
                activate_payload(
                    iid, first_boot=first_boot, activation_verifier=verifier))

        if r is None or r.status_code != 200:
            return
        data = r.json()
        token = data.get("gateway_token", "")
        if not token:
            return
        public_url = data.get("public_url")
        from celerp.services.cloud_entitlement import apply_activation_state
        await apply_activation_state(
            token, iid, public_url=public_url,
            tos_version=data.get("tos_version"),
            backup_encryption_key=data.get("backup_encryption_key"),
            tier=data.get("tier"), status=data.get("status"),
            expected_verifier=verifier if verifier else None)
        _log.info("Recovered cloud relay activation (instance_id=%s)", iid)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "Activation recovery/check-in failed (expected for self-hosted): %s", exc)


async def _try_sync_existing_entitlement() -> None:
    """Best-effort boot convergence for an already-persisted relay credential."""
    try:
        from celerp.services.cloud_entitlement import sync_existing_entitlement
        await sync_existing_entitlement(require_persisted_key=True)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "Cloud startup reconciliation failed (non-fatal): %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    (settings.data_dir / "static" / "attachments").mkdir(parents=True, exist_ok=True)
    try:
        async with lifecycle_engine.begin() as conn:
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
            async with lifecycle_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            # Allow modules to backfill data for existing companies (e.g. seed
            # chart of accounts when accounting module is first enabled on an
            # instance that already has companies).
            from celerp.modules.slots import fire_lifecycle as _fire
            from celerp.db import LifecycleSessionLocal as _LifecycleSession
            # Best-effort, like the two sibling blocks below: a hook that fails
            # during flush poisons the shared session, so the commit raises.
            # Roll back and log at ERROR rather than let that crash boot - the
            # manufacturing seed hook, for one, must never be able to take the
            # app down. Seed hooks can replay large ledgers, so they run on the
            # unbounded lifecycle engine, not the timeout-bounded request pool.
            async with _LifecycleSession() as _sess:
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
                    async with _LifecycleSession() as _dsess:
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
        from celerp.db import LifecycleSessionLocal as _GuardSession
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
        from celerp.db import LifecycleSessionLocal as _BackfillSession
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
        from celerp.db import LifecycleSessionLocal as _CogsSession
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
        # Authenticated activation is the canonical durable reconciliation path.
        # It is bounded, idempotent for established credentials, and runs in the
        # background so tunnel startup is never delayed.
        asyncio.create_task(_try_sync_existing_entitlement())
    else:
        # Auto-activate: probe relay for an existing subscription (silent, no-op on failure)
        asyncio.create_task(_try_auto_activate())

    # Start backup scheduler - paid tiers only (public_url is the paid signal;
    # a free instance is not entitled to backups at all).
    if settings.celerp_public_url and settings.backup_encryption_key and settings.backup_enabled:
        from celerp.services import backup_scheduler
        backup_scheduler.start()
        log.debug("Backup scheduler started")

    # AI batch jobs cannot survive a restart: mark any left pending or running
    # as failed so their owners are told to resend instead of waiting forever.
    try:
        from celerp.ai.batch import fail_interrupted_jobs
        from celerp.db import LifecycleSessionLocal as _BatchSweepSession
        async with _BatchSweepSession() as _sweep_session:
            interrupted = await fail_interrupted_jobs(_sweep_session)
            await _sweep_session.commit()
        if interrupted:
            logging.getLogger(__name__).warning(
                "Marked %d interrupted AI batch job(s) as failed", interrupted
            )
    except Exception:
        logging.getLogger(__name__).exception("AI batch job sweep failed (non-fatal)")

    # Start AI file cleanup background task
    from celerp.ai.cleanup import run_cleanup_loop
    cleanup_task = asyncio.create_task(run_cleanup_loop())

    # Start JTI cleanup background task (runs hourly, advisory lock prevents duplicates)
    from celerp.services.session_tracker import run_jti_cleanup_loop
    jti_cleanup_task = asyncio.create_task(run_jti_cleanup_loop())

    # Adopt legacy connector rows only when ownership is
    # unambiguous, then start near-real-time outbound stock delivery.
    from celerp.connectors.outbound_queue import (
        adopt_legacy_connector_configs,
        outbound_queue_loop,
    )
    await adopt_legacy_connector_configs()
    outbound_connector_task = asyncio.create_task(outbound_queue_loop())

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

    # Update checks: reports the last update attempt once, then checks hourly and,
    # when automatic updates are on, installs overnight in the owner's time zone.
    from celerp.services.update import update_loop
    update_task = asyncio.create_task(update_loop(restart=system._send_sigterm))

    yield

    # Terminate all active SSE connections so Uvicorn doesn't hang on shutdown
    from celerp.notifications.sse import shutdown_all as _sse_shutdown
    _sse_shutdown()

    # Stop background tasks
    cleanup_task.cancel()
    jti_cleanup_task.cancel()
    connector_sched_task.cancel()
    outbound_connector_task.cancel()
    reorder_alert_task.cancel()
    update_task.cancel()
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
    {"name": "auth", "description": "Authenticate users and manage sessions and access tokens."},
    {"name": "companies", "description": "Manage companies, members, and roles."},
    {"name": "items", "description": "Manage inventory items, stock levels, and valuation."},
    {"name": "docs", "description": "Create and manage invoices, purchase orders, quotations, and credit notes."},
    {"name": "lists", "description": "Create and manage shipping documents and packing lists."},
    {"name": "accounting", "description": "Manage the chart of accounts and journal entries."},
    {"name": "ledger", "description": "Work with the event-sourced ledger and its projections."},
    {"name": "reports", "description": "Generate financial statements and aging reports."},
    {"name": "manufacturing", "description": "Manage bills of materials and production orders."},
    {"name": "labels", "description": "Manage label templates, barcodes, and printing."},
    {"name": "crm", "description": "Manage contacts, pipeline, and activity."},
    {"name": "subscriptions", "description": "Manage recurring billing and automatic invoicing."},
    {"name": "connectors", "description": "Configure and operate external integrations."},
    {"name": "payments", "description": "Manage payment collection."},
    {"name": "backup", "description": "Export and import Celerp data."},
    {"name": "notifications", "description": "Read and manage in-app notifications."},
    {"name": "events", "description": "Consume server-sent events for live updates."},
    {"name": "system", "description": "Inspect health, status, and instance metadata."},
]

_OPENAPI_SUMMARY = (
    "Self-hosted ERP API for inventory, accounting, CRM, documents, manufacturing, "
    "reporting, and automation."
)

_OPENAPI_DESCRIPTION = (
    "The Celerp REST API provides programmatic access to the business operations "
    "used to integrate and automate a self-hosted Celerp ERP instance, including "
    "inventory, accounting, CRM, invoices and purchasing documents, manufacturing, "
    "reporting, and related workflows. Requests use Celerp authentication and "
    "role-based permissions. API documentation: https://celerp.com/docs/api"
)


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
        "summary": _OPENAPI_SUMMARY,
        "description": _OPENAPI_DESCRIPTION,
        "version": __version__,
        "contact": {
            "name": "Celerp",
            "url": "https://celerp.com/about",
            "email": "hello@celerp.com",
        },
        "openapi_tags": _OPENAPI_TAGS,
        "openapi_url": "/openapi.json" if expose else None,
    }


app = FastAPI(title="Celerp REST API", docs_url=None, redoc_url=None, lifespan=lifespan, **_openapi_settings())
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


@app.exception_handler(CodeConflictError)
async def code_conflict_handler(_request: Request, exc: CodeConflictError):
    # The projection applier raises a CodeConflictError (barcode or RFID / EPC) when a
    # physical-code unique index rejects a write that bypassed the allocation lock
    # (imports, connectors). One handler on the shared base maps every physical-code
    # collision to 409 instead of a masked 500.
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# Kernel routes — always present regardless of module configuration
app.include_router(health.router, tags=["system"])
app.include_router(health.settings_router, tags=["settings"])
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(ledger.router, prefix="/ledger", tags=["ledger"])
app.include_router(companies.router, prefix="/companies", tags=["companies"])
app.include_router(system.router, prefix="/system", tags=["system"])
app.include_router(system.update_router, prefix="/system", tags=["system"])
app.include_router(stars_router_mod.router, prefix="/stars", tags=["stars"])
app.include_router(notifications.router)
app.include_router(events_router_mod.router)
app.include_router(search_router_mod.router, tags=["search"])

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

from celerp.routers import attachments as _attachments  # noqa: E402
app.include_router(_attachments.router)
