# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Ask Celerp's team to move a client: the export set open on the upload page goes to the
cloud, the team reads it, runs the migration and sends the company copy back through
the normal send of a company copy, and the firm sees each request's status here.

Unlike a client's export request, these files are sent readable: the team has to read
them to do the move. The cloud stores them encrypted at rest and deletes them when the
request closes or 30 days after it was sent. The page says so before anything is sent.
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi import HTTPException

from celerp.importers.adapters.base import SourceRevisionError
from celerp.importers.adapters.registry import get_adapter
from celerp.services import migration_scan_store as store
from celerp.services.company_handoff_sender import cloud_json
from celerp.services.migration_export_requests import required_email, required_text

ACCOUNT_NEEDED = "Sign in to a free Celerp account to ask us to move a client."
UNCONFIRMED = "Could not confirm the move request. Retry the same step"
NO_SET = "Add the export files on this page first, then ask us to move this client."
UPLOAD_S = 600


async def _cloud(method: str, path: str, **kw) -> dict:
    return await cloud_json(method, path, unconfirmed=UNCONFIRMED, account_needed=ACCOUNT_NEEDED, **kw)


async def send(*, operation_id: uuid.UUID, firm_name: str, client_name: str, reply_to: str,
               draft_token: str, owner: store.ScanOwner) -> dict:
    """Send the owner's open export set to the team. Only a whole set is sent. A retry of
    the same ``operation_id`` continues the same request and uploads only the files the
    cloud does not hold yet, so an interrupted send resumes and the team is told once."""
    payload = {"operation_id": str(operation_id), "firm_name": required_text(firm_name, "firm name"),
               "client_name": required_text(client_name, "client name"), "reply_to": required_email(reply_to, "reply-to email")}
    if not (draft_token or "").strip():
        raise HTTPException(status_code=422, detail=NO_SET)
    adapter_key, artifacts = await asyncio.to_thread(store.draft_files, draft_token, owner=owner)
    adapter = get_adapter(adapter_key)
    check = getattr(adapter, "check", None)
    if check is None or not artifacts:
        raise HTTPException(status_code=422, detail=NO_SET)
    try:
        checked = await asyncio.to_thread(check, artifacts)
    except SourceRevisionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not checked.complete:
        raise HTTPException(status_code=422, detail=(
            "Finish the export set before asking us to move this client: " + " ".join(checked.problems)))
    created = await _cloud("POST", "/move-requests", payload={**payload, "source": adapter.key}, expected=201)
    request_id = uuid.UUID(str(created.get("id")))
    if created.get("status") == "uploading":
        held = {str(f.get("name")) for f in (await _row(request_id)).get("files") or []}
        for artifact in artifacts:
            if artifact.original_name in held:
                continue
            data = await asyncio.to_thread(artifact.path.read_bytes)
            await _cloud("POST", f"/move-requests/{request_id}/files", params={"name": artifact.original_name},
                         content=data, total_s=UPLOAD_S, expected=201)
    return await _cloud("POST", f"/move-requests/{request_id}/submit")


async def list_requests() -> list[dict]:
    """Requests sent from this installation's account, newest first."""
    items = (await _cloud("GET", "/move-requests")).get("items")
    return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []


async def _row(request_id: uuid.UUID) -> dict:
    for row in await list_requests():
        if row.get("id") == str(request_id):
            return row
    raise HTTPException(status_code=404, detail="Move request not found")


async def withdraw(request_id: uuid.UUID) -> dict:
    """Stop the request: the team no longer works it and its files are deleted."""
    return await _cloud("POST", f"/move-requests/{request_id}/withdraw")
