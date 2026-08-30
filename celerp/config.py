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
    # Uniform across direct and relay access; set to 0 to disable.
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
    # True after an explicit Cloud disconnect: the startup probe must not
    # re-link the install. Cleared when the user reconnects (settings or a
    # sign-in flow applies a fresh token). Persisted as [cloud] disconnected.
    cloud_disconnected: bool = False
    gateway_url: str = "wss://relay.celerp.com/ws/connect"
    # Unique instance identifier sent to gateway (auto-generated if blank).
    gateway_instance_id: str = ""
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
    """Return gateway_instance_id, generating and persisting one if blank.

    Called at startup so every instance has a stable UUID before the user
    ever visits the Cloud settings page.
    """
    if settings.gateway_instance_id:
        return settings.gateway_instance_id

    import uuid as _uuid
    iid = str(_uuid.uuid4())
    settings.gateway_instance_id = iid

    # Persist to config.toml, creating it when missing - the id must survive
    # restarts (best-effort; silently skip on error)
    try:
        persist_cloud_settings(instance_id=iid)
    except Exception:
        pass

    return iid


def persist_cloud_settings(**values: str) -> None:
    """Write the given [cloud] settings into config.toml.

    Creates the file when it does not exist yet (first boot of a packaged
    install), so identity, token, and backup key survive restarts. Falsy
    values are skipped, never erased.
    """
    cfg = read_config()
    cloud = cfg.setdefault("cloud", {})
    for key, value in values.items():
        if value:
            cloud[key] = value
    write_config(cfg)


def record_deployment_association() -> None:
    """Record that the relay has associated this instance and remove the
    deployment credential from bootstrap state.

    Called once, after the first successful hello_ack that carried the
    credential. persist_cloud_settings never erases a key, so a dedicated helper
    is needed to pop the credential: it drops [cloud] deployment_credential, sets
    the sticky deployment_associated marker, persists, and clears the in-memory
    credential so it cannot be re-offered this session.
    """
    cfg = read_config()
    cloud = cfg.setdefault("cloud", {})
    cloud.pop("deployment_credential", None)
    cloud["deployment_associated"] = True
    write_config(cfg)
    settings.deployment_credential = ""
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
    # Auto-enable secure cookies when relay-connected (HTTPS via Caddy/Cloudflare)
    if settings.gateway_token and not os.environ.get("COOKIE_SECURE"):
        settings.cookie_secure = True


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


def write_config(cfg: dict) -> None:
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
        if cloud.get("disconnected"):
            lines.append("disconnected = true")
        # Deployment credential survives every [cloud] write until the relay
        # accepts it: emitted only while non-empty, and the association marker
        # only once set. A direct install carries neither key. Without this, the
        # fixed-key serializer would drop the credential before the first hello.
        if cloud.get("deployment_credential"):
            lines.append(f'deployment_credential = {_str(cloud["deployment_credential"])}')
        if cloud.get("deployment_associated"):
            lines.append("deployment_associated = true")
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

    path.write_text("\n".join(lines))


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
    cfg = read_config()
    _pkg_root = Path(__file__).parent.parent
    module_dir = _pkg_root / "default_modules"
    currently_enabled: list[str] = cfg.get("modules", {}).get("enabled", [])
    to_add = [n for n in names if n not in currently_enabled]
    if not to_add:
        return False
    install_order = resolve_install_order(list(to_add), module_dir)
    new_modules = [n for n in install_order if n not in currently_enabled]
    if "modules" not in cfg:
        cfg["modules"] = {}
    cfg["modules"]["enabled"] = currently_enabled + new_modules
    write_config(cfg)
    return True

