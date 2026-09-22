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
            # Without a durable recovery proof, startup is observational only.
            # There is one controlled relay, so there is no older protocol to
            # fall back to and UUID knowledge never becomes credential authority.
            async def _checkin():
                async with httpx.AsyncClient(timeout=6.0) as c:
                    await c.post(
                        f"{relay_base}/auth/checkin",
                        json=activate_payload(iid, first_boot=first_boot))

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
        from celerp.gateway.state import entitlement_snapshot
        snapshot = entitlement_snapshot(data)
        if snapshot is None:
            return
        connect_entitled, feature_flags = snapshot
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
            connect_entitled=connect_entitled,
            feature_flags=feature_flags,
            expected_verifier=verifier if verifier else None)
        _log.info("Recovered cloud relay activation (instance_id=%s)", iid)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "Activation recovery/check-in failed (expected for self-hosted): %s", exc)


async def _try_sync_existing_entitlement() -> None:
    """Best-effort boot convergence for an already-persisted relay credential."""
    try:
        from celerp.services.cloud_entitlement import sync_existing_entitlement
        await sync_existing_entitlement()
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