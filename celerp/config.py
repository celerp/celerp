# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

import os
import platform
import sys
from pathlib import Path

from pydantic import Field
from pydantic.aliases import AliasChoices
from pydantic_settings import BaseSettings

_DEFAULT_JWT_SECRET = "dev-secret"
_CONFIG_ENV_VAR = "CELERP_CONFIG"


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp"
    jwt_secret: str = _DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    # Log the UI out after this many minutes of no user interaction (client-side idle timer).
    # Applied only when the installation is exposed as a server (headless/service-managed
    # or reachable at a public URL); see effective_idle_logout_minutes(). Set to 0 to disable.
    idle_logout_minutes: int = 15
    # Set to "true" to allow the default JWT secret (CI only).
    allow_insecure_jwt: str = "false"
    # Serve the OpenAPI schema (/openapi.json) and the grouped API reference.
    # Default off: production does not publish the API shape. Enabled only by the
    # press-kit capture harness (and CI) to render the reference. Never set true
    # in production.
    expose_openapi_schema: bool = False
    # Logging level for both API and UI processes. Override with LOG_LEVEL env var.
    # Accepted values: debug, info, warning, error, critical (case-insensitive).
    log_level: str = "INFO"
    # Public URL of this Celerp instance (e.g. https://erp.acme.com).
    # When set, share links include ?src= enabling p2p import by recipients.
    # Leave blank on private/LAN installs — bundle download fallback applies.
    celerp_public_url: str = ""
    # Web Access relay (opt-in - leave blank to disable entirely).
    # Set GATEWAY_TOKEN to activate the persistent WS connection to relay.celerp.com.
    # No `[gateway_token]` means no gateway connection, no product telemetry, and
    # no cloud dependency, except a startup subscription check.
    gateway_token: str = ""
    # Reusable partner deployment credential. A one-time registration input sent
    # only on the first relay `hello` of a partner-provisioned install, distinct
    # from the live-session gateway_token: it associates the instance with the
    # partner and is removed from bootstrap state once the relay accepts it.
    # Env CELERP_DEPLOYMENT_CREDENTIAL or [cloud] deployment_credential.
    deployment_credential: str = Field(
        default="",
        validation_alias=AliasChoices("CELERP_DEPLOYMENT_CREDENTIAL", "deployment_credential"),
    )
    # True once the relay has accepted the deployment credential and associated
    # this instance. Suppresses any re-send of the credential (an env-sourced
    # credential cannot be erased from the environment). Persisted as
    # [cloud] deployment_associated.
    deployment_associated: bool = False
    # Idempotency nonce for the partner deployment association. Generated once,
    # persisted before the first associate call, and reused on every retry and
    # restart so a lost response cannot create a duplicate association: the relay
    # keys the relationship on (partner, nonce). Dropped once consumed on a
    # successful association. Persisted as [cloud] deployment_nonce.
    deployment_nonce: str = ""
    # True after an explicit Cloud disconnect: the startup probe must not
    # re-link the install. Cleared when the user reconnects (settings or a
    # sign-in flow applies a fresh token). Persisted as [cloud] disconnected.
    cloud_disconnected: bool = False
    gateway_url: str = "wss://relay.celerp.com/ws/connect"
    # Unique instance identifier sent to gateway (auto-generated if blank).
    gateway_instance_id: str = ""
    # Local secret used only to redeem an email/Google-approved activation.
    # The relay sees SHA-256(verifier), never this value. Persisted until the
    # returned gateway credential is safely written, so response loss/restart is
    # retry-safe and API workers converge on one proof.
    activation_verifier: str = ""
    # HTTP base URL for relay API calls (quota, etc.).
    # Derived from gateway_url by replacing wss->https and stripping /ws/connect.
    # Override with GATEWAY_HTTP_URL if relay is on a different host.
    gateway_http_url: str = ""
    # Cloud backup (opt-in - requires Cloud subscription).
    # backup_encryption_key: 32-byte base64-encoded AES-256 key.
    # Auto-generated during Cloud activation, persisted to config.toml.
    backup_encryption_key: str = ""
    backup_hour: int = 2
    backup_enabled: bool = True
    # File storage backend (opt-in — requires Team subscription for s3).
    # storage_backend: "local" (default) or "s3"
    # For s3: set STORAGE_S3_ENDPOINT, STORAGE_S3_BUCKET, STORAGE_S3_ACCESS_KEY, STORAGE_S3_SECRET_KEY
    storage_backend: str = "local"
    storage_s3_endpoint: str = ""
    storage_s3_bucket: str = ""
    storage_s3_access_key: str = ""
    storage_s3_secret_key: str = ""
    # Self-hosted only: the operator declares that this install runs on a
    # customer-owned external database covered by the Team subscription. Default
    # off; set true via EXTERNAL_DB / [cloud] external_db. It gates Team-infra
    # visibility and the post-lapse "restore backup" recovery UI, so it is an
    # explicit, durable opt-in and is never inferred from database_url (every
    # ordinary self-hosted install points database_url at its own Postgres, which
    # is not the same as opting into customer-owned Team infrastructure).
    # Packaged builds detect the external database from celerp-config.json instead
    # and never read this field.
    external_db: bool = Field(
        default=False,
        validation_alias=AliasChoices("EXTERNAL_DB", "external_db"),
    )
    # Data directory for runtime artifacts (uploads, caches).
    # Accepts CELERP_DATA_DIR (Electron) or DATA_DIR (legacy). Defaults to ./data.
    data_dir: Path = Field(
        default=Path("data"),
        validation_alias=AliasChoices("CELERP_DATA_DIR", "DATA_DIR", "data_dir"),
    )
    # Directory containing pg_dump / pg_restore binaries.
    # Set automatically by the Electron app (CELERP_PG_BIN_DIR → bundled tools).
    # Self-hosted users can override via env var or config.toml [backup] pg_bin_dir.
    # Empty = fall back to system PATH and macOS candidate dirs. When set, the
    # tool MUST be found there — no PATH fallback (see backup._find_pg_tool).
    pg_bin_dir: str = Field(
        default="",
        validation_alias=AliasChoices("CELERP_PG_BIN_DIR", "pg_bin_dir"),
    )

    # Cookie security — set True in prod (HTTPS); False allows HTTP in dev/CI
    cookie_secure: bool = False
    # Redis URL for distributed rate limiting; empty = per-process only
    redis_url: str = ""  # e.g. redis://localhost:6379/0
    # Email: SMTP fallback for self-hosted installs.
    # If GATEWAY_TOKEN is set, email routes through the cloud relay instead.
    # If neither is configured, email notifications are silently skipped.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_from_name: str = "Celerp"  # Display name shown to recipients, e.g. "Acme ERP"
    smtp_tls: bool = True
    # Worker counts for multi-worker server deploys.
    # api_workers: number of Uvicorn workers for the API process.
    # gui_workers: number of Uvicorn workers for the GUI process.
    # Electron builds always use 1/1 regardless of this setting.
    api_workers: int = 2
    gui_workers: int = 1

    # GitHub-star CTA. The relay (celerp.com) is the single source of the count,
    # tier ladder, and copy; the install only renders the relay's resolved CTA and
    # falls back to a static neutral link when the relay is unreachable.
    star_cta_enabled: bool = True       # local kill switch
    star_cta_cache_ttl_s: int = 3600    # how long to cache the relay-resolved CTA


settings = Settings()


def ensure_instance_id() -> str:
    """Return one durable instance id shared by every local process."""
    if settings.gateway_instance_id:
        return settings.gateway_instance_id

    import uuid as _uuid

    def _ensure(cloud: dict) -> str:
        stored = cloud.get("instance_id")
        if isinstance(stored, str) and stored:
            return stored
        iid = str(_uuid.uuid4())
        cloud["instance_id"] = iid
        return iid

    iid = _update_cloud_config(_ensure)
    settings.gateway_instance_id = iid
    return iid


def ensure_activation_verifier() -> str:
    """Return one durable verifier shared by every API worker until activation."""
    if settings.activation_verifier:
        return settings.activation_verifier

    import secrets as _secrets

    def _ensure(cloud: dict) -> str:
        stored = cloud.get("activation_verifier")
        if isinstance(stored, str) and stored:
            return stored
        verifier = _secrets.token_urlsafe(32)
        cloud["activation_verifier"] = verifier
        return verifier

    verifier = _update_cloud_config(_ensure)
    settings.activation_verifier = verifier
    return verifier


def refresh_activation_verifier() -> str:
    """Replace the pending activation verifier with a fresh durable value."""
    import secrets as _secrets

    verifier = _secrets.token_urlsafe(32)

    def _refresh(cloud: dict) -> None:
        cloud["activation_verifier"] = verifier

    _update_cloud_config(_refresh)
    settings.activation_verifier = verifier
    return verifier


def ensure_connect_identity() -> tuple[str, str]:
    """Return the durable instance id + activation verifier in one locked RMW.

    Fresh account/claim routes need both values together. Creating them under
    one config lock avoids two sequential writes and, importantly, avoids
    self-contention when those synchronous helpers are offloaded from asyncio.
    """
    if settings.gateway_instance_id and settings.activation_verifier:
        return settings.gateway_instance_id, settings.activation_verifier

    import secrets as _secrets
    import uuid as _uuid

    def _ensure(cloud: dict) -> tuple[str, str]:
        iid = cloud.get("instance_id")
        if not isinstance(iid, str) or not iid:
            iid = str(_uuid.uuid4())
            cloud["instance_id"] = iid
        verifier = cloud.get("activation_verifier")
        if not isinstance(verifier, str) or not verifier:
            verifier = _secrets.token_urlsafe(32)
            cloud["activation_verifier"] = verifier
        return iid, verifier

    iid, verifier = _update_cloud_config(_ensure)
    settings.gateway_instance_id = iid
    settings.activation_verifier = verifier
    return iid, verifier


def activation_challenge(verifier: str | None = None) -> str:
    """SHA-256 challenge safe to send through account-proof requests."""
    import hashlib
    value = verifier or ensure_activation_verifier()
    return hashlib.sha256(value.encode()).hexdigest()


def set_cloud_disconnected(disconnected: bool) -> None:
    """Persist explicit Connect intent before changing any live runtime state."""
    def _set(cloud: dict) -> None:
        if disconnected:
            cloud["disconnected"] = True
        else:
            cloud.pop("disconnected", None)

    _update_cloud_config(_set)
    settings.cloud_disconnected = disconnected


def record_cloud_activation(
    gateway_token: str, instance_id: str, *, public_url: str | None = None,
    tos_version: str | None = None, backup_encryption_key: str | None = None,
    expected_api_key: str | None = None,
    expected_verifier: str | None = None,
    keep_disconnected: bool = False,
) -> bool:
    """CAS-persist authoritative activation without reviving stale operations."""
    def _record(cloud: dict) -> bool:
        if bool(cloud.get("disconnected")) and not keep_disconnected:
            return False

        current_iid = str(cloud.get("instance_id") or "")
        pending_verifier = str(cloud.get("activation_verifier") or "")

        if expected_api_key is not None:
            if cloud.get("token") != expected_api_key:
                return False
            if pending_verifier and current_iid and current_iid != instance_id:
                return False

        if expected_verifier is not None:
            if pending_verifier != expected_verifier:
                return False
            if current_iid and current_iid != instance_id:
                return False

        cloud["token"] = gateway_token
        cloud["instance_id"] = instance_id
        if public_url:
            cloud["public_url"] = public_url
        else:
            cloud.pop("public_url", None)
        if tos_version:
            cloud["tos_version"] = tos_version
        if backup_encryption_key:
            cloud["backup_encryption_key"] = backup_encryption_key
        if expected_verifier is not None:
            cloud.pop("activation_verifier", None)
        return True

    accepted = bool(_update_cloud_config(_record))
    if accepted and expected_verifier is not None:
        settings.activation_verifier = ""
    return accepted

def persist_cloud_settings(**values: object) -> None:
    """Write the given [cloud] settings into config.toml.

    Creates the file when it does not exist yet (first boot of a packaged
    install), so identity, token, and backup key survive restarts. Falsy
    values are skipped, never erased.
    """
    def _persist(cloud: dict) -> None:
        for key, value in values.items():
            if value:
                cloud[key] = value
    _update_cloud_config(_persist)


def ensure_deployment_nonce() -> str:
    """Return the deployment association nonce, generating and persisting one when
    absent.

    The nonce must be persisted before the association call so a lost response
    plus a retry reuses the same value and resolves to the same association
    (idempotency keyed on partner + nonce). A persist failure propagates so the
    caller can abort before any network call rather than send an unpersisted
    nonce that a later boot could not reproduce.
    """
    if settings.deployment_nonce:
        return settings.deployment_nonce
    import uuid as _uuid
    nonce = _uuid.uuid4().hex
    settings.deployment_nonce = nonce
    persist_cloud_settings(deployment_nonce=nonce)
    return nonce


def record_deployment_association(gateway_token: str, instance_id: str) -> None:
    """Record a successful partner deployment association durably in one write.

    Persists the relay-issued gateway_token and instance_id, sets the sticky
    deployment_associated marker, and removes the now-consumed deployment
    credential and nonce from [cloud]. persist_cloud_settings never erases a key,
    so this dedicated helper is what drops them. The disk write precedes the
    in-memory updates: if the write raises, the caller sees the failure and no
    live-but-unpersisted identity is carried, so the next boot's idempotent retry
    can recover.
    """
    def _record(cloud: dict) -> None:
        cloud["token"] = gateway_token
        cloud["instance_id"] = instance_id
        cloud["deployment_associated"] = True
        cloud.pop("deployment_credential", None)
        cloud.pop("deployment_nonce", None)
    _update_cloud_config(_record)
    settings.gateway_token = gateway_token
    settings.gateway_instance_id = instance_id
    settings.deployment_credential = ""
    settings.deployment_nonce = ""
    settings.deployment_associated = True


def load_cloud_config() -> None:
    """Load cloud settings from config.toml into the Settings object.

    Called at startup alongside ensure_instance_id(). Reads gateway_token,
    instance_id, and public_url from [cloud] if present - these values are
    written by the activation flow and survive restarts without env vars.
    """
    try:
        cfg = read_config()
    except Exception:
        return
    cloud = cfg.get("cloud", {})
    if not cloud:
        return
    disconnected = bool(cloud.get("disconnected"))
    if disconnected:
        settings.cloud_disconnected = True
        # gateway_token and celerp_public_url are also env-bound (GATEWAY_TOKEN /
        # CELERP_PUBLIC_URL), which pydantic reads at construction, before this runs.
        # A sticky disconnect has to win over that env credential too, or the install
        # boots token-bound and the settings UI shows it reconnected. Clear it here so
        # the whole app - tunnel, share seam, and the UI's token-bound view - treats
        # the install as disconnected until an explicit reconnect applies a fresh one.
        settings.gateway_token = ""
        settings.celerp_public_url = ""
    # A sticky disconnect keeps the credential in config for a one-click reconnect
    # but must NOT bring it live: leaving gateway_token/public_url unset holds the
    # tunnel down, share-minting off, and the startup probe skipped, so the
    # disconnected choice survives the restart. instance_id and the backup key are
    # identity, not the live connection, so they still load.
    if cloud.get("token") and not settings.gateway_token and not disconnected:
        settings.gateway_token = cloud["token"]
    if cloud.get("instance_id") and not settings.gateway_instance_id:
        settings.gateway_instance_id = cloud["instance_id"]
    if cloud.get("activation_verifier") and not settings.activation_verifier:
        settings.activation_verifier = cloud["activation_verifier"]
    if cloud.get("public_url") and not settings.celerp_public_url and not disconnected:
        settings.celerp_public_url = cloud["public_url"]
    if cloud.get("backup_encryption_key") and not settings.backup_encryption_key:
        settings.backup_encryption_key = cloud["backup_encryption_key"]
    # Deployment credential + association marker. The credential is only consumed
    # on the first hello, so a config value loads unless env already supplied one;
    # the marker is sticky (an associated install must never re-offer it).
    if cloud.get("deployment_credential") and not settings.deployment_credential:
        settings.deployment_credential = cloud["deployment_credential"]
    if cloud.get("deployment_associated"):
        settings.deployment_associated = True
    # The association nonce is reused across boots: reload it so a retry after a
    # lost response resolves to the same association rather than minting a new one.
    if cloud.get("deployment_nonce") and not settings.deployment_nonce:
        settings.deployment_nonce = cloud["deployment_nonce"]
    # Self-hosted external-database opt-in. Durable [cloud] key so the operator's
    # declaration survives restarts; loads unless the environment already set it.
    if cloud.get("external_db") and not settings.external_db:
        settings.external_db = True


def load_backup_config() -> None:
    """Load backup settings from config.toml into the Settings object.

    Called at startup. Reads [backup] pg_bin_dir so self-hosted users can
    point to their own pg_dump/pg_restore without setting env vars.
    The env var (CELERP_PG_BIN_DIR) takes precedence — this only fills in
    the setting when it hasn't been set by the environment already.
    """
    try:
        cfg = read_config()
    except Exception:
        return
    backup = cfg.get("backup", {})
    if backup.get("pg_bin_dir") and not settings.pg_bin_dir:
        settings.pg_bin_dir = backup["pg_bin_dir"]


