# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from contextlib import asynccontextmanager
import asyncio
import logging
import re
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from celerp.db import engine
from celerp.config import settings, assert_secure_jwt, ensure_instance_id, load_cloud_config
load_cloud_config()
assert_secure_jwt()
ensure_instance_id()
from celerp.middleware import MaxBodySizeMiddleware, SecurityHeadersMiddleware, SlidingTokenRefreshMiddleware, log_unhandled_exception
from celerp.models.base import Base
from fastapi.staticfiles import StaticFiles

from celerp.routers import auth, companies, ledger
from celerp.routers import health, notifications, system

import celerp.models  # noqa: F401 - ensures kernel models (UserCompany, ImportBatch, DocShareToken) are registered

# Module system (opt-in: no-op if MODULE_DIR not set)
import os as _os
_MODULE_DIR = _os.environ.get("MODULE_DIR", "")


async def _try_auto_activate() -> None:
    """Probe the relay for an existing subscription and auto-connect if found.

    Called at startup when gateway_token is empty. Silent on any failure.
    """
    _log = logging.getLogger(__name__)
    try:
        import httpx
        from celerp.config import settings as _s, ensure_instance_id, read_config, write_config
        iid = ensure_instance_id()
        relay_base = (
            _s.gateway_http_url.rstrip("/") if _s.gateway_http_url
            else _s.gateway_url.replace("wss://", "https://").replace("ws://", "http://").replace("/ws/connect", "")
        )
        _httpx_log = logging.getLogger("httpx")
        _prev_level = _httpx_log.level
        _httpx_log.setLevel(logging.WARNING)
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(f"{relay_base}/auth/activate", json={"instance_id": iid})
        finally:
            _httpx_log.setLevel(_prev_level)
        if r.status_code != 200:
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
        # Persist to config.toml
        try:
            cfg = read_config()
            if cfg:
                cloud = cfg.setdefault("cloud", {})
                cloud["token"] = token
                cloud["instance_id"] = iid
                if public_url:
                    cloud["public_url"] = public_url
                if tos_version:
                    cloud["tos_version"] = tos_version
                if _s.backup_encryption_key:
                    cloud["backup_encryption_key"] = _s.backup_encryption_key
                write_config(cfg)
        except Exception:
            pass
        # Start gateway WS client
        import asyncio
        from celerp.gateway import client as _gw
        if _gw.get_client() is None:
            gw = _gw.GatewayClient(gateway_token=token, instance_id=iid, gateway_url=_s.gateway_url)
            _gw.set_client(gw)
            asyncio.create_task(gw.run())
        _log.info("Auto-activated cloud relay (instance_id=%s)", iid)
        # Start backup scheduler
        if _s.backup_enabled and _s.backup_encryption_key:
            from celerp.services import backup_scheduler
            backup_scheduler.start()
    except Exception as exc:
        logging.getLogger(__name__).debug("Auto-activate probe failed (expected for self-hosted): %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import os
    import uuid
    from pathlib import Path
    Path("static/attachments").mkdir(parents=True, exist_ok=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        masked_url = re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", settings.database_url)
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
            _loaded_modules = load_all(_MODULE_DIR, _enabled)
            register_api_routes(_app, _loaded_modules)
            # Module models register on Base.metadata at import time.
            # Run create_all again so module tables are created (idempotent).
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

    # Register kernel projection handler for sys.* events (not module-owned)
    from celerp.modules.slots import register as register_slot
    register_slot("projection_handler", {
        "prefix": "sys.",
        "handler": "celerp.projections.handlers.system:apply_system_event",
        "_module": "_kernel",
    })

    # Start gateway client if configured (opt-in, no-op if GATEWAY_TOKEN is blank)
    gateway_task = None
    if settings.gateway_token:
        from celerp.gateway import client as _gw
        instance_id = settings.gateway_instance_id or str(uuid.uuid4())
        gw = _gw.GatewayClient(
            gateway_token=settings.gateway_token,
            instance_id=instance_id,
            gateway_url=settings.gateway_url,
        )
        _gw.set_client(gw)
        gateway_task = asyncio.create_task(gw.run())
        log.info("Gateway client started (instance_id=%s)", instance_id)
    else:
        # Auto-activate: probe relay for an existing subscription (silent, no-op on failure)
        asyncio.create_task(_try_auto_activate())

    # Start backup scheduler if cloud is connected and backup is enabled
    if settings.gateway_token and settings.backup_encryption_key and settings.backup_enabled:
        from celerp.services import backup_scheduler
        backup_scheduler.start()
        log.info("Backup scheduler started")

    # Start AI file cleanup background task
    from celerp.ai.cleanup import run_cleanup_loop
    cleanup_task = asyncio.create_task(run_cleanup_loop())

    yield

    # Terminate all active SSE connections so Uvicorn doesn't hang on shutdown
    from celerp.notifications.sse import shutdown_all as _sse_shutdown
    _sse_shutdown()

    # Stop AI file cleanup
    cleanup_task.cancel()
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

    if gateway_task:
        from celerp.gateway import client as _gw
        if _gw.get_client():
            await _gw.get_client().close()
        gateway_task.cancel()
        try:
            await asyncio.wait_for(gateway_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        _gw.set_client(None)


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

_storage_uri = settings.redis_url or "memory://"
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"], storage_uri=_storage_uri)

app = FastAPI(title="Celerp", docs_url=None, redoc_url=None, lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
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


# Kernel routes — always present regardless of module configuration
app.include_router(health.router, tags=["system"])
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(ledger.router, prefix="/ledger", tags=["ledger"])
app.include_router(companies.router, prefix="/companies", tags=["companies"])
app.include_router(system.router, prefix="/system", tags=["system"])
app.include_router(notifications.router)
app.mount("/static", StaticFiles(directory="static", check_dir=False), name="static")
