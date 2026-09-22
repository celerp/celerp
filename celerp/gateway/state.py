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
    """Set the canonical instance_id. Called only by GatewayClient on hello_ack.

    An observed CHANGE of a known instance_id resets the commercial-context
    version namespace: the held snapshot belonged to the previous instance, so a
    new instance's context (which may start from a lower version) must not be
    rejected as stale. The initial set from the empty default is not a change -
    it preserves a context loaded from disk at startup.
    """
    global _instance_id, _commercial_context
    if _instance_id and iid != _instance_id:
        _commercial_context = {}
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


_REQUIRED_FEATURE_FLAG_BOOLS = (
    "payments_enabled", "external_db", "external_storage")


def valid_feature_flags(flags) -> bool:
    """Validate the complete relay feature snapshot before applying it."""
    if not isinstance(flags, dict):
        return False
    for key in _REQUIRED_FEATURE_FLAG_BOOLS:
        if not isinstance(flags.get(key), bool):
            return False
    grace = flags.get("grace_period_ends")
    if grace is None:
        return True
    if not isinstance(grace, str):
        return False
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(grace)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def entitlement_snapshot(data) -> tuple[bool, dict] | None:
    """Return the canonical relay entitlement snapshot, or None if malformed."""
    if not isinstance(data, dict):
        return None
    entitled = data.get("connect_entitled")
    flags = data.get("feature_flags")
    if not isinstance(entitled, bool) or not valid_feature_flags(flags):
        return None
    return entitled, dict(flags)


async def apply_feature_flags_async(flags: dict, *, persist: bool = True) -> None:
    """Apply one authoritative feature snapshot and optionally persist it."""
    if not valid_feature_flags(flags):
        raise ValueError("Malformed relay feature_flags")
    set_feature_flags(flags)
    if not persist:
        return
    import asyncio
    from celerp.config_store import merge_packaged_config
    await asyncio.to_thread(
        merge_packaged_config, {"feature_flags": dict(flags)})


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

# A support_email is likewise relay-controlled and reaches a mailto: href. The
# cap is generous but bounds a header-injection payload; the validator below
# rejects anything but a single clean addr-spec.
MAX_SUPPORT_EMAIL_LEN = 254

# The maximum retail_amount an offer may carry, in minor units. A value at or
# above this (or zero, negative, or a bool) is treated as malformed and drops
# the offer. Cloud is the source of truth for the bound; the app enforces the
# same 0 < amount < ceiling range on the values it consumes.
_MAX_RETAIL_AMOUNT = 10 ** 12