def assert_secure_jwt() -> None:
    """Abort if JWT_SECRET is still the insecure default.

    Call this at server startup only — NOT at import time so that CLI
    commands like `celerp init` can run before a config exists.
    """
    if settings.jwt_secret == _DEFAULT_JWT_SECRET and settings.allow_insecure_jwt.lower() != "true":
        print(
            "FATAL: JWT_SECRET is set to the default 'dev-secret' value. "
            "Set a strong JWT_SECRET before running in production. "
            "To override in CI, set ALLOW_INSECURE_JWT=true.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Config file helpers — shared by CLI and API layer
# ---------------------------------------------------------------------------

def config_path() -> Path:
    override = os.environ.get(_CONFIG_ENV_VAR)
    if override:
        return Path(override)
    if platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "celerp" / "config.toml"


def read_config() -> dict:
    """Read config.toml. Returns {} if missing."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    path = config_path()
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def effective_idle_logout_minutes() -> int:
    """Idle-logout minutes for the current deployment, or 0 when it must be off.

    Idle logout is an installation-level exposure policy: on when Celerp runs as a
    server (headless/service-managed, or reachable at a public URL), off for an
    ordinary local desktop install. Cloud/account linkage or a gateway token alone
    is not exposure; only an active public URL is. An explicit Cloud disconnect
    suppresses the Connect-hosting reason but never overrides headless mode or an
    independently configured public URL. ``idle_logout_minutes <= 0`` is the
    explicit disable in every mode.

    The durable config is read here because Connect/disconnect state is written by
    the API process while the UI runs in a separate process, so the UI's in-memory
    settings can be stale.
    """
    minutes = max(0, int(settings.idle_logout_minutes))
    if minutes == 0:
        return 0

    cfg = {}
    try:
        cfg = read_config() or {}
    except Exception:
        pass

    env_headless = os.environ.get("CELERP_MODE", "").strip().lower() == "headless"
    config_headless = bool((cfg.get("server") or {}).get("headless"))
    if env_headless or config_headless:
        return minutes

    # An explicitly configured public URL is independent of Celerp Connect, so a
    # Cloud disconnect must not suppress a separately self-hosted/public server.
    explicit_public_url = os.environ.get("CELERP_PUBLIC_URL", "").strip()
    if explicit_public_url:
        return minutes

    cloud = cfg.get("cloud") or {}
    disconnected = bool(cloud.get("disconnected")) or settings.cloud_disconnected
    if disconnected:
        return 0

    public_url = str(cloud.get("public_url") or settings.celerp_public_url or "").strip()
    return minutes if public_url else 0


def _write_config_unlocked(cfg: dict) -> None:
    """Write cfg back to config.toml.

    Only emits sections that are present in cfg — never writes empty/zero
    defaults for sections the caller did not touch. This keeps the file
    minimal on first boot (e.g. only [modules]) and prevents overwriting
    already-written sections (e.g. [server] with api_port=0).
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    _str = lambda v: f'"{v}"'

    if "database" in cfg:
        db = cfg["database"]
        lines += ["[database]", f'url = {_str(db.get("url", ""))}']
        # `embedded = true` marks a bundled-PostgreSQL install so later commands
        # boot the cluster before connecting. Absent key = external (all configs
        # written before this shipped), so external installs are unchanged.
        if db.get("embedded"):
            lines += ["embedded = true"]
        lines += [""]

    if "auth" in cfg:
        auth = cfg["auth"]
        lines += ["[auth]", f'jwt_secret = {_str(auth.get("jwt_secret", ""))}']
        if auth.get("setup_code_hash"):
            lines.append(f'setup_code_hash = {_str(auth["setup_code_hash"])}')
        lines.append("")

    if "server" in cfg:
        srv = cfg["server"]
        lines += [
            "[server]",
            f'api_port = {srv.get("api_port", 0)}',
            f'ui_port = {srv.get("ui_port", 0)}',
        ]
        if srv.get("headless"):
            lines.append("headless = true")
        lines.append("")

    if "cloud" in cfg:
        cloud = cfg["cloud"]
        lines += [
            "[cloud]",
            f'token = {_str(cloud.get("token", ""))}',
            f'instance_id = {_str(cloud.get("instance_id", ""))}',
            f'public_url = {_str(cloud.get("public_url", ""))}',
            f'backup_encryption_key = {_str(cloud.get("backup_encryption_key", ""))}',
            f'tos_version = {_str(cloud.get("tos_version", ""))}',
        ]
        # Absent key = never explicitly disconnected (all configs written
        # before this shipped), matching the embedded/headless idiom.
        if cloud.get("activation_verifier"):
            lines.append(f'activation_verifier = {_str(cloud["activation_verifier"])}')
        if cloud.get("disconnected"):
            lines.append("disconnected = true")
        # Self-hosted last-known-good commercial context, compact JSON of the
        # already-validated envelope, so a partner-managed install presents its
        # partner identity across an offline restart instead of defaulting to
        # celerp_direct. Emitted only when set (a direct install that has never
        # cached one carries no key); serialised through the JSON string encoder
        # so embedded quotes survive the TOML round-trip.
        if cloud.get("commercial_context_json"):
            import json as _json
            lines.append(
                f"commercial_context_json = {_json.dumps(cloud['commercial_context_json'])}")
        # Deployment credential survives every [cloud] write until the relay
        # accepts it: emitted only while non-empty, and the association marker
        # only once set. A direct install carries neither key. Without this, the
        # fixed-key serializer would drop the credential before the first hello.
        if cloud.get("deployment_credential"):
            lines.append(f'deployment_credential = {_str(cloud["deployment_credential"])}')
        # The association nonce persists across boots until an association
        # consumes it, so a retry reuses it; emitted only while set.
        if cloud.get("deployment_nonce"):
            lines.append(f'deployment_nonce = {_str(cloud["deployment_nonce"])}')
        if cloud.get("deployment_associated"):
            lines.append("deployment_associated = true")
        # Self-hosted external-database opt-in, emitted only when set. A direct
        # local install carries no key and reads as off.
        if cloud.get("external_db"):
            lines.append("external_db = true")
        lines.append("")

    if "storage" in cfg:
        st = cfg["storage"]
        lines += [
            "[storage]",
            f'backend = {_str(st.get("backend", "local"))}',
            f's3_endpoint = {_str(st.get("s3_endpoint", ""))}',
            f's3_bucket = {_str(st.get("s3_bucket", ""))}',
            f's3_access_key = {_str(st.get("s3_access_key", ""))}',
            f's3_secret_key = {_str(st.get("s3_secret_key", ""))}',
            "",
        ]

    if "database_backup" in cfg:
        dbak = cfg["database_backup"]
        lines += ["[database_backup]", f'previous_url = {_str(dbak.get("previous_url", ""))}', ""]

    if "storage_backup" in cfg:
        sbak = cfg["storage_backup"]
        lines += [
            "[storage_backup]",
            f'backend = {_str(sbak.get("backend", ""))}',
            f's3_endpoint = {_str(sbak.get("s3_endpoint", ""))}',
            f's3_bucket = {_str(sbak.get("s3_bucket", ""))}',
            f's3_access_key = {_str(sbak.get("s3_access_key", ""))}',
            f's3_secret_key = {_str(sbak.get("s3_secret_key", ""))}',
            "",
        ]

    if "backup" in cfg:
        bak = cfg["backup"]
        lines += ["[backup]", f'pg_bin_dir = {_str(bak.get("pg_bin_dir", ""))}', ""]

    if "modules" in cfg:
        enabled = cfg["modules"].get("enabled", [])
        enabled_toml = ", ".join(f'"{m}"' for m in enabled)
        lines += ["[modules]", f"enabled = [{enabled_toml}]", ""]

    # Crash-safe replacement: fsync a 0600 temp inode, then atomically swap it
    # over config.toml. Readers see either the complete old file or complete new
    # file, never torn contents from concurrent workers.
    import uuid as _uuid
    data = "\n".join(lines)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{_uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        from celerp import config_store as _config_store
        _config_store._fsync_dir(str(path.parent))
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _config_lock():
    """Acquire the cross-process config.toml writer lock or fail closed."""
    from celerp import config_store as _config_store
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = f"{path}.lock"
    acquired = _config_store._acquire_lock(lock_path)
    if acquired is None:
        raise TimeoutError("could not acquire config.toml writer lock")
    return _config_store, lock_path, acquired


def write_config(cfg: dict) -> None:
    store, lock_path, (fd, token) = _config_lock()
    try:
        _write_config_unlocked(cfg)
    finally:
        store._release_lock(fd, lock_path, token)


def _update_config(mutator):
    """Locked read-modify-write of the complete config snapshot."""
    from copy import deepcopy

    store, lock_path, (fd, token) = _config_lock()
    try:
        cfg = read_config()
        before = deepcopy(cfg)
        result = mutator(cfg)
        if cfg != before:
            _write_config_unlocked(cfg)
        return result
    finally:
        store._release_lock(fd, lock_path, token)


def _update_cloud_config(mutator):
    """Locked read-modify-write of [cloud], returning mutator's result."""
    return _update_config(lambda cfg: mutator(cfg.setdefault("cloud", {})))


def resolve_install_order(names: list[str], module_dir: Path) -> list[str]:
    """Return names + all transitive depends_on deps, in topo order.

    Searches module_dir, premium_modules/, and every MODULE_DIR entry (where
    marketplace-installed and sideloaded third-party packages land) for manifests.
    """
    import ast as _ast

    _pkg_root = module_dir.parent
    _search_dirs = [module_dir, _pkg_root / "premium_modules"]
    # Marketplace/sideloaded modules live in MODULE_DIR, not the bundled trees.
    # Without these a third-party module's own depends_on is invisible, so the
    # dependency is never pre-enabled and the module silently fails to load after
    # restart (the loader skips it as "requires X, which is not enabled").
    for _d in os.environ.get("MODULE_DIR", "").split(","):
        _d = _d.strip()
        if _d:
            _p = Path(_d)
            if _p not in _search_dirs:
                _search_dirs.append(_p)

    def _find_pkg(name: str) -> Path | None:
        for d in _search_dirs:
            pkg = d / name / "__init__.py"
            if pkg.exists():
                return pkg
        return None

    def _get_deps(name: str) -> list[str]:
        pkg = _find_pkg(name)
        if not pkg:
            return []
        try:
            tree = _ast.parse(pkg.read_text())
        except Exception:
            return []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Assign):
                for t in node.targets:
                    if isinstance(t, _ast.Name) and t.id == "PLUGIN_MANIFEST":
                        try:
                            m = _ast.literal_eval(node.value)
                            return list(m.get("depends_on") or [])
                        except Exception:
                            return []
        return []

    ordered: list[str] = []
    visited: set[str] = set()

    def _visit(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        for dep in _get_deps(name):
            _visit(dep)
        ordered.append(name)

    for n in names:
        _visit(n)
    return ordered


def set_enabled_modules(names: list[str]) -> bool:
    """Idempotently add modules to the config file's enabled list.

    Resolves transitive dependencies and writes the updated config to disk.
    Works even when config.toml does not yet exist (e.g. Electron binary on
    first boot before 'celerp init' is run). write_config() handles missing
    sections with empty defaults so the file is always well-formed.

    Returns True if the enabled set changed (config was written), False if
    every requested module was already enabled (no-op). Callers can use this
    to skip follow-up work like a process restart when nothing changed.
    """
    _pkg_root = Path(__file__).parent.parent
    module_dir = _pkg_root / "default_modules"
    install_order = resolve_install_order(list(names), module_dir)

    def _enable(cfg: dict) -> bool:
        modules = cfg.setdefault("modules", {})
        currently_enabled: list[str] = list(modules.get("enabled", []))
        new_modules = [n for n in install_order if n not in currently_enabled]
        if not new_modules:
            return False
        modules["enabled"] = currently_enabled + new_modules
        return True

    return bool(_update_config(_enable))


def remove_enabled_module(name: str) -> None:
    """Drop one module from [modules].enabled in config.toml under the config
    lock, so the next restart honours a disable or removal. A name that is not
    enabled leaves the file untouched."""
    def _remove(cfg: dict) -> None:
        enabled = cfg.get("modules", {}).get("enabled", [])
        cfg.setdefault("modules", {})["enabled"] = [m for m in enabled if m != name]

    _update_config(_remove)


def sync_engine_url(db_url: str) -> str:
    """db_url for a synchronous SQLAlchemy engine (Alembic and the CLI's checks),
    naming psycopg2 as the driver. A bare postgresql:// leaves the driver to
    SQLAlchemy, whose default moved to psycopg 3 in 2.1, and only psycopg2 is
    installed. Not for pg_dump or psql, which take a plain libpq URL."""
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if db_url.startswith(prefix):
            return "postgresql+psycopg2://" + db_url[len(prefix):]
    return db_url
