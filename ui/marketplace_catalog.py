# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Marketplace catalog, relay-steered.

The catalog is public data: index.json in github.com/celerp/community-modules.
The app fetches it from the relay (which serves a cached copy; the repo stays
the public source of truth anyone can fork), falling back to the repo directly
and then to the local cache. Shipped clients are long-lived, so the relay
endpoint is the ONE url baked into a release; listings, hashes, and future
download descriptors are all catalog data the server can steer. Fetched only
when the user opens the Marketplace tab, treated as untrusted input (size cap,
structure validation, plain-text rendering only). Browsing needs no account.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from ui.config import RELAY_URL

CATALOG_SOURCES = (
    f"{RELAY_URL}/marketplace/catalog",
    "https://raw.githubusercontent.com/celerp/community-modules/main/index.json",
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
        # bool is a subclass of int in Python: reject True/False so a stray
        # "price_monthly": true does not render as "$1/mo".
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
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
    """Return (modules, from_cache). Relay first, repo second, cache last.
    Raises only when all three are unavailable."""
    for url in CATALOG_SOURCES:
        try:
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=False) as c:
                r = await c.get(url)
                r.raise_for_status()
                if len(r.content) > MAX_CATALOG_BYTES:
                    raise ValueError("catalog too large")
                modules = _parse(r.content)
        except Exception:
            continue
        try:
            # Atomic write: two open Marketplace tabs (or a SIGTERM mid-write)
            # must never leave torn JSON in the cache. Write a temp file on the
            # same directory, then os.replace it into place.
            cache = _cache_path()
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(f".tmp-{os.getpid()}")
            tmp.write_text(
                json.dumps({"fetched_at": time.time(), "modules": modules}),
                encoding="utf-8",
            )
            os.replace(tmp, cache)
        except OSError:
            pass  # cache is best-effort
        return modules, False
    cached = read_cached()
    if cached is None:
        raise ConnectionError("catalog unavailable from all sources")
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


# ── community module download (author repo archive, staged for import) ────────

MAX_MODULE_ARCHIVE_BYTES = 50 * 1024 * 1024


def _staging_dir() -> Path:
    d = _data_dir() / "community-downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _valid_id(module_id: str) -> bool:
    return bool(module_id) and all(
        c.isascii() and (c.isalnum() or c in "-_") for c in module_id
    )


async def download_community_archive(repo_url: str, module_id: str) -> str:
    """Download a community module's repo archive to a staged .zip and return
    its path. The repo is public, author-controlled source; the bytes stay
    untrusted - only the module importer installs them, behind its zip-slip,
    symlink, size, manifest, and reserved-prefix guards."""
    if not _valid_id(module_id):
        raise ValueError("Invalid module id.")
    if not repo_url.startswith("https://"):
        raise ValueError("Module repository URL is not https.")
    archive_url = repo_url.rstrip("/") + "/archive/HEAD.zip"
    buf = bytearray()
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        async with c.stream("GET", archive_url) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_MODULE_ARCHIVE_BYTES:
                    raise ValueError("Module archive is too large.")
    dest = _staging_dir() / f"{module_id}.zip"
    dest.write_bytes(bytes(buf))
    return str(dest)


def read_staged_archive(path: str) -> bytes:
    """Read a previously staged archive, refusing any path outside the staging
    directory so a client-supplied path cannot read arbitrary files."""
    p = Path(path).resolve()
    base = _staging_dir().resolve()
    if p != base and base not in p.parents:
        raise ValueError("Staged archive path is outside the staging directory.")
    return p.read_bytes()
