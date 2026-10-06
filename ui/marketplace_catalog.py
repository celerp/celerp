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
import re
import time
from pathlib import Path

import httpx

from celerp.services import staged_downloads
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
    # Module dependencies: the ids of other modules this one needs. Same id
    # grammar as the module's own id, so a buyer sees what else a listing pulls
    # in before purchasing. Absent or empty drops the field (rendered "--").
    deps = entry.get("depends_on")
    if isinstance(deps, list):
        clean_deps = [
            d.strip()[: _STR_LIMITS["id"]]
            for d in deps
            if isinstance(d, str) and d.strip()
            and all(c.isascii() and (c.isalnum() or c in "-_") for c in d.strip())
        ]
        if clean_deps:
            out["depends_on"] = clean_deps[:50]
    # The commit a community listing pins: downloads fetch exactly this code.
    commit = entry.get("commit")
    if _valid_commit(commit):
        out["commit"] = commit
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

_GITHUB_REPO = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)")
_STAGED_OWNER = re.compile(r"([A-Za-z0-9_-]+)-[0-9a-f]{40}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


def _staging_dir() -> Path:
    d = _data_dir() / "community-downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


class DownloadRefused(ValueError):
    """A download or import Celerp will not carry out; ``key`` names the message
    the user is shown."""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def _valid_commit(commit) -> bool:
    """A full 40-character lowercase hex commit id; branch names, HEAD and short
    ids are not."""
    return isinstance(commit, str) and _COMMIT.fullmatch(commit) is not None


def _valid_id(module_id: str) -> bool:
    return bool(module_id) and all(
        c.isascii() and (c.isalnum() or c in "-_") for c in module_id
    )


def _archive_url(repo_url, commit) -> str:
    """The GitHub archive of ``commit`` for a listing whose source is a GitHub
    repository, given exactly as https://github.com/<owner>/<repo>."""
    m = _GITHUB_REPO.fullmatch(repo_url) if isinstance(repo_url, str) else None
    if m is None or m.group(2).strip(".") == "" or m.group(2).lower().endswith(".git"):
        raise DownloadRefused("marketplace.download_not_github")
    if not _valid_commit(commit):
        raise DownloadRefused("marketplace.download_unpinned")
    return f"https://codeload.github.com/{m.group(1)}/{m.group(2)}/zip/{commit}"


async def download_community_archive(repo_url: str, commit: str, module_id: str) -> str:
    """Download the commit a community listing pins from the module's GitHub repo
    and return the token of that download, which Import names to install exactly
    these bytes. The repo is public, author-controlled source; the bytes stay
    untrusted - only the module importer installs them, behind its zip-slip,
    symlink, size, manifest, and reserved-prefix guards."""
    if not _valid_id(module_id):
        raise ValueError("Invalid module id.")
    archive_url = _archive_url(repo_url, commit)
    buf = bytearray()
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as c:
        async with c.stream("GET", archive_url) as r:
            if r.is_redirect:
                raise DownloadRefused("marketplace.download_redirected")
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_MODULE_ARCHIVE_BYTES:
                    raise ValueError("Module archive is too large.")
    return staged_downloads.stage(_staging_dir(), f"{module_id}-{commit}", bytes(buf))


def _check_owner(module_id: str, token) -> None:
    try:
        m = _STAGED_OWNER.fullmatch(staged_downloads.owner_of(token))
    except staged_downloads.StagedDownloadMissing:
        m = None
    if m is None or m.group(1) != module_id:
        raise DownloadRefused("marketplace.import_expired")


def read_staged_archive(module_id: str, token) -> bytes:
    """The bytes of the download ``token`` names, for module ``module_id``."""
    _check_owner(module_id, token)
    try:
        return staged_downloads.read(_staging_dir(), token)[0]
    except staged_downloads.StagedDownloadMissing:
        raise DownloadRefused("marketplace.import_expired")


def discard_staged_archive(module_id: str, token) -> None:
    """Remove the download ``token`` names, once it is installed."""
    _check_owner(module_id, token)
    staged_downloads.discard(_staging_dir(), token)