def safe_support_url(value) -> str:
    """Return a partner support URL only if it is a canonical, safe https URL;
    otherwise the empty string.

    Rejects: non-strings, anything longer than MAX_SUPPORT_URL_LEN, any value
    urlparse would silently alter (leading/embedded whitespace or C0 control
    characters), embedded userinfo (user:pass@host), any scheme other than
    https, and an empty host. A clean value is returned unchanged.

    The one public support-URL validator: every surface that lets a
    relay-controlled URL reach an href (ingress normalisation, the health
    identity, the settings claim preview) routes through this, so there is one
    source of truth for what counts as a safe support URL.
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


def safe_support_email(value) -> str:
    """Return a partner support email only if it is a single clean addr-spec;
    otherwise the empty string.

    The one public support-email validator, mirroring safe_support_url: every
    surface that lets a relay-controlled email reach a mailto: href routes
    through this. Rejects non-strings, anything longer than
    MAX_SUPPORT_EMAIL_LEN, any whitespace or C0 control character (which blocks
    header-injection payloads carrying CR/LF), and anything without exactly one
    '@' separating a non-empty local part from a non-empty host containing a
    dot. A clean value is returned unchanged.
    """
    if not isinstance(value, str):
        return ""
    if not value or len(value) > MAX_SUPPORT_EMAIL_LEN:
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        return ""
    local, sep, host = value.partition("@")
    if not sep or "@" in host:
        return ""
    if not local or not host or "." not in host:
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
        # Cloud is the source of truth for the bound: a retail amount must be
        # strictly positive and below the ceiling. Zero is not a valid price, so
        # the lower bound rejects it rather than admitting a free offer.
        if amount <= 0 or amount >= _MAX_RETAIL_AMOUNT:
            return None
        # A priced offer must carry the minor-unit exponent and billing interval
        # the price renderer needs: the amount is meaningless without the
        # exponent (amount / 10 ** exponent) and the interval decides the /mo or
        # /yr suffix. A missing or malformed either drops the offer whole.
        exponent = offer.get("currency_exponent")
        if not _valid_int(exponent) or not (0 <= exponent <= 4):
            return None
        if offer.get("billing_interval") not in ("month", "year"):
            return None
    currency = offer.get("currency")
    if currency is not None and not isinstance(currency, str):
        return None
    bullets = offer.get("service_bullets")
    if bullets is not None and not isinstance(bullets, list):
        return None
    return offer


def _validated_subscription(sub):
    """Return the subscription dict when well-formed, else None (dropped whole).

    status is a required non-empty string of at most 64 characters;
    cancel_at_period_end, when present, must be a real bool (not an int);
    current_period_end, when present, must be an ISO-8601 string that parses
    after normalising a trailing Z, the same shape the grace handling reads. A
    malformed block is dropped whole so nothing grants on a half-trusted state.
    """
    if not isinstance(sub, dict):
        return None
    status = sub.get("status")
    if not isinstance(status, str) or not status or len(status) > 64:
        return None
    cancel_at_period_end = sub.get("cancel_at_period_end")
    if cancel_at_period_end is not None and not isinstance(cancel_at_period_end, bool):
        return None
    period_end = sub.get("current_period_end")
    if period_end is not None:
        if not isinstance(period_end, str):
            return None
        from datetime import datetime
        try:
            datetime.fromisoformat(period_end.replace("Z", "+00:00"))
        except ValueError:
            return None
    return sub


def _normalized_implementation(implementation):
    """Return the implementation dict with validated fields, or None when the
    block is unusable. partner_id is required and must be a non-empty string;
    display_name, support_email, and status are optional but must be strings
    when present. A present-but-invalid support_url, or any field of the wrong
    shape, drops the whole block (fail closed) so no malformed partner
    identity survives.
    """
    if not isinstance(implementation, dict):
        return None
    partner_id = implementation.get("partner_id")
    if not isinstance(partner_id, str) or not partner_id:
        return None
    for key in ("display_name", "support_email", "status"):
        value = implementation.get(key)
        if value is not None and not isinstance(value, str):
            return None
    raw = implementation.get("support_url")
    if raw is not None:
        safe = safe_support_url(raw)
        if not safe:
            return None
        implementation = dict(implementation)
        implementation["support_url"] = safe
    return implementation


def _validated_commercial_shape(new) -> dict | None:
    """Return the accepted snapshot (a deep copy with a sanitised
    implementation) when the WHOLE envelope shape is valid, else None. Judges
    shape only - it never consults or advances the held version, so it can
    answer both "would this apply" and "is this a valid already-converged
    shape".

    Validity is all-or-nothing. Any invalidity - a bad envelope shape, a
    partner_managed context without a valid implementation, a supplied offer or
    subscription that fails validation, or a celerp_direct context carrying an
    implementation or offer - rejects the whole envelope and logs a single
    reason line. Nothing is ever partial-applied.
    """
    if not isinstance(new, dict):
        log.warning("Commercial context rejected: payload is not an object.")
        return None
    version = new.get("version")
    if not _valid_int(version):
        log.warning("Commercial context rejected: version missing or not an integer.")
        return None
    schema_version = new.get("schema_version")
    if not _valid_int(schema_version):
        log.warning("Commercial context rejected: schema_version missing or not an integer.")
        return None
    if schema_version > _SUPPORTED_SCHEMA_VERSION:
        log.warning(
            "Commercial context schema_version %s exceeds the supported maximum %s; "
            "this client needs updating. Preserving last-known-good.",
            schema_version, _SUPPORTED_SCHEMA_VERSION)
        return None
    if schema_version < _SUPPORTED_SCHEMA_VERSION:
        log.warning(
            "Commercial context rejected: invalid schema_version %s (below the "
            "supported %s).",
            schema_version, _SUPPORTED_SCHEMA_VERSION)
        return None
    mode = new.get("commercial_mode")
    if mode not in _VALID_COMMERCIAL_MODES:
        log.warning("Commercial context rejected: unrecognised commercial_mode.")
        return None
    for key in ("implementation", "offer", "subscription"):
        value = new.get(key)
        if value is not None and not isinstance(value, dict):
            log.warning("Commercial context rejected: %s is neither null nor an object.", key)
            return None
    raw_offers = new.get("offers", {})
    if raw_offers is None:
        raw_offers = {}
    if not isinstance(raw_offers, dict):
        log.warning("Commercial context rejected: offers is not an object.")
        return None
    normalized_offers: dict[str, dict] = {}
    for tier, offer_value in raw_offers.items():
        if tier not in ("cloud", "ai", "team"):
            log.warning("Commercial context rejected: unrecognised offer tier.")
            return None
        validated = _validated_offer(offer_value)
        if validated is None:
            log.warning("Commercial context rejected: target-tier offer failed validation.")
            return None
        normalized_offers[tier] = copy.deepcopy(validated)

    raw_impl = new.get("implementation")
    normalized_impl = _normalized_implementation(raw_impl) if raw_impl is not None else None
    if mode == "partner_managed":
        if normalized_impl is None:
            log.warning(
                "Commercial context rejected: partner_managed requires a valid "
                "implementation (mode=%s, version=%s).", mode, version)
            return None
    else:  # celerp_direct
        if raw_impl is not None or new.get("offer") is not None or normalized_offers:
            log.warning(
                "Commercial context rejected: celerp_direct must carry no "
                "implementation or offer (mode=%s, version=%s).", mode, version)
            return None
    raw_offer = new.get("offer")
    if raw_offer is not None and _validated_offer(raw_offer) is None:
        log.warning(
            "Commercial context rejected: offer failed validation (version=%s).", version)
        return None
    raw_subscription = new.get("subscription")
    if raw_subscription is not None and _validated_subscription(raw_subscription) is None:
        log.warning(
            "Commercial context rejected: subscription failed validation (version=%s).",
            version)
        return None

    accepted = copy.deepcopy(new)
    # Carry the normalized implementation (a sanitised support_url) into the
    # stored snapshot. The block is present and valid here for partner_managed;
    # celerp_direct carries none.
    if normalized_impl is not None:
        accepted["implementation"] = normalized_impl
    accepted["offers"] = normalized_offers
    return accepted


def set_commercial_context(new: dict) -> bool:
    """Validate a relay-pushed commercial context and, if the WHOLE envelope is
    valid and strictly newer, replace the held model. Returns whether it was
    accepted.

    This is the single acceptance gate: both inbound branches (hello_ack and
    commercial_updated) route through it. Acceptance is all-or-nothing (see
    ``_validated_commercial_shape``): the last-known-good model AND its version
    are preserved unchanged on any invalidity, so a rejected envelope never
    advances the held version and a corrected retransmission at the same version
    is accepted. Called by GatewayClient.
    """
    global _commercial_context
    accepted = _validated_commercial_shape(new)
    if accepted is None:
        return False

    # The whole envelope is valid; only now does the strictly-newer version gate
    # decide whether it supersedes the held snapshot.
    version = accepted["version"]
    current = _commercial_context.get("version")
    if _valid_int(current) and version <= current:
        log.warning(
            "Commercial context rejected: version %s is not newer than held %s.",
            version, current)
        return False

    _commercial_context = accepted
    return True


def _persist_commercial_context(ctx: dict) -> None:
    """Write the already-validated commercial context to whichever local store
    this platform uses, so an offline restart presents the last-known-good
    partner identity rather than the neutral default.

    Packaged (Electron, CELERP_DATA_DIR set): the 'commercial_context' key of
    celerp-config.json via the shared atomic writer. Self-hosted (no
    CELERP_DATA_DIR): the [cloud] commercial_context_json key of config.toml, the
    only cross-restart store a self-hosted install has. Both are best-effort: a
    write failure leaves the prior on-disk state untouched and never raises into
    the acceptance path.
    """
    import os
    if os.environ.get("CELERP_DATA_DIR", ""):
        from celerp.config_store import merge_packaged_config
        merge_packaged_config({"commercial_context": ctx})
        return
    import json
    from celerp.config import _update_cloud_config
    try:
        _update_cloud_config(lambda cloud: cloud.__setitem__(
            "commercial_context_json", json.dumps(ctx, separators=(",", ":"))))
    except Exception as exc:
        log.debug("Gateway: self-hosted commercial-context persist failed: %s", exc)


def _accept_commercial_context(ctx: dict) -> tuple[str, dict | None]:
    """Apply only in-memory state; return a snapshot when disk persistence is needed."""
    if _validated_commercial_shape(ctx) is None:
        return "rejected", None
    if set_commercial_context(ctx):
        return "applied", get_commercial_context()
    return "converged", None


def apply_commercial_context(ctx: dict) -> str:
    """Synchronous adapter for non-async callers and tests."""
    status, snapshot = _accept_commercial_context(ctx)
    if snapshot is not None:
        _persist_commercial_context(snapshot)
    return status


_commercial_apply_lock = None
_commercial_apply_loop = None


def _async_commercial_apply_lock():
    """One serialization lock per event loop, created lazily for test-loop safety."""
    import asyncio
    global _commercial_apply_lock, _commercial_apply_loop
    loop = asyncio.get_running_loop()
    if _commercial_apply_loop is not loop:
        _commercial_apply_loop = loop
        _commercial_apply_lock = asyncio.Lock()
    return _commercial_apply_lock


async def apply_commercial_context_async(ctx: dict) -> str:
    """Apply and persist in version order without blocking the event loop."""
    import asyncio
    async with _async_commercial_apply_lock():
        status, snapshot = _accept_commercial_context(ctx)
        if snapshot is not None:
            await asyncio.to_thread(_persist_commercial_context, snapshot)
        return status


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


def get_offer(tier: str | None = None) -> dict | None:
    """Return the current or a target-tier partner offer."""
    if tier is not None:
        offers = _commercial_context.get("offers")
        offer = offers.get(tier) if isinstance(offers, dict) else None
    else:
        offer = _commercial_context.get("offer")
    return copy.deepcopy(offer) if isinstance(offer, dict) else None


def load_commercial_context() -> None:
    """Load the last-known-good commercial context from the local cache at
    startup, ungated by the relay connection so an offline restart still
    presents the cached partner_managed identity instead of the neutral default.

    Packaged (Electron, CELERP_DATA_DIR set): reads the 'commercial_context' key
    from <CELERP_DATA_DIR>/celerp-config.json. Self-hosted (no CELERP_DATA_DIR):
    reads and decodes the [cloud] commercial_context_json key from config.toml.
    Both route the decoded object through set_commercial_context(), so a missing
    store, missing key, corrupt JSON, or an invalid/stale shape leaves the
    neutral empty model in place; it never fabricates a partner.
    """
    import os
    import json
    data_dir = os.environ.get("CELERP_DATA_DIR", "")
    if data_dir:
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
        return
    # Self-hosted: the durable store is the fixed [cloud] commercial_context_json
    # config key, serialised as compact JSON of the already-validated context.
    try:
        from celerp.config import read_config
        raw = read_config().get("cloud", {}).get("commercial_context_json", "")
        if not raw:
            return
        cached = json.loads(raw)
        if isinstance(cached, dict):
            set_commercial_context(cached)
    except Exception as exc:
        log.debug("Gateway: self-hosted commercial-context cache unreadable; "
                  "using neutral default: %s", exc)


def grace_ends_in_future(value) -> bool:
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
    # Grace only applies to a resource this install actually configured: a
    # DB-only install is never classed as storage grace (and vice versa), and an
    # install with neither external target never enters grace at all.
    in_grace = (bool(external_db_url) and grace_ends_in_future(grace_period_ends)
                and not external_db_entitled)
    storage_in_grace = (bool(has_external_storage) and grace_ends_in_future(grace_period_ends)
                        and not external_storage_entitled)
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


def get_local_infra_state() -> dict:
    """Return the non-secret Team infrastructure state for the UI, on both
    packaged and self-hosted installs, exposing only booleans and dates.

    Keys, and only these keys:
        has_external_url, has_external_storage, external_db_entitled,
        external_storage_entitled, grace_period_ends, in_grace, storage_in_grace

    Packaged (Electron, CELERP_DATA_DIR set): projected from
    get_packaged_db_state(), which reads celerp-config.json straight from disk so
    the cold-boot and relay-disconnected cases are served without waiting on a
    relay flag push. Self-hosted (no CELERP_DATA_DIR): the configured external
    DB/storage booleans come from the runtime Settings object, and the
    entitlement/grace flags from the same feature-flags cache the packaged branch
    reads (get_feature_flags()), so there is one entitlement source, not a third.

    Never exposes a DB URL, password, S3 endpoint, access key, or secret key -
    only whether an external target is configured. This is the visibility/
    recovery source; establishing or probing paid infra keeps a live-entitlement
    gate elsewhere.
    """
    import os
    if os.environ.get("CELERP_DATA_DIR", ""):
        packaged = get_packaged_db_state()
        return {
            "has_external_url": packaged["has_external_url"],
            "has_external_storage": packaged["has_external_storage"],
            "external_db_entitled": packaged["external_db_entitled"],
            "external_storage_entitled": packaged["external_storage_entitled"],
            "grace_period_ends": packaged["grace_period_ends"],
            "in_grace": packaged["in_grace"],
            "storage_in_grace": packaged["storage_in_grace"],
        }
    # Self-hosted: configured targets from runtime settings (booleans only),
    # entitlement/grace from the shared feature-flags cache. The external-DB
    # signal is the explicit, default-off external_db opt-in, never inferred from
    # database_url: every ordinary install points database_url at its own
    # Postgres, which is not the same as opting into customer-owned Team
    # infrastructure, and inferring it there both fabricates Team-infra
    # visibility and depends on the exact runtime DB URL.
    from celerp.config import settings
    has_external_url = bool(settings.external_db)
    has_external_storage = settings.storage_backend == "s3" or bool(
        settings.storage_s3_endpoint or settings.storage_s3_bucket
        or settings.storage_s3_access_key)
    flags = get_feature_flags()
    external_db_entitled = bool(flags.get("external_db"))
    external_storage_entitled = bool(flags.get("external_storage"))
    grace_period_ends = flags.get("grace_period_ends")
    # Grace only applies to a resource this install actually configured (see the
    # packaged branch): the external-DB and external-storage windows are
    # independent, and neither opens for a resource this install never set up.
    in_grace = (bool(has_external_url) and grace_ends_in_future(grace_period_ends)
                and not external_db_entitled)
    storage_in_grace = (bool(has_external_storage) and grace_ends_in_future(grace_period_ends)
                        and not external_storage_entitled)
    return {
        "has_external_url": bool(has_external_url),
        "has_external_storage": bool(has_external_storage),
        "external_db_entitled": external_db_entitled,
        "external_storage_entitled": external_storage_entitled,
        "grace_period_ends": grace_period_ends,
        "in_grace": in_grace,
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


# Every relay HTTP leg opens its socket under a short connect deadline and, when
# the connect phase itself fails, opens a fresh socket and tries again. A lost
# SYN on a path that never retransmits it (observed on macOS) then costs one
# connect deadline instead of the whole leg. The last attempt gets whatever the
# leg has left, so a slow but working path keeps a connect window at least as
# long as it had before. Retries stop at the connect phase: once a request has
# been sent, its response (of any status) is final.
RELAY_CONNECT_TIMEOUT_S = 2.0
RELAY_CONNECT_ATTEMPTS = 3


def relay_timeout(total_s: float, connect_s: float = RELAY_CONNECT_TIMEOUT_S):
    """httpx timeout for a relay leg: total_s for read/write/pool, a short
    connect deadline so a silently lost connection attempt fails fast."""
    import httpx

    return httpx.Timeout(total_s, connect=connect_s)


def relay_connect_deadline(total_s: float, attempt: int) -> float:
    """Connect deadline for one attempt of a leg with total_s seconds: the short
    deadline for every attempt but the last, which takes the rest of the leg."""
    if attempt < RELAY_CONNECT_ATTEMPTS:
        return RELAY_CONNECT_TIMEOUT_S
    spent = RELAY_CONNECT_TIMEOUT_S * (RELAY_CONNECT_ATTEMPTS - 1)
    return max(RELAY_CONNECT_TIMEOUT_S, total_s - spent)


async def with_relay_client(total_s: float, op):
    """Run op(client) within one true wall-clock budget, reopening on connect failure.

    Only httpx.ConnectError and httpx.ConnectTimeout are retried, and only up to
    RELAY_CONNECT_ATTEMPTS: both are raised before any request bytes leave the
    machine, so a retry can never duplicate a request the relay already saw.
    The outer asyncio.wait_for owns the total wall clock; per-attempt httpx
    read/write/pool values stay deterministic at total_s while connect gets the
    short retry budget. Every other outcome propagates from the first attempt
    that produced it.
    """
    import asyncio

    import httpx

    async def _run():
        for attempt in range(1, RELAY_CONNECT_ATTEMPTS + 1):
            connect_s = min(
                relay_connect_deadline(total_s, attempt), total_s)
            try:
                timeout = relay_timeout(total_s, connect_s=connect_s)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    return await op(client)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt == RELAY_CONNECT_ATTEMPTS:
                    raise
                log.debug("Relay connect attempt %d failed (%s); reopening.",
                          attempt, type(exc).__name__)

    try:
        return await asyncio.wait_for(_run(), timeout=total_s)
    except asyncio.TimeoutError as exc:
        raise httpx.ReadTimeout(
            f"relay operation exceeded {total_s:.1f}s wall-clock budget") from exc


# Transient transport failures (slow first network, relay restarting) are retried;
# any HTTP response of any status is final. Single source for every relay POST that
# needs this shape (auto-activate, deployment association).
_RELAY_POST_RETRY_DELAYS = (0, 5, 30)
_RELAY_POST_TIMEOUT_S = 10.0


async def relay_post_with_retry(url: str, json_body: dict):
    """POST json_body to url, retrying only transient transport failures.

    Returns the httpx.Response (of any status) on the first attempt that gets one,
    or None when every attempt hit a transport error. httpx's own logger is quieted
    for the duration because its records can carry request detail, and some callers
    send a credential in the body; the exception value is never logged for the same
    reason (an httpx error repr can embed the request body).
    """
    import asyncio

    import httpx

    async def _post(client):
        return await client.post(url, json=json_body)