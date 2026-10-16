# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Download an authenticated company handoff into a local restore staging file."""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from celerp.services.company_handoff_crypto import MAX_CIPHERTEXT, decrypt_file
from celerp.services.company_handoff_links import parse_handoff_link


class HandoffDownloadError(Exception):
    """The copy could not be retrieved or authenticated; nothing is imported."""


def _validated_object_url(value: object, copy_id) -> str:
    """Accept only the expected ciphertext on the supported object host."""
    if not isinstance(value, str) or len(value) > 4096:
        raise HandoffDownloadError("The company copy is unavailable")
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
    except ValueError:
        raise HandoffDownloadError("The company copy is unavailable") from None
    if (url.scheme != "https" or not host or
            not (host == "digitaloceanspaces.com" or host.endswith(".digitaloceanspaces.com")) or
            port is not None or url.username or url.password or url.fragment or
            not url.path.endswith(f"/handoffs/{copy_id.hex}.bin")):
        raise HandoffDownloadError("The company copy is unavailable")
    return value


async def verify_company_handoff(copy_id: str | None, proof_hash: str | None,
                                 backup_sha256: str | None) -> None:
    """Confirm the staged archive belongs to this independently delivered copy."""
    try:
        identifier = uuid.UUID(copy_id or "")
        if str(identifier) != copy_id:
            raise ValueError
    except (ValueError, TypeError):
        raise HandoffDownloadError("Invalid company copy link") from None
    for value in (proof_hash, backup_sha256):
        if (not isinstance(value, str) or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)):
            raise HandoffDownloadError("Invalid company copy link")
    try:
        async with httpx.AsyncClient(follow_redirects=False,
                                     timeout=httpx.Timeout(15, connect=5)) as client:
            response = await client.post(
                f"https://relay.celerp.com/handoffs/{identifier}/verify",
                json={"proof": proof_hash, "backup_sha256": backup_sha256},
            )
    except (httpx.HTTPError, TimeoutError) as exc:
        raise HandoffDownloadError("Company copy verification is unavailable") from exc
    if response.status_code != 200:
        raise HandoffDownloadError("This file does not match the company copy")
    try:
        confirmed = response.json().get("verified") is True
    except (ValueError, AttributeError):
        confirmed = False
    if not confirmed:
        raise HandoffDownloadError("Company copy verification was not confirmed")


Progress = Callable[..., None]


def _silent(stage: str, received: int = 0, total: int | None = None) -> None:
    return None


async def download_company_handoff(link: str, destination: Path, progress: Progress | None = None) -> None:
    """Authenticate a ciphertext download before publishing the staged restore input.

    The existing company-backup preview and restore routes remain responsible for
    validating and importing the decrypted company. This function changes no books.
    `progress(stage, received, total)` hears "requesting", then "downloading" with the
    bytes received so far and the size when the storage names it, then "decrypting".
    """
    progress = progress or _silent
    parsed = parse_handoff_link(link)
    root = destination.parent
    encrypted = None
    try:
        async with asyncio.timeout(900):
            async with httpx.AsyncClient(follow_redirects=False,
                                         timeout=httpx.Timeout(120, connect=10)) as client:
                grant_id = str(uuid.uuid4())
                progress("requesting")
                for attempt in range(2):
                    try:
                        response = await client.post(
                            f"https://relay.celerp.com/handoffs/{parsed.copy_id}/fetch",
                            json={"proof": parsed.proof_hash, "operation_id": grant_id},
                        )
                    except httpx.TransportError:
                        if attempt:
                            raise
                        continue
                    if response.status_code not in (502, 503, 504) or attempt:
                        break
                if response.status_code != 200:
                    raise HandoffDownloadError("This company copy is not available for import")
                try:
                    url = _validated_object_url(response.json().get("download_url"), parsed.copy_id)
                except (ValueError, AttributeError):
                    raise HandoffDownloadError("The company copy is unavailable") from None
                fd, name = tempfile.mkstemp(prefix=".celerp-handoff-", suffix=".encrypted", dir=root)
                encrypted = Path(name)
                with os.fdopen(fd, "wb") as target:
                    async with client.stream("GET", url) as incoming:
                        if incoming.status_code != 200:
                            raise HandoffDownloadError("The company copy could not be downloaded")
                        length = incoming.headers.get("content-length")
                        size = None
                        if length:
                            try:
                                size = int(length)
                            except ValueError:
                                raise HandoffDownloadError("The company copy has an invalid size") from None
                            if size > MAX_CIPHERTEXT:
                                raise HandoffDownloadError("The company copy is too large")
                        total = 0
                        progress("downloading", 0, size)
                        async for chunk in incoming.aiter_bytes(chunk_size=1024 * 1024):
                            total += len(chunk)
                            if total > MAX_CIPHERTEXT:
                                raise HandoffDownloadError("The company copy is too large")
                            target.write(chunk)
                            progress("downloading", total, size)
                    target.flush()
                    os.fsync(target.fileno())
            progress("decrypting")
            await asyncio.to_thread(decrypt_file, encrypted, destination, parsed.secret)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise HandoffDownloadError("The company copy could not be downloaded") from exc
    finally:
        if encrypted is not None:
            encrypted.unlink(missing_ok=True)
