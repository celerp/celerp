# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Email a client the export checklist of a migration source, and bring the files the
client uploads into the migration upload.

The client's browser encrypts each file with a key that travels only in the emailed link
and in this installation, so the cloud stores files it cannot read. Files are read here:
downloaded, authenticated and decrypted, then handed to the scan store as one upload,
or added to the draft already open, exactly as if they had been chosen on the page.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException

from celerp.importers.adapters.registry import get_adapter
from celerp.services import company_backup_files as files
from celerp.services import migration_scan_store as store
from celerp.services.company_handoff_crypto import MAX_CIPHERTEXT, decrypt_file
from celerp.services.company_handoff_links import spaces_object_url
from celerp.services.company_handoff_sender import cloud_json, decode_key, encode_key

ACCOUNT_NEEDED = "Sign in to a free Celerp account to email an export checklist."
UNCONFIRMED = "Could not confirm the export checklist. Retry the same step"
CLOSED = ("revoked", "expired")
_TEXT_MAX = 160
_EMAIL_MAX = 320


def _text(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > _TEXT_MAX or any(ord(ch) < 0x20 for ch in value):
        raise HTTPException(status_code=422, detail=f"Enter the {what}.")
    return value


def _email(value: str, what: str) -> str:
    value = (value or "").strip()
    local, sep, host = value.partition("@")
    if (not sep or not local or "@" in host or "." not in host or len(value) > _EMAIL_MAX
            or any(ch.isspace() or ord(ch) < 0x20 for ch in value)):
        raise HTTPException(status_code=422, detail=f"Enter a valid {what}.")
    return value


async def _cloud(method: str, path: str, **kw) -> dict:
    return await cloud_json(method, path, unconfirmed=UNCONFIRMED, account_needed=ACCOUNT_NEEDED, **kw)


async def send_request(*, operation_id: uuid.UUID, firm_name: str, client_name: str,
                       recipient: str, reply_to: str, source: str) -> dict:
    """Email the client the files ``source`` asks for, each with the menu and dates to
    export. A retry of the same ``operation_id`` sends the same key, so a link already
    emailed still opens the request."""
    adapter = get_adapter(source)
    checklist = getattr(adapter, "checklist", None)
    if checklist is None:
        name = adapter.display_name if adapter is not None else source
        raise HTTPException(status_code=422, detail=f"{name} has no list of files to export.")
    payload = {"operation_id": str(operation_id), "firm_name": _text(firm_name, "firm name"),
               "client_name": _text(client_name, "client name"),
               "recipient": _email(recipient, "client email"), "reply_to": _email(reply_to, "reply-to email"),
               "source": adapter.key,
               "items": [{"label": i["label"], "menu": i["menu"], "range": "" if i["range"] == "--" else i["range"]}
                         for i in checklist().view()["items"] if i["status"] != "not_needed"]}
    key = await asyncio.to_thread(files.export_request_key, operation_id)
    if key is None:
        key = encode_key(os.urandom(32))
        # Kept before the email is sent: a link that reaches the client is always readable here.
        await asyncio.to_thread(files.remember_export_request_key, operation_id, key)
    return await _cloud("POST", "/export-requests", payload={**payload, "key": key}, expected=201)


async def list_requests() -> list[dict]:
    """Requests sent from this installation's account, newest first; ``readable`` says
    whether their files can still be added here."""
    items = (await _cloud("GET", "/export-requests")).get("items")
    rows = [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []
    for row in rows:
        held = await asyncio.to_thread(_key_for, row)
        row["readable"] = row.get("status") not in CLOSED and held is not None
    return rows


def _key_for(row: dict) -> str | None:
    try:
        return files.export_request_key(row.get("operation_id"))
    except ValueError:
        return None


async def _row(request_id: uuid.UUID) -> dict:
    for row in await list_requests():
        if row.get("id") == str(request_id):
            return row
    raise HTTPException(status_code=404, detail="Export request not found")


async def _download(client: httpx.AsyncClient, request_id: uuid.UUID, file: dict, folder: Path,
                    secret: bytes) -> Path:
    """One client file, downloaded and decrypted into ``folder``; the plain file's path."""
    file_id, name = str(file.get("id")), str(file.get("name") or "file")
    grant = await _cloud("POST", f"/export-requests/{request_id}/files/{file_id}/fetch")
    try:
        url = spaces_object_url(grant.get("download_url"), f"/export-requests/{request_id.hex}/{file_id}")
    except ValueError:
        raise HTTPException(status_code=502, detail=f"The export file {name} is unavailable") from None
    encrypted, plain = folder / f"{file_id}.encrypted", folder / f"{file_id}.plain"
    try:
        with files.private_file(encrypted) as target:
            async with client.stream("GET", url) as incoming:
                if incoming.status_code != 200:
                    raise HTTPException(status_code=502, detail=f"The export file {name} could not be downloaded")
                total = 0
                async for chunk in incoming.aiter_bytes(chunk_size=1024 * 1024):
                    total += len(chunk)
                    if total > MAX_CIPHERTEXT:
                        raise HTTPException(status_code=413, detail=f"The export file {name} is too large")
                    target.write(chunk)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"The export file {name} could not be downloaded") from exc
    try:
        await asyncio.to_thread(decrypt_file, encrypted, plain, secret)
    except (ValueError, InvalidTag):
        raise HTTPException(status_code=422, detail=(
            f"{name} could not be read with this request's key. Ask the client to upload it again. "
            "Nothing was added.")) from None
    finally:
        encrypted.unlink(missing_ok=True)
    return plain


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            yield chunk


async def _text_part(value: str) -> AsyncIterator[bytes]:
    yield value.encode()


async def add_files(request_id: uuid.UUID, draft_token: str | None, *, owner: store.ScanOwner):
    """Read every file the client uploaded and add them to the migration upload, or to the
    owner's open draft. Nothing is added unless every file is read."""
    row = await _row(request_id)
    uploaded = [f for f in row.get("files") or [] if isinstance(f, dict)]
    if not uploaded:
        raise HTTPException(status_code=422, detail="No files have arrived yet. They appear here as the client uploads them.")
    key = await asyncio.to_thread(_key_for, row)
    try:
        secret = decode_key(key)
    except ValueError:
        raise HTTPException(status_code=409, detail=(
            "This installation does not hold the key for these files, so it cannot read them. "
            "Send a new request from here.")) from None
    with tempfile.TemporaryDirectory(prefix="download-", dir=files.export_requests_dir()) as folder:
        folder = Path(folder)
        async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(120, connect=10)) as client:
            plain = [(f, await _download(client, request_id, f, folder, secret)) for f in uploaded]

        async def parts():
            yield store.UploadPart("source", None, _text_part(str(row.get("source") or "")))
            if draft_token:
                yield store.UploadPart("draft", None, _text_part(draft_token))
            for f, path in plain:
                yield store.UploadPart("files", str(f.get("name") or "file"), _chunks(path))

        return await store.create_scan(parts(), owner=owner)


async def revoke(request_id: uuid.UUID) -> dict:
    """Close the request: its link stops working and the files stored for it are deleted.
    The key is forgotten too, so nothing it covered can be read here again."""
    row = await _row(request_id)
    result = await _cloud("POST", f"/export-requests/{request_id}/revoke")
    try:
        await asyncio.to_thread(files.forget_export_request_key, row.get("operation_id"))
    except ValueError:
        pass
    return result
