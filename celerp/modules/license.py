# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""License verification for premium modules.

Called by the module loader at startup for every module loaded from the
``premium_modules/`` directory.  Uses the relay's ``/marketplace/license/verify``
endpoint and caches the result locally to allow a 7-day offline grace period.

Public API
----------
``check_license(slug, relay_url, instance_jwt, cache_dir) -> bool``
    Returns True if the instance has a valid active license for *slug*.

``is_premium_path(pkg_path) -> bool``
    True when the module lives inside a ``premium_modules/`` parent directory.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

_OFFLINE_GRACE_SECONDS: int = 7 * 24 * 3600  # 7 days

# ES256 public key for verifying LIFETIME module licenses OFFLINE. The relay
# holds the matching private key. A lifetime license is an ES256 JWT the relay
# signs once; the app verifies it here against this embedded key with NO
# phone-home, so a purchased module keeps working even if the relay is gone
# (the manifesto clause). Subscription licenses re-check online; only lifetime
# licenses verify offline.
_LICENSE_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAErpSt33cgy+leXLEI0o3MFgCvmlau
LH1ud9XC8JShPMazsJa9Qj2+YHgTkln/zWnLUi79VYaVMknJ8BA8BUCL8Q==
-----END PUBLIC KEY-----"""


def _verify_lifetime_jwt(token: str, slug: str, instance_id: str) -> bool:
    """True if *token* is a valid lifetime license for *slug* issued to THIS
    instance, verified offline against the embedded public key. No network, no
    expiry (lifetime).

    A license carries the instance it was issued to in its `sub` claim, so it is
    only accepted on that instance (`sub == instance_id`). The legitimate owner
    still verifies fully offline."""
    if not token or not instance_id:
        return False
    try:
        from jose import jwt
        claims = jwt.decode(token, _LICENSE_PUBLIC_KEY, algorithms=["ES256"],
                            issuer="celerp-relay")
    except Exception:
        return False
    return (claims.get("kind") == "lifetime"
            and claims.get("mod") == slug
            and claims.get("sub") == instance_id)


def is_premium_path(pkg_path: Path) -> bool:
    """True if *pkg_path* is license-gated: inside a ``premium_modules/`` directory,
    or carrying the marker the marketplace installer drops for a paid module
    (which lands in the regular module dir, not a premium tree)."""
    if any(p.name == "premium_modules" for p in pkg_path.parents):
        return True
    from celerp.modules.importer import PREMIUM_MARKER
    return (pkg_path / PREMIUM_MARKER).exists()


def check_license(
    slug: str,
    relay_url: str,
    instance_jwt: str,
    cache_dir: Path,
    instance_id: str,
) -> bool:
    """Verify that *slug* is licensed for this Celerp instance.

    1. POST to ``relay_url/marketplace/license/verify`` with Bearer JWT.
    2. If call succeeds: write result to ``cache_dir/license_cache/{slug}.json``.
    3. If call fails (offline, network error): fall back to cached result if
       the cache is younger than ``_OFFLINE_GRACE_SECONDS``; otherwise deny.

    Args:
        slug:         Module slug to verify (e.g. ``"celerp-warehousing"``).
        relay_url:    Base URL of the Celerp relay service (no trailing slash).
        instance_jwt: Bearer JWT obtained from relay ``/auth/token``.
        cache_dir:    DATA_DIR for this Celerp instance — cache is stored in a
                      ``license_cache/`` subdirectory.
        instance_id:  This instance's canonical id; a lifetime license only
                      counts when its ``sub`` claim matches it (see
                      ``_verify_lifetime_jwt``).

    Returns:
        True if licensed and active; False otherwise.
    """
    cache_file = Path(cache_dir) / "license_cache" / f"{slug}.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    # ── 0. Offline lifetime license: verify the stored JWT against the embedded
    # public key. If valid, the module is licensed FOREVER with no network and no
    # grace expiry - a purchased module survives the relay being gone. ──────────
    stored = _cache_data(cache_file)
    if stored and _verify_lifetime_jwt(stored.get("license_jwt", ""), slug, instance_id):
        return True

    # ── 1. Try live verification (subscriptions, and first lifetime fetch) ──────
    try:
        licensed, status, kind, ljwt = _verify_remote(slug, relay_url, instance_jwt)
        _write_cache(cache_file, licensed=licensed, status=status,
                     license_kind=kind, license_jwt=ljwt)
        # A freshly fetched lifetime JWT is authoritative and offline-valid.
        if kind == "lifetime" and _verify_lifetime_jwt(ljwt, slug, instance_id):
            return True
        if not licensed:
            log.warning(
                "Premium module %r: license status=%r — not loading", slug, status
            )
        return licensed
    except Exception as exc:
        log.info(
            "Premium module %r: relay unreachable (%s) — falling back to cache",
            slug, exc,
        )

    # ── 2. Offline grace (subscriptions only) ────────────────────────────────
    return _read_cache(cache_file, slug)


def exchange_api_key_for_jwt(relay_url: str, api_key: str) -> str | None:
    """Exchange the permanent gateway api_key for a short-lived instance JWT via
    POST /auth/token - same pattern as celerp.routers.health's async relay
    calls, but synchronous since the module loader runs at process startup
    before the event loop is serving requests. Returns None on any failure
    (network error, invalid key, malformed response) so the caller can fall
    back to skipping the license check rather than crashing startup."""
    if not relay_url or not api_key:
        return None
    url = relay_url.rstrip("/") + "/auth/token"
    body = json.dumps({"api_key": api_key}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        token = data.get("access_token")
        return str(token) if token else None
    except Exception:
        return None


def _verify_remote(slug: str, relay_url: str, jwt: str) -> tuple[bool, str, str, str]:
    """POST to relay: (licensed, status, license_kind, license_jwt). Raises on
    network error."""
    url = relay_url.rstrip("/") + "/marketplace/license/verify"
    body = json.dumps({"slug": slug}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {jwt}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # Invalid JWT — deny immediately, don't fall back to cache
            raise PermissionError(f"Relay rejected JWT for {slug!r}: HTTP {exc.code}") from exc
        raise
    return (bool(data.get("licensed")), str(data.get("status", "unknown")),
            str(data.get("license_kind", "")), str(data.get("license_jwt", "")))


def _cache_data(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, *, licensed: bool, status: str,
                 license_kind: str = "", license_jwt: str = "") -> None:
    payload = {"licensed": licensed, "status": status, "cached_at": time.time(),
               "license_kind": license_kind, "license_jwt": license_jwt}
    try:
        path.write_text(json.dumps(payload))
    except OSError as exc:
        log.debug("Could not write license cache for %s: %s", path.name, exc)


def _read_cache(path: Path, slug: str) -> bool:
    """Return cached license result if within grace period, else False."""
    try:
        data = json.loads(path.read_text())
        age = time.time() - float(data.get("cached_at", 0))
        if age > _OFFLINE_GRACE_SECONDS:
            log.warning(
                "Premium module %r: license cache expired (%d days old) — denying",
                slug, int(age // 86400),
            )
            return False
        licensed = bool(data.get("licensed"))
        if not licensed:
            log.warning(
                "Premium module %r: cached license status=%r — not loading",
                slug, data.get("status"),
            )
        else:
            log.info(
                "Premium module %r: using cached license (age %.1fh)",
                slug, age / 3600,
            )
        return licensed
    except (OSError, json.JSONDecodeError, KeyError):
        log.warning("Premium module %r: no valid license cache — denying", slug)
        return False
