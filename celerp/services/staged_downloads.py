# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Downloaded module archives waiting for Import.

Each download is a file of its own, named by a token: the owner it was
downloaded for (a module id, plus whatever the caller pins with it) and a random
part. Import names the token, so it installs exactly the bytes that download
fetched, whatever else is downloaded in the meantime. Files are written under a
temporary name and moved into place, so a token never names a partial file.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

# A download is imported within this long, or downloaded again.
TTL_SECONDS = 24 * 60 * 60

_OWNER = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TOKEN = re.compile(r"([A-Za-z0-9_-]{1,128})-[0-9a-f]{32}")


class StagedDownloadMissing(LookupError):
    """The token names no current download: never issued, already imported, or
    expired."""


class StagedDownloadUnreadable(StagedDownloadMissing):
    """The download exists but the details recorded with it cannot be read."""


def valid_owner(owner) -> bool:
    """Whether ``owner`` can name downloads: up to 128 letters, digits, '-' and
    '_'."""
    return isinstance(owner, str) and _OWNER.fullmatch(owner) is not None


def _expired(path: Path) -> bool:
    return time.time() - path.stat().st_mtime > TTL_SECONDS


def _write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _drop_expired(directory: Path) -> None:
    for p in directory.iterdir():
        try:
            if _expired(p):
                p.unlink()
        except OSError:
            pass


def stage(directory: Path, owner: str, data: bytes, meta: dict | None = None) -> str:
    """Keep ``data`` (and ``meta``, if given) as a download of its own and
    return the token naming it."""
    if not valid_owner(owner):
        raise ValueError("Invalid download owner.")
    directory.mkdir(parents=True, exist_ok=True)
    _drop_expired(directory)
    token = f"{owner}-{secrets.token_hex(16)}"
    if meta is not None:
        _write(directory / f"{token}.json", json.dumps(meta).encode())
    _write(directory / f"{token}.zip", data)
    return token


def owner_of(token) -> str:
    """The owner a token was issued for; StagedDownloadMissing if it is not a
    token at all."""
    m = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    if m is None:
        raise StagedDownloadMissing(token)
    return m.group(1)


def read(directory: Path, token) -> tuple[bytes, dict | None]:
    """The bytes of the download ``token`` names, and the details staged with
    it (None if none were). An expired download is removed and refused."""
    owner_of(token)
    path = directory / f"{token}.zip"
    try:
        if _expired(path):
            discard(directory, token)
            raise StagedDownloadMissing(token)
        data = path.read_bytes()
    except FileNotFoundError:
        raise StagedDownloadMissing(token)
    sidecar = path.with_suffix(".json")
    if not sidecar.exists():
        return data, None
    try:
        meta = json.loads(sidecar.read_text())
    except (ValueError, OSError):
        raise StagedDownloadUnreadable(token)
    if not isinstance(meta, dict):
        raise StagedDownloadUnreadable(token)
    return data, meta


def discard(directory: Path, token) -> None:
    """Remove the download ``token`` names, once it is installed."""
    owner_of(token)
    for suffix in (".zip", ".json"):
        (directory / f"{token}{suffix}").unlink(missing_ok=True)
