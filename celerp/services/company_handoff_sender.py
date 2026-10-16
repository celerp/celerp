# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Owner-approved encrypted company backup delivery with retry-safe staging."""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from celerp.services import cloud_entitlement, company_backup_files as files
from celerp.services.company_handoff_crypto import decrypt_file, encrypt_file
from celerp.services.company_handoff_links import handoff_page_link


class HandoffSendError(Exception):
    """The delivery was refused or its result is unconfirmed; retry the same operation."""


class HandoffAccountNeeded(HandoffSendError):
    """Sending a link needs a free, verified Celerp account on this installation."""


_ACCOUNT_NEEDED = "Sign in to a free Celerp account to send a company copy link."


def _upload_url(value: object, copy_id: uuid.UUID) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise HandoffSendError("The upload destination is unavailable")
    try:
        url = urlsplit(value)
        host, port = url.hostname, url.port
    except ValueError:
        raise HandoffSendError("The upload destination is unavailable") from None
    if (url.scheme != "https" or not host or
            not (host == "digitaloceanspaces.com" or host.endswith(".digitaloceanspaces.com")) or
            port is not None or url.username or url.password or url.fragment or
            not url.path.endswith(f"/handoffs/{copy_id.hex}.upload")):
        raise HandoffSendError("The upload destination is unavailable")
    return value


async def _cloud(method: str, path: str, *, payload: dict | None = None,
                 params: dict | None = None, expected: int = 200) -> dict:
    try:
        response = await cloud_entitlement.authenticated_request(
            method, path, json=payload, params=params, total_s=35)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise HandoffSendError("Could not confirm the company copy. Retry the same operation") from exc
    if response is None or response.status_code == 401:
        raise HandoffAccountNeeded(_ACCOUNT_NEEDED)
    if response.status_code == 403 and (method, path) == ("POST", "/handoffs"):
        # The relay's only refusal on create is an account it has not verified.
        raise HandoffAccountNeeded(_ACCOUNT_NEEDED)
    if response.status_code != expected:
        # A refusal the relay explains (a limit and its way out, a reply-to
        # mismatch) is shown as is; anything else is unconfirmed.
        detail = _detail(response) if 400 <= response.status_code < 500 else None
        raise HandoffSendError(detail or "Could not confirm the company copy. Retry the same operation")
    try:
        data = response.json()
    except ValueError:
        raise HandoffSendError("Invalid company copy response") from None
    if not isinstance(data, dict):
        raise HandoffSendError("Invalid company copy response")
    return data


def _detail(response: httpx.Response) -> str | None:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return None
    return detail if isinstance(detail, str) and detail else None


async def _existing(company_id: uuid.UUID, operation_id: uuid.UUID) -> dict | None:
    result = await _cloud("GET", "/handoffs", params={
        "company_id": str(company_id), "operation_id": str(operation_id),
    })
    items = result.get("items")
    if not isinstance(items, list) or len(items) > 1:
        raise HandoffSendError("Could not verify the existing company copy")
    if not items:
        return None
    row = items[0]
    if (not isinstance(row, dict) or row.get("operation_id") != str(operation_id)
            or row.get("company_id") != str(company_id)):
        raise HandoffSendError("Could not verify the existing company copy")
    return row


def _stage_path(company_id: uuid.UUID, operation_id: uuid.UUID, secret: bytes) -> Path:
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise HandoffSendError("Invalid company copy key")
    token = operation_id.hex + hashlib.sha256(secret).hexdigest()
    path = files.stage_path(str(company_id), token)
    if path is None:
        raise HandoffSendError("Invalid handoff operation")
    return path


