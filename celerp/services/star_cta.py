# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""GitHub-star CTA client.

The relay (celerp.com) is the single source of truth for the star count, the tier
ladder, and the CTA copy. This module only fetches the relay's already-resolved CTA
for a given surface (``medium``) in the UI language (``lang``) and caches it. It never calls GitHub and never
computes tiers or copy.

Determinism: on any relay failure ``get_star_cta`` returns ``None``. Callers render
the static neutral link in that case (``neutral_cta``); they must never fabricate a
count or a tier.
"""
from __future__ import annotations

import time

import httpx

from celerp.config import settings
from celerp.gateway.state import build_handoff_url, relay_http_url

# Cache keyed by surface and language: (medium, lang) -> (fetched_at_monotonic, cta_dict)
_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def _cache_get(key: tuple[str, str]) -> dict | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    fetched_at, value = entry
    if time.monotonic() - fetched_at > settings.star_cta_cache_ttl_s:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: tuple[str, str], value: dict) -> None:
    _cache[key] = (time.monotonic(), value)


def cache_bust() -> None:
    """Drop all cached CTAs (used on config change and in tests)."""
    _cache.clear()


def neutral_cta(medium: str) -> dict:
    """The static, relay-independent CTA: just the link, no count, no tier.

    This is the deterministic offline state - shown when the relay is unreachable.
    """
    return {
        "mode": "neutral",
        "show_count": False,
        "count": None,
        "cta_label": "Star on GitHub",
        "url": build_handoff_url("/github", medium=medium),
    }


async def get_star_cta(medium: str, lang: str) -> dict | None:
    """Return the relay-resolved CTA for ``medium`` with its copy asked for in ``lang``,
    or ``None`` if unavailable.

    ``None`` means "relay unreachable / no data" - the caller renders neutral_cta.
    Returns ``None`` (not neutral) so the caller decides how to degrade.
    """
    if not settings.star_cta_enabled:
        return None

    key = (medium, lang)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    base = relay_http_url()
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{base}/github/cta", params={"medium": medium, "lang": lang})
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None

    _cache_set(key, data)
    return data
