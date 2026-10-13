# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Copy document attachments using the existing company file and document event services."""
from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import find_event_by_idempotency
from celerp.models.projections import Projection
from celerp.services.attachments import MAX_FILE_BYTES, storing
from celerp.services.peer_identity import peer_identity


def _identity(source_company_id: uuid.UUID, document_id: str, file_id: str) -> tuple[str, str]:
    if not document_id or len(document_id) > 512 or not file_id or len(file_id) > 255:
        raise HTTPException(422, "Invalid attachment identity")
    raw = f"{source_company_id}:{document_id}:{file_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return "peer:file:" + digest, str(uuid.uuid5(uuid.NAMESPACE_URL, "celerp-peer-file:" + raw))


async def receive_attachment(
    session: AsyncSession, *, grant, source_doc_id: str, source_file_id: str,
    sha256_hex: str, file: UploadFile, document_tag: str | None = None,
    description: str | None = None,
) -> str:
    """Atomically record an attachment, with exactly one logical source identity."""
    if (not isinstance(sha256_hex, str) or len(sha256_hex) != 64
            or any(ch not in "0123456789abcdef" for ch in sha256_hex)):
        raise HTTPException(422, "Invalid attachment checksum")
    key, target_file_id = _identity(grant.source_company_id, source_doc_id, source_file_id)
    async with storing(session, grant.company_id) as files:
        target_doc_id = await peer_identity(
            session, target_company=grant.company_id,
            source_company=grant.source_company_id, entity_type="doc",
            source_entity_id=source_doc_id,
        )
        if not target_doc_id:
            raise HTTPException(409, "Document must be synchronized before attachments")
        row = await session.get(Projection, (grant.company_id, target_doc_id), with_for_update=True)
        if row is None or row.entity_type != "doc":
            raise HTTPException(409, "Document is unavailable")
        if not file.filename or len(file.filename) > 255:
            raise HTTPException(422, "Invalid attachment name")
        raw = await file.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(413, "Attachment exceeds file size limit")
        if hashlib.sha256(raw).hexdigest() != sha256_hex:
            raise HTTPException(409, "Attachment content does not match its checksum")
        from celerp.services.attachments import upload_mime
        mime = upload_mime(file)
        identity = {
            "sha256": sha256_hex, "mime": mime, "filename": file.filename,
            "description": description, "document_tag": document_tag,
        }
        proof = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        prior = await find_event_by_idempotency(session, grant.company_id, key)
        if prior is not None:
            if (prior.event_type != "doc.file_attached"
                    or prior.entity_id != target_doc_id
                    or (prior.metadata_ or {}).get("peer_payload_hash") != proof):
                raise HTTPException(409, "Attachment identity was reused with different content")
            return "skipped"
        try:
            meta = await files.file(raw, file.filename, mime, att_id=target_file_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        from celerp.services.attachments import attach_file as record_file
        result = await record_file(
            session, grant.company_id, "doc", target_doc_id, meta, None,
            source="connector", idempotency_key=key, document_tag=document_tag,
            description=description, metadata_={"peer_payload_hash": proof},
        )
        if getattr(result, "was_deduped", False):
            raise HTTPException(409, "Attachment identity changed during transfer")
    return "created"
