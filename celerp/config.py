# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

import os
import platform
import sys
from pathlib import Path

from pydantic_settings import BaseSettings

_DEFAULT_JWT_SECRET = "dev-secret"
_CONFIG_ENV_VAR = "CELERP_CONFIG"


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp"
    jwt_secret: str = _DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    # Set to "true" to allow the default JWT secret (CI only).
    allow_insecure_jwt: str = "false"
    # Public URL of this Celerp instance (e.g. https://erp.acme.com).
    # When set, share links include ?src= enabling p2p import by recipients.
    # Leave blank on private/LAN installs — bundle download fallback applies.
    celerp_public_url: str = ""
    # Cloud Relay (opt-in - leave blank to disable entirely).
    # Set GATEWAY_TOKEN to activate the persistent WS connection to relay.celerp.com.
    # No value = no connection, no telemetry, no cloud dependency.
    gateway_token: str = ""
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
    # Defaults to ./data (relative to CWD); set DATA_DIR in production.
    data_dir: Path = Path("data")
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

    # Persist to config.toml if it exists (best-effort; silently skip on error)
    try:
        cfg = read_config()
        if cfg:
            cloud = cfg.setdefault("cloud", {})
            cloud["instance_id"] = iid
            write_config(cfg)
    except Exception:
        pass

    return iid


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
    if cloud.get("token") and not settings.gateway_token:
        settings.gateway_token = cloud["token"]
    if cloud.get("instance_id") and not settings.gateway_instance_id:
        settings.gateway_instance_id = cloud["instance_id"]
    if cloud.get("public_url") and not settings.celerp_public_url:
        settings.celerp_public_url = cloud["public_url"]
    if cloud.get("backup_encryption_key") and not settings.backup_encryption_key:
        settings.backup_encryption_key = cloud["backup_encryption_key"]
    # Auto-enable secure cookies when relay-connected (HTTPS via Caddy/Cloudflare)
    if settings.gateway_token and not os.environ.get("COOKIE_SECURE"):
        settings.cookie_secure = True


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
    """Write cfg back to config.toml, including the [modules] section."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    enabled = cfg.get("modules", {}).get("enabled", [])
    enabled_toml = ", ".join(f'"{m}"' for m in enabled)
    lines = [
        "[database]",
        f'url = "{cfg["database"]["url"]}"',
        "",
        "[auth]",
        f'jwt_secret = "{cfg["auth"]["jwt_secret"]}"',
        "",
        "[server]",
        f'api_port = {cfg["server"]["api_port"]}',
        f'ui_port = {cfg["server"]["ui_port"]}',
        "",
        "[cloud]",
        f'token = "{cfg["cloud"]["token"]}"',
        f'instance_id = "{cfg["cloud"].get("instance_id", "")}"',
        f'public_url = "{cfg["cloud"].get("public_url", "")}"',
        f'backup_encryption_key = "{cfg["cloud"].get("backup_encryption_key", "")}"',
        f'tos_version = "{cfg["cloud"].get("tos_version", "")}"',
        "",
        "[storage]",
        f'backend = "{cfg.get("storage", {}).get("backend", "local")}"',
        f's3_endpoint = "{cfg.get("storage", {}).get("s3_endpoint", "")}"',
        f's3_bucket = "{cfg.get("storage", {}).get("s3_bucket", "")}"',
        f's3_access_key = "{cfg.get("storage", {}).get("s3_access_key", "")}"',
        f's3_secret_key = "{cfg.get("storage", {}).get("s3_secret_key", "")}"',
        "",
        "[database_backup]",
        f'previous_url = "{cfg.get("database_backup", {}).get("previous_url", "")}"',
        "",
        "[storage_backup]",
        f'backend = "{cfg.get("storage_backup", {}).get("backend", "")}"',
        f's3_endpoint = "{cfg.get("storage_backup", {}).get("s3_endpoint", "")}"',
        f's3_bucket = "{cfg.get("storage_backup", {}).get("s3_bucket", "")}"',
        f's3_access_key = "{cfg.get("storage_backup", {}).get("s3_access_key", "")}"',
        f's3_secret_key = "{cfg.get("storage_backup", {}).get("s3_secret_key", "")}"',
        "",
        "[modules]",
        f"enabled = [{enabled_toml}]",
        "",
    ]
    path.write_text("\n".join(lines))


def resolve_install_order(names: list[str], module_dir: Path) -> list[str]:
    """Return names + all transitive depends_on deps, in topo order.

    Searches module_dir and premium_modules/ for package manifests.
    """
    import ast as _ast

    _pkg_root = module_dir.parent
    _search_dirs = [module_dir, _pkg_root / "premium_modules"]

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


def set_enabled_modules(names: list[str]) -> None:
    """Idempotently add modules to the config file's enabled list.

    Resolves transitive dependencies and writes the updated config to disk.
    No-op if config file does not exist (non-CLI deployments).
    """
    cfg = read_config()
    if not cfg:
        return
    _pkg_root = Path(__file__).parent.parent
    module_dir = _pkg_root / "default_modules"
    currently_enabled: list[str] = cfg.get("modules", {}).get("enabled", [])
    to_add = [n for n in names if n not in currently_enabled]
    if not to_add:
        return
    install_order = resolve_install_order(list(to_add), module_dir)
    new_modules = [n for n in install_order if n not in currently_enabled]
    if "modules" not in cfg:
        cfg["modules"] = {}
    cfg["modules"]["enabled"] = currently_enabled + new_modules
    write_config(cfg)

