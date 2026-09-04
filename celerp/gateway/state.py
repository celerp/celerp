# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Gateway session-token state - internal only.

This module is in _PROTECTED_BSL_INTERNALS. Third-party modules MUST NOT import it.
The session token is issued by relay.celerp.com after the hello_ack handshake
and is required for cloud-gated endpoints (/ai/*, /backup/*, /connectors/*).
"""
from __future__ import annotations

import copy
import logging

log = logging.getLogger(__name__)

_session_token: str = ""
_subscription_tier: str = ""
_subscription_status: str = ""
_feature_flags: dict = {}
_commercial_context: dict = {}
_instance_id: str = ""

# The client understands commercial-context envelopes up to this schema_version.
# A higher one is rejected (last-known-good preserved) rather than partial-parsed,
# so partner identity is never misrepresented from an unknown shape.
_SUPPORTED_SCHEMA_VERSION = 1
_VALID_COMMERCIAL_MODES = ("celerp_direct", "partner_managed")


def get_instance_id() -> str:
    """Return the relay-canonical instance_id (empty string if not connected)."""
    return _instance_id


def set_instance_id(iid: str) -> None:
    """Set the canonical instance_id. Called only by GatewayClient on hello_ack."""
    global _instance_id
    _instance_id = iid


def get_session_token() -> str:
    """Return the current live session token (empty string if not connected)."""
    return _session_token


def set_session_token(token: str) -> None:
    """Set the current session token. Called only by GatewayClient."""
    global _session_token
    _session_token = token


def set_subscription_state(tier: str, status: str) -> None:
    """Update local subscription state from gateway WS push."""
    global _subscription_tier, _subscription_status
    _subscription_tier = tier
    _subscription_status = status


def get_subscription_state() -> tuple[str, str]:
    """Return (tier, status) of the current subscription."""
    return _subscription_tier, _subscription_status


def set_feature_flags(flags: dict) -> None:
    """Store feature flags received from gateway. Called by GatewayClient."""
    global _feature_flags
    _feature_flags = dict(flags)


def get_feature_flags() -> dict:
    """Return a copy of the current feature flags."""
    return dict(_feature_flags)


def _valid_int(value) -> bool:
    """A real JSON integer, not a bool (True/False are int subclasses and must
    never pass as a version or schema_version)."""
    return isinstance(value, int) and not isinstance(value, bool)


# A support_url is a relay-controlled string that reaches an href. urlparse
# silently strips leading/embedded whitespace and control characters and admits
# userinfo, so a boolean "looks like a URL" check is not enough: an attacker who
# controls the partner record could smuggle a javascript:/data: payload or a
# credentials-bearing host past a naive check. The validator below rejects any
# non-canonical value outright rather than trying to sanitise it.
MAX_SUPPORT_URL_LEN = 2048

# The maximum retail_amount an offer may carry, in minor units. A value at or
# above this (or negative, or a bool) is treated as malformed and drops the offer.
_MAX_RETAIL_AMOUNT = 10 ** 12


def _safe_support_url(value) -> str:
    """Return a partner support URL only if it is a canonical, safe https URL;
    otherwise the empty string.

    Rejects: non-strings, anything longer than MAX_SUPPORT_URL_LEN, any value
    urlparse would silently alter (leading/embedded whitespace or C0 control
    characters), embedded userinfo (user:pass@host), any scheme other than
    https, and an empty host. A clean value is returned unchanged.
    """
    from urllib.parse import urlparse

    if not isinstance(value, str):
        return ""
    if len(value) > MAX_SUPPORT_URL_LEN:
        return ""
    # urlparse strips these silently, so a downstream re-parse would disagree
    # with the value we validated. Reject rather than canonicalise.
    if value != value.strip():
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        return ""
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme != "https":
        return ""
    if not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return value


def _validated_offer(offer):
    """Return the offer dict when every field it carries is well-formed, else
    None. Degrades honestly: a malformed offer is dropped whole rather than
    partially trusted, so no fabricated price or currency can reach a surface.
    """
    if not isinstance(offer, dict):
        return None
    amount = offer.get("retail_amount")
    if amount is not None:
        if isinstance(amount, bool) or not isinstance(amount, int):
            return None
        if amount < 0 or amount >= _MAX_RETAIL_AMOUNT:
            return None
    currency = offer.get("currency")
    if currency is not None and not isinstance(currency, str):
        return None
    bullets = offer.get("service_bullets")
    if bullets is not None and not isinstance(bullets, list):
        return None
    return offer


def _normalized_implementation(implementation):
    """Return the implementation dict with a validated support_url, or None when
    the block is unusable. A present-but-invalid support_url drops the whole
    block (fail closed) so no partner identity carrying a hostile URL survives.
    """
    if not isinstance(implementation, dict):
        return None
    raw = implementation.get("support_url")
    if raw is not None:
        safe = _safe_support_url(raw)
        if not safe:
            return None
        implementation = dict(implementation)
        implementation["support_url"] = safe
    return implementation


def set_commercial_context(new: dict) -> bool:
    """Validate a relay-pushed commercial context and, if it is a strictly-newer
    well-formed update, replace the held model. Returns whether it was accepted.

    This is the single acceptance gate: both inbound branches (hello_ack and
    commercial_updated) route through it. On any rejection the last-known-good
    model is preserved unchanged and a single reason line is logged; the context
    is never partial-applied. Called by GatewayClient.
    """
    global _commercial_context
    if not isinstance(new, dict):
        log.warning("Commercial context rejected: payload is not an object.")
        return False
    version = new.get("version")
    if not _valid_int(version):
        log.warning("Commercial context rejected: version missing or not an integer.")
        return False
    current = _commercial_context.get("version")
    if _valid_int(current) and version <= current:
        log.warning(
            "Commercial context rejected: version %s is not newer than held %s.",
            version, current)
        return False
    schema_version = new.get("schema_version")
    if not _valid_int(schema_version):
        log.warning("Commercial context rejected: schema_version missing or not an integer.")
        return False
    if schema_version > _SUPPORTED_SCHEMA_VERSION:
        log.warning(
            "Commercial context schema_version %s exceeds the supported maximum %s; "
            "this client needs updating. Preserving last-known-good.",
            schema_version, _SUPPORTED_SCHEMA_VERSION)
        return False
    if schema_version < _SUPPORTED_SCHEMA_VERSION:
        log.warning(
            "Commercial context rejected: invalid schema_version %s (below the "
            "supported %s).",
            schema_version, _SUPPORTED_SCHEMA_VERSION)
        return False
    if new.get("commercial_mode") not in _VALID_COMMERCIAL_MODES:
        log.warning("Commercial context rejected: unrecognised commercial_mode.")
        return False
    for key in ("implementation", "offer"):
        value = new.get(key)
        if value is not None and not isinstance(value, dict):
            log.warning("Commercial context rejected: %s is neither null nor an object.", key)
            return False
    accepted = copy.deepcopy(new)
    # Normalize the relay-controlled sub-objects at ingress: a malformed
    # support_url or offer drops that block whole (fail closed) while the
    # envelope is still accepted so the version advances. A later egress guard
    # backstops caches written by an older, pre-validation binary.
    normalized_impl = _normalized_implementation(accepted.get("implementation"))
    if normalized_impl is None:
        accepted.pop("implementation", None)
    else:
        accepted["implementation"] = normalized_impl
    if _validated_offer(accepted.get("offer")) is None:
        accepted.pop("offer", None)
    _commercial_context = accepted
    return True


def get_commercial_context() -> dict:
    """Return a copy of the current commercial-context model (empty when none
    has been accepted)."""
    return copy.deepcopy(_commercial_context)


def get_commercial_mode() -> str:
    """Return the current commercial mode, defaulting to the neutral
    'celerp_direct' when no context has been accepted."""
    return _commercial_context.get("commercial_mode", "celerp_direct")


def get_partner_identity() -> dict | None:
    """Return a copy of the partner implementation object, or None when the
    install is not partner-managed."""
    implementation = _commercial_context.get("implementation")
    return copy.deepcopy(implementation) if isinstance(implementation, dict) else None


def get_offer() -> dict | None:
    """Return a copy of the partner offer object, or None when none is set."""
    offer = _commercial_context.get("offer")
    return copy.deepcopy(offer) if isinstance(offer, dict) else None


def load_commercial_context() -> None:
    """Load the last-known-good commercial context from the local cache at
    startup, ungated by the relay connection so an offline restart still
    presents the cached partner_managed identity instead of the neutral default.

    Reads the 'commercial_context' key from <CELERP_DATA_DIR>/celerp-config.json.
    A missing data dir, missing file, missing key, or corrupt JSON leaves the
    neutral empty model in place; it never fabricates a partner.
    """
    import os
    import json
    data_dir = os.environ.get("CELERP_DATA_DIR", "")
    if not data_dir:
        return
    config_path = os.path.join(data_dir, "celerp-config.json")
    if not os.path.exists(config_path):
        return
    try:
        with open(config_path) as f:
            existing = json.load(f)
        cached = existing.get("commercial_context")
        if isinstance(cached, dict):
            set_commercial_context(cached)
    except Exception as exc:
        log.debug("Gateway: commercial-context cache unreadable; using neutral default: %s", exc)


def _grace_ends_in_future(value) -> bool:
    """True when an ISO-8601 grace_period_ends timestamp is still in the future.

    A missing or unparseable value is treated as expired (False), so a corrupt
    flag can never keep a lapsed install in grace.
    """
    if not value:
        return False
    from datetime import datetime, timezone
    try:
        ends = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    return ends > datetime.now(timezone.utc)


def get_packaged_db_state() -> dict:
    """Read the packaged app's database mode straight from celerp-config.json on
    disk, mirroring load_commercial_context's read pattern.

    This deliberately does NOT use the in-memory get_feature_flags(): those are
    empty until the relay pushes flags, which is exactly wrong for the cold-boot,
    after-grace, and relay-disconnected cases this state must serve. Only a
    has_external_url boolean is exposed, never the URL string, which holds the
    database password.

    Returns {db_mode, has_external_url, external_db_entitled, in_grace,
    grace_period_ends, storage_mode, has_external_storage,
    external_storage_entitled, storage_in_grace}. A missing data dir, missing
    file, or corrupt JSON degrades to the neutral local state, never an
    exception. Only booleans are exposed for the external targets, never the URL
    or the S3 secret, both of which hold credentials.
    """
    import os
    import json
    db_mode = "local"
    external_db_url = ""
    storage_mode = "local"
    has_external_storage = False
    flags: dict = {}
    data_dir = os.environ.get("CELERP_DATA_DIR", "")
    if data_dir:
        config_path = os.path.join(data_dir, "celerp-config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    existing = json.load(f)
                db_mode = existing.get("db_mode", "local") or "local"
                external_db_url = existing.get("external_db_url", "") or ""
                storage_mode = existing.get("storage_mode", "local") or "local"
                has_external_storage = storage_mode == "s3" or any(
                    existing.get(k)
                    for k in ("storage_s3_endpoint", "storage_s3_bucket", "storage_s3_access_key")
                )
                raw_flags = existing.get("feature_flags")
                if isinstance(raw_flags, dict):
                    flags = raw_flags
            except Exception as exc:
                log.debug("Gateway: packaged db-state unreadable; using neutral default: %s", exc)
    external_db_entitled = bool(flags.get("external_db"))
    external_storage_entitled = bool(flags.get("external_storage"))
    grace_period_ends = flags.get("grace_period_ends")
    in_grace = _grace_ends_in_future(grace_period_ends) and not external_db_entitled
    storage_in_grace = _grace_ends_in_future(grace_period_ends) and not external_storage_entitled
    return {
        "db_mode": db_mode,
        "has_external_url": bool(external_db_url),
        "external_db_entitled": external_db_entitled,
        "in_grace": in_grace,
        "grace_period_ends": grace_period_ends,
        "storage_mode": storage_mode,
        "has_external_storage": bool(has_external_storage),
        "external_storage_entitled": external_storage_entitled,
        "storage_in_grace": storage_in_grace,
    }


# ── Relay connection helpers (single source of truth) ────────────────────────
# All relay HTTP calls use these. Never inline these values elsewhere.

def relay_http_url() -> str:
    """Derive the relay HTTP base URL from gateway settings.

    Handles both explicit gateway_http_url config and WS-URL derivation.
    Single source of truth - used by backup, ai/quota, and any future module.
    """
    from celerp.config import settings
    if settings.gateway_http_url:
        return settings.gateway_http_url.rstrip("/")
    url = settings.gateway_url
    url = url.replace("wss://", "https://").replace("ws://", "http://")
    if "/ws/" in url:
        url = url.rsplit("/ws/", 1)[0]
    return url.rstrip("/")


async def fetch_relay_bearer(http_client) -> str:
    """Exchange the instance API key (gateway_token) for a short-lived relay
    bearer JWT via POST /auth/token.

    Single source of the relay auth handshake every relay REST call needs.
    Callers pass their own httpx client so they own the timeout and connection
    lifecycle, and reuse it for the follow-up request. Raises RuntimeError on a
    non-200 so each caller degrades in one place.
    """
    from celerp.config import settings
    resp = await http_client.post(
        f"{relay_http_url()}/auth/token", json={"api_key": settings.gateway_token})
    if resp.status_code != 200:
        raise RuntimeError(f"relay auth failed ({resp.status_code})")
    return resp.json()["access_token"]


def _launch_mode() -> str | None:
    """The launch channel, when the launcher told us one. Electron sets
    CELERP_MODE=desktop; a headless service sets headless. A bare or dev run
    reports nothing rather than guessing: the relay treats a real install with
    no channel as pypi, and dev builds are already excluded by version."""
    import os
    return os.environ.get("CELERP_MODE")


def activate_payload(instance_id: str, *, first_boot: bool | None = None) -> dict:
    """Build the /auth/activate request body.

    Single source of truth for every activation call site (startup probe,
    Cloud settings, claim-by-email), so the relay always learns version,
    platform, and launch mode. first_boot is only known by the startup probe.
    """
    import platform as _platform

    from celerp import __version__

    payload = {
        "instance_id": instance_id,
        "version": __version__,
        "platform": _platform.system(),
        "mode": _launch_mode(),
    }
    if first_boot is not None:
        payload["first_boot"] = first_boot
    return payload


def relay_session_headers() -> dict[str, str]:
    """Return X-Session-Token + X-Instance-ID headers for relay REST calls.

    Always uses the relay-canonical instance_id (set on hello_ack), with the
    config value as fallback. The relay keys its session table on the canonical
    id — using the config id directly causes 401 when they differ.
    """
    from celerp.config import settings
    return {
        "X-Session-Token": _session_token,
        "X-Instance-ID": _instance_id or settings.gateway_instance_id,
    }


HANDOFF_BASE = "https://celerp.com"


def build_handoff_url(path: str, *, medium: str = "inapp", lead: str = "", extra: str = "") -> str:
    """Single source of truth for celerp.com handoff links (subscribe, github, ...).

    Keeps the format identical everywhere: an optional ``lead`` param first (e.g.
    ``instance_id=...``, so attribution tags never bury it), then the UTM tags
    (``utm_source=app`` + the caller's ``medium``), then any ``extra`` params.
    Everything travels as query params, never fragments - fragments are invisible
    to the server, so they can't attribute which CTA drove the click.
    """
    params = ([lead] if lead else []) + [f"utm_source=app&utm_medium={medium}"]
    if extra:
        params.append(extra.lstrip("?&"))
    return f"{HANDOFF_BASE}{path}?{'&'.join(params)}"


def build_subscribe_url(instance_id: str = "", *, topup: bool = False, extra: str = "") -> str:
    """In-app celerp.com/subscribe handoff URL. Thin caller of build_handoff_url.

    ``topup=True`` selects the /subscribe/topup variant. Callers resolve the instance
    id however they need (the gateway id, ``ensure_instance_id()``, or a payload value).
    """
    path = "/subscribe/topup" if topup else "/subscribe"
    lead = f"instance_id={instance_id}" if instance_id else ""
    return build_handoff_url(path, medium="inapp", lead=lead, extra=extra)


def build_commercial_handoff(instance_id: str, intent: str, sku: str = "") -> str:
    """Single policy point resolving every core-app commercial CTA to its correct
    destination, layered above the URL builders (never changing their signatures).

    Keyed on the install's commercial mode and the requested sku:
      - partner_managed: the partner's support URL when set, else the Enterprise
        acquisition route. Never a direct Celerp checkout, so a partner-managed
        install can never be sent to self-serve billing.
      - a team sku (direct install, no partner): the Enterprise route; the app
        never emits a direct plan=team checkout.
      - celerp_direct with a cloud/ai sku: the same direct subscribe URL the app
        has always produced for that plan (behavior-preserving).
      - celerp_direct with an empty or unknown sku: the generic subscribe URL.

    ``intent`` is the acquisition intent the CTA carries: "subscribe" for an
    upgrade/subscribe CTA, "topup" for a credit top-up. It selects the direct
    variant only on the celerp_direct path; a partner-managed or unknown mode
    never reaches a direct checkout regardless of intent.

    Fails closed: only the explicit ``celerp_direct`` mode reaches a direct
    subscribe URL. partner_managed routes to the partner support URL (re-validated
    at egress) or the Enterprise route; any other or unknown mode routes to
    Enterprise. The returned URL is always non-empty, so callers need no per-site
    empty-href guard.
    """
    mode = get_commercial_mode()
    if mode == "partner_managed":
        # Egress re-validation: an auto-updated binary may read an on-disk cache
        # written by a prior binary that predates the ingress guard, so trust the
        # stored support_url only after re-checking it here too.
        support_url = _safe_support_url((get_partner_identity() or {}).get("support_url"))
        if support_url:
            return support_url
        return _enterprise_handoff(instance_id)
    if mode != "celerp_direct":
        return _enterprise_handoff(instance_id)
    if sku == "team":
        return _enterprise_handoff(instance_id)
    if intent == "topup":
        return build_subscribe_url(instance_id, topup=True)
    if sku in ("cloud", "ai"):
        return build_subscribe_url(instance_id, extra=f"plan={sku}")
    return build_subscribe_url(instance_id)


def _enterprise_handoff(instance_id: str) -> str:
    """The Enterprise/partner acquisition route, attributed to the instance."""
    lead = f"instance_id={instance_id}" if instance_id else ""
    return build_handoff_url("/enterprise", medium="inapp", lead=lead)


def enterprise_url(instance_id: str = "") -> str:
    """Public entry point for the Enterprise/partner acquisition route."""
    return _enterprise_handoff(instance_id)
