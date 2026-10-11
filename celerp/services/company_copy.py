# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Send one portable company copy using the existing backup and Connect services."""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import httpx
from fastapi import HTTPException

from celerp.services import cloud_entitlement
from celerp.services import company_backup as backup
from celerp.services import company_backup_files as files
from celerp.services.company_copy_crypto import encrypt_copy, new_secret

_API = "/handoffs"


def _cloud_response(response, *, expected: int) -> dict:
    if response is None:
        raise HTTPException(503, "Connect is unavailable. No company copy was sent.")
    if response.status_code != expected:
        message = "Could not send the company copy. Nothing was emailed."
        try:
            detail = response.json().get("detail")
            if isinstance(detail, str) and detail:
                message = detail
        except (ValueError, AttributeError):
            pass
        raise HTTPException(response.status_code if response.status_code < 500 else 502, message)
    return response.json()


async def _upload(url: str, headers: dict, path: Path) -> None:
    async def contents():
        with path.open("rb") as stream:
            for piece in iter(lambda: stream.read(1024 * 1024), b""):
                yield piece

    if not url.startswith("https://"):
        raise HTTPException(502, "Connect did not provide a secure upload address")
    hdr = {"Content-Length": str(path.stat().st_size)}
    if headers.get("Content-Length") != hdr["Content-Length"]:
        raise HTTPException(502, "Connect returned a mismatched upload length")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=120, write=120)) as client:
            response = await client.put(url, content=contents(), headers=hdr)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "The encrypted company copy could not be uploaded") from exc
    if response.status_code not in (200, 201, 204):
        raise HTTPException(502, "The encrypted company copy could not be uploaded")


async def send_company_copy(company_id, *, recipient: str, note: str,
                            firm_name: str, reply_to: str) -> dict:
    """Export only this company, encrypt locally, then email a single-use link."""
    if not recipient.strip() or not reply_to.strip() or not firm_name.strip():
        raise HTTPException(422, "Enter the recipient, firm name and reply-to address")
    if len(note) > 1500 or len(firm_name) > 160 or len(recipient) > 320:
        raise HTTPException(422, "A company-copy field is too long")
    source = files.export_path(company_id)
    ciphertext = source.with_name(source.name + ".encrypted")
    key, fragment = new_secret()
    try:
        try:
            await backup.export_company_snapshot(company_id, source)
        except backup.BackupError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        encrypted_bytes = await asyncio.to_thread(encrypt_copy, source, ciphertext, key)
        requested = await cloud_entitlement.authenticated_request("POST", _API, total_s=40, json={
            "company_id": str(company_id), "firm_name": firm_name.strip(),
            "size_bytes": encrypted_bytes, "proof_hash": hashlib.sha256(key).hexdigest(),
        })
        created = _cloud_response(requested, expected=201)
        url = created.get("upload_url")
        if not isinstance(url, str) or not isinstance(created.get("id"), str):
            raise HTTPException(502, "Connect could not prepare the company copy")
        await _upload(url, created.get("upload_headers") or {}, ciphertext)
        handoff_id = str(uuid.UUID(created["id"]))
        link = f"https://relay.celerp.com/handoffs/{handoff_id}/open#{fragment}"
        sent = await cloud_entitlement.authenticated_request("POST", f"{_API}/{handoff_id}/send",
            total_s=40, json={
                "operation_id": str(uuid.uuid4()), "recipient": recipient.strip(),
                "reply_to": reply_to.strip(), "note": note, "link": link,
            })
        _cloud_response(sent, expected=200)
        return {"id": handoff_id, "link": link, "status": "sent"}
    finally:
        source.unlink(missing_ok=True)
        ciphertext.unlink(missing_ok=True)


async def list_company_copies(company_id) -> list[dict]:
    response = await cloud_entitlement.authenticated_request("GET", _API,
        params={"company_id": str(company_id)}, total_s=25)
    return _cloud_response(response, expected=200).get("items", [])


async def revoke_company_copy(handoff_id: uuid.UUID) -> dict:
    response = await cloud_entitlement.authenticated_request("POST",
        f"{_API}/{handoff_id}/revoke", total_s=25)
    return _cloud_response(response, expected=200)
