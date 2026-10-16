# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Owner-approved encrypted company backup delivery with retry-safe staging."""
from __future__ import annotations

import asyncio
import base64
import binascii
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


class HandoffRefused(HandoffSendError):
    """The cloud refused with a reason the owner can act on; keeps its status code."""

    def __init__(self, detail: str, status_code: int):
        super().__init__(detail)
        self.status_code = status_code


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
        if detail:
            raise HandoffRefused(detail, response.status_code)
        raise HandoffSendError("Could not confirm the company copy. Retry the same operation")
    try:
        data = response.json()
    except ValueError:
        raise HandoffSendError("Invalid company copy response") from None
    if not isinstance(data, dict):
        raise HandoffSendError("Invalid company copy response")
    return data


def _detail(response: httpx.Response) -> str | None:
    """The refusal's reason: its text, or the messages of a request the cloud found invalid."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return None
    if isinstance(detail, list):
        detail = " ".join(m for e in detail if isinstance(e, dict) and isinstance(m := e.get("msg"), str))
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


def encode_key(secret: bytes) -> str:
    return base64.urlsafe_b64encode(secret).decode().rstrip("=")


def decode_key(value: str) -> bytes:
    """The 32-byte link key from its 43-character form; ValueError for anything else."""
    try:
        secret = base64.urlsafe_b64decode(str(value) + "=")
    except (ValueError, binascii.Error):
        raise ValueError("Invalid company copy key") from None
    if len(secret) != 32 or encode_key(secret) != value:
        raise ValueError("Invalid company copy key")
    return secret


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


def _link_id(row: dict) -> uuid.UUID:
    try:
        return uuid.UUID(row["link_id"])
    except (ValueError, TypeError, KeyError):
        raise HandoffSendError("Invalid company copy response") from None


async def _send(company_id: uuid.UUID, copy_id: uuid.UUID, link_id: uuid.UUID,
                operation_id: uuid.UUID, secret: bytes, *,
                recipient: str, reply_to: str, note: str) -> dict:
    link = handoff_page_link(link_id, secret)
    await _cloud("POST", f"/handoffs/{copy_id}/send", payload={
        "operation_id": str(operation_id), "recipient": recipient,
        "reply_to": reply_to, "note": note, "link": link,
    })
    await asyncio.to_thread(files.remember_sent_copy, company_id, copy_id, {
        "key": encode_key(secret), "recipient": recipient})
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
            result = await _send(company_id, uuid.UUID(existing["id"]), _link_id(existing),
                                 operation_id, secret,
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
    result = await _send(company_id, copy_id, _link_id(created), operation_id, secret,
                         recipient=recipient, reply_to=reply_to, note=note)
    stage.unlink(missing_ok=True)
    return result


# ── Copies sent ──────────────────────────────────────────────────────────────

_UNAVAILABLE = "Company copy activity is unavailable"
_STATUSES = ("pending", "ready", "sent", "imported", "expired", "revoked")


class CopyNotFound(HandoffRefused):
    def __init__(self):
        super().__init__("Company copy not found", 404)


def _copy(company_id: uuid.UUID, row: object) -> dict:
    """One relay row checked and joined with what this installation kept at sending."""
    if not isinstance(row, dict) or row.get("company_id") != str(company_id):
        raise HandoffSendError(_UNAVAILABLE)
    try:
        copy_id, link_id = uuid.UUID(row["id"]), uuid.UUID(row["link_id"])
    except (ValueError, KeyError, TypeError):
        raise HandoffSendError(_UNAVAILABLE) from None
    status = row.get("status")
    if status not in _STATUSES:
        raise HandoffSendError(_UNAVAILABLE)
    stored, can_restore = row.get("stored") is True, row.get("can_restore") is True
    facts = files.sent_copy(company_id, copy_id)
    if not stored and not can_restore:
        files.forget_sent_copy(company_id, copy_id)  # its key opens nothing any more
    link = None
    if status in ("sent", "imported") and stored and facts.get("key"):
        try:
            link = handoff_page_link(link_id, decode_key(facts["key"]))
        except ValueError:
            link = None
    return {"id": str(copy_id), "status": status,
            "sent_at": row.get("sent_at") or None, "expires_at": row.get("expires_at") or None,
            "opened": row.get("opened") is True, "downloaded": row.get("downloaded") is True,
            "company_opened": row.get("company_opened") is True,
            "stored": stored, "can_restore": can_restore,
            "firm_name": row.get("firm_name") if isinstance(row.get("firm_name"), str) else None,
            "recipient": facts.get("recipient"), "link": link}


async def company_copies(company_id: uuid.UUID) -> list[dict]:
    """This company's copies, newest first, each with its link when this installation sent it."""
    answer = await _cloud("GET", "/handoffs", params={"company_id": str(company_id)})
    rows = answer.get("items")
    if not isinstance(rows, list) or len(rows) > 100:
        raise HandoffSendError(_UNAVAILABLE)
    return [_copy(company_id, row) for row in rows]  # the relay lists newest first


async def _owned(company_id: uuid.UUID, copy_id: uuid.UUID) -> dict:
    """One installation holds several companies: act only on this company's copy."""
    for copy in await company_copies(company_id):
        if copy["id"] == str(copy_id):
            return copy
    raise CopyNotFound()


async def revoke_copy(company_id: uuid.UUID, copy_id: uuid.UUID) -> dict:
    await _owned(company_id, copy_id)
    await _cloud("POST", f"/handoffs/{copy_id}/revoke")
    return await _owned(company_id, copy_id)


async def restore_copy(company_id: uuid.UUID, copy_id: uuid.UUID) -> dict:
    await _owned(company_id, copy_id)
    await _cloud("POST", f"/handoffs/{copy_id}/restore")
    return await _owned(company_id, copy_id)


async def new_link(company_id: uuid.UUID, copy_id: uuid.UUID, operation_id: uuid.UUID) -> dict:
    """Replace the copy's link; the old one stops working. Retrying the same operation
    returns the same new link."""
    await _owned(company_id, copy_id)
    await _cloud("POST", f"/handoffs/{copy_id}/new-link", payload={"operation_id": str(operation_id)})
    return await _owned(company_id, copy_id)
