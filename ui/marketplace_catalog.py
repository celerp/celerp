# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Repo-direct marketplace catalog.

The catalog is public data: index.json in github.com/celerp/community-modules.
The app fetches it straight from the repo when the user opens the Marketplace
tab, treats it as untrusted input (size cap, structure validation, plain-text
rendering only), and caches it locally for offline. The relay is not in the
read path; browsing needs no account.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

CATALOG_URL = (
    "https://raw.githubusercontent.com/celerp/community-modules/main/index.json"
)
MAX_CATALOG_BYTES = 512 * 1024
TIERS = ("official", "verified", "community")

_STR_LIMITS = {
    "id": 64, "name": 80, "description": 300, "author": 80, "license": 60,
    "version": 30, "min_celerp_version": 30, "data_access": 500,
    "network_calls": 500, "repo": 300, "homepage": 300, "feedback": 300,
}
_URL_FIELDS = ("repo", "homepage", "feedback")


def _data_dir() -> Path:
    return Path(os.getenv("CELERP_DATA_DIR") or os.getenv("DATA_DIR") or "./data")


def _cache_path() -> Path:
    return _data_dir() / "marketplace-catalog.json"


def _ack_path() -> Path:
    return _data_dir() / "marketplace-community-ack.json"


def _clean(entry) -> dict | None:
    """Validate one catalog entry; None drops it. Untrusted input: strings are
    length-capped, URLs must be https, unknown tiers are dropped."""
    if not isinstance(entry, dict):
        return None
    out: dict = {}
    for field in ("id", "name", "description", "author", "license"):
        v = entry.get(field)
        if not isinstance(v, str) or not v.strip():
            return None
        out[field] = v.strip()[: _STR_LIMITS[field]]
    if entry.get("tier") not in TIERS:
        return None
    out["tier"] = entry["tier"]
    if not all(c.isascii() and (c.isalnum() or c in "-_") for c in out["id"]):
        return None
    for field in ("version", "min_celerp_version", "data_access", "network_calls",
                  *_URL_FIELDS):
        v = entry.get(field)
        if isinstance(v, str) and v.strip():
            v = v.strip()[: _STR_LIMITS[field]]
            if field in _URL_FIELDS and not v.startswith("https://"):
                continue
            out[field] = v
    for field in ("price_monthly", "price_once"):
        v = entry.get(field)
        if isinstance(v, (int, float)) and v >= 0:
            out[field] = float(v)
    return out


def _parse(raw: bytes) -> list[dict]:
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise ValueError("unsupported catalog format")
    entries = doc.get("modules")
    if not isinstance(entries, list) or len(entries) > 500:
        raise ValueError("bad catalog module list")
    return [m for m in (_clean(e) for e in entries) if m]


async def fetch_catalog() -> tuple[list[dict], bool]:
    """Return (modules, from_cache). Live fetch first; cache on failure.
    Raises if both are unavailable."""
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=False) as c:
            r = await c.get(CATALOG_URL)
            r.raise_for_status()
            if len(r.content) > MAX_CATALOG_BYTES:
                raise ValueError("catalog too large")
            modules = _parse(r.content)
        try:
            _cache_path().parent.mkdir(parents=True, exist_ok=True)
            _cache_path().write_text(
                json.dumps({"fetched_at": time.time(), "modules": modules}),
                encoding="utf-8",
            )
        except OSError:
            pass  # cache is best-effort
        return modules, False
    except Exception:
        cached = read_cached()
        if cached is None:
            raise
        return cached, True


def read_cached() -> list[dict] | None:
    try:
        doc = json.loads(_cache_path().read_text(encoding="utf-8"))
        modules = doc.get("modules")
        return [m for m in (_clean(e) for e in modules) if m] if isinstance(modules, list) else None
    except Exception:
        return None


# ── community opt-in (per instance, stored with a timestamp) ──────────────────

def community_acked() -> bool:
    try:
        return bool(json.loads(_ack_path().read_text(encoding="utf-8")).get("acked_at"))
    except Exception:
        return False


def set_community_ack() -> None:
    _ack_path().parent.mkdir(parents=True, exist_ok=True)
    _ack_path().write_text(json.dumps({"acked_at": time.time()}), encoding="utf-8")
