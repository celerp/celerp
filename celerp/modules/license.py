# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
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


def is_premium_path(pkg_path: Path) -> bool:
    """Return True if *pkg_path* lives inside a ``premium_modules/`` directory."""
    return any(p.name == "premium_modules" for p in pkg_path.parents)


def check_license(
    slug: str,
    relay_url: str,
    instance_jwt: str,
    cache_dir: Path,
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

    Returns:
        True if licensed and active; False otherwise.
    """
    cache_file = Path(cache_dir) / "license_cache" / f"{slug}.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    # ── 1. Try live verification ──────────────────────────────────────────────
    try:
        licensed, status = _verify_remote(slug, relay_url, instance_jwt)
        _write_cache(cache_file, licensed=licensed, status=status)
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

    # ── 2. Offline grace ─────────────────────────────────────────────────────
    return _read_cache(cache_file, slug)


def _verify_remote(slug: str, relay_url: str, jwt: str) -> tuple[bool, str]:
    """POST to relay and return (licensed, status). Raises on network error."""
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
    return bool(data.get("licensed")), str(data.get("status", "unknown"))


def _write_cache(path: Path, *, licensed: bool, status: str) -> None:
    payload = {"licensed": licensed, "status": status, "cached_at": time.time()}
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