async def _stage_once(path: Path, secret: bytes,
                      export: Callable[[Path], Awaitable[None]], company_id: uuid.UUID) -> None:
    """Keep one authenticated ciphertext across crashes and ambiguous retries.

    Publishing with a hard link prevents a concurrent attempt from overwriting
    ciphertext that another attempt is uploading. The staging folder's existing
    expiry sweep removes abandoned copies after 24 hours.
    """
    if path.is_file():
        return
    plain = files.export_path(company_id)
    candidate = path.with_name(path.name + f".{uuid.uuid4().hex}.partial")
    try:
        await export(plain)
        await asyncio.to_thread(encrypt_file, plain, candidate, secret)
        try:
            os.link(candidate, path)
        except FileExistsError:
            pass  # another attempt published the same operation first
    finally:
        plain.unlink(missing_ok=True)
        candidate.unlink(missing_ok=True)


def _verified_archive_digest(stage: Path, secret: bytes, company_id: uuid.UUID) -> str:
    """Hash the exact immutable snapshot represented by the staged ciphertext."""
    plain = files.export_path(company_id)
    try:
        decrypt_file(stage, plain, secret)
        digest = hashlib.sha256()
        with plain.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        plain.unlink(missing_ok=True)


async def _send(copy_id: uuid.UUID, operation_id: uuid.UUID, secret: bytes, *,
                recipient: str, reply_to: str, note: str) -> dict:
    link = handoff_page_link(copy_id, secret)
    await _cloud("POST", f"/handoffs/{copy_id}/send", payload={
        "operation_id": str(operation_id), "recipient": recipient,
        "reply_to": reply_to, "note": note, "link": link,
    })
    return {"id": str(copy_id), "link": link}


async def send_company_backup(*, export: Callable[[Path], Awaitable[None]],
                              company_id: uuid.UUID, operation_id: uuid.UUID,
                              secret: bytes, firm_name: str, recipient: str,
                              reply_to: str, note: str) -> dict:
    stage = _stage_path(company_id, operation_id, secret)
    existing = await _existing(company_id, operation_id)
    if existing is not None:
        if existing.get("firm_name") != firm_name:
            raise HandoffSendError("This handoff was already prepared for a different firm")
        if existing.get("status") in ("ready", "sent", "imported"):
            result = await _send(uuid.UUID(existing["id"]), operation_id, secret,
                                 recipient=recipient, reply_to=reply_to, note=note)
            stage.unlink(missing_ok=True)
            return result
        if existing.get("status") != "pending":
            raise HandoffSendError("This handoff has expired. Start a new company copy")
        if not stage.is_file():
            raise HandoffSendError("The original encrypted upload is unavailable. Start a new copy")
    else:
        await _stage_once(stage, secret, export, company_id)

    size = stage.stat().st_size
    backup_sha256 = await asyncio.to_thread(_verified_archive_digest, stage, secret, company_id)
    created = await _cloud("POST", "/handoffs", payload={
        "operation_id": str(operation_id), "company_id": str(company_id),
        "firm_name": firm_name, "size_bytes": size,
        "proof_hash": hashlib.sha256(secret).hexdigest(),
        "backup_sha256": backup_sha256,
    }, expected=201)
    try:
        copy_id = uuid.UUID(created["id"])
    except (ValueError, TypeError, KeyError):
        raise HandoffSendError("Invalid company copy response") from None
    upload_url = created.get("upload_url")
    if upload_url is not None:
        url = _upload_url(upload_url, copy_id)
        async def chunks():
            with stage.open("rb") as inp:
                while data := inp.read(1024 * 1024):
                    yield data
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10),
                                         follow_redirects=False) as client:
                response = await client.put(url, content=chunks(),
                                            headers={"Content-Length": str(size)})
        except (httpx.HTTPError, TimeoutError) as exc:
            raise HandoffSendError("Encrypted upload not confirmed. Retry the same operation") from exc
        if response.status_code not in (200, 201, 204):
            raise HandoffSendError("Encrypted upload not confirmed. Retry the same operation")
    elif created.get("status") not in ("ready", "sent", "imported"):
        raise HandoffSendError("This handoff cannot be resumed")
    result = await _send(copy_id, operation_id, secret, recipient=recipient,
                         reply_to=reply_to, note=note)
    stage.unlink(missing_ok=True)
    return result
