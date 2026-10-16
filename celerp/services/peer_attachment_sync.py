# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Send document files after document synchronization, with independent retries."""
from __future__ import annotations

import hashlib
import uuid

from celerp.services.attachments import MAX_FILE_BYTES, accepts_mime, read_company_file


async def sync_document_attachments(connector, ctx):
    from celerp.connectors.base import SyncDirection, SyncEntity, SyncResult
    from celerp.connectors.http import RateLimitedClient
    from celerp.connectors.celerp import (
        _PAGE, _read_projection_page, _target_url, attention_label,
    )
    from celerp.connectors.sync_runner import _last_success_watermark

    result = SyncResult(
        entity=SyncEntity.ATTACHMENTS, direction=SyncDirection.OUTBOUND,
        errors=[], attention=[],
    )
    target = _target_url(ctx, "attachments")
    if not isinstance(ctx.access_token, str) or not ctx.access_token.startswith("cp1."):
        raise ValueError("No scoped receiving credential is connected")
    cid = uuid.UUID(ctx.company_id)
    since = await _last_success_watermark(ctx.company_id, connector.name, "attachments_out")
    after = ""
    async with RateLimitedClient(public_only=True, timeout=180.0) as client:
        while True:
            page = await _read_projection_page(cid, after, since, "doc")
            if not page:
                break
            for source_doc_id, _, state in page:
                if state.get("accounting_sync_excluded"):
                    result.skipped += 1
                    continue
                files = state.get("files") or []
                doc_label = attention_label(state, source_doc_id)
                if not isinstance(files, list):
                    result.errors.append("Document attachment list needs review")
                    result.attention.append({
                        "id": source_doc_id, "label": doc_label,
                        "reason": "Invalid attachment list",
                    })
                    continue
                for meta in files:
                    attachment_id = meta.get("id") if isinstance(meta, dict) else None
                    filename = meta.get("filename") if isinstance(meta, dict) else None
                    key = f"{source_doc_id}:{attachment_id or '?'}"
                    label = (f"{doc_label}: {filename}" if isinstance(filename, str) and filename
                             else doc_label)
                    try:
                        if (not isinstance(attachment_id, str) or not attachment_id
                                or len(attachment_id) > 255):
                            raise ValueError("File identity unavailable")
                        name, mime, url = meta.get("filename"), meta.get("mime"), meta.get("url")
                        if (not isinstance(name, str) or not name or len(name) > 255
                                or not isinstance(mime, str) or not accepts_mime(mime)
                                or not isinstance(url, str) or not url):
                            raise ValueError("File metadata is invalid")
                        content = await read_company_file(cid, url, MAX_FILE_BYTES)
                        if content is None:
                            raise ValueError("File is missing or exceeds size limit")
                        form = {
                            "source_company_id": str(cid),
                            "source_document_id": source_doc_id,
                            "source_file_id": attachment_id,
                            "sha256_hex": hashlib.sha256(content).hexdigest(),
                        }
                        if meta.get("document_tag"):
                            form["document_tag"] = str(meta["document_tag"])
                        if meta.get("description"):
                            form["description"] = str(meta["description"])
                        response = await client.post(
                            target, headers={"Authorization": "Peer " + ctx.access_token},
                            data=form, files={"file": (name, content, mime)},
                        )
                        if response.status_code != 200:
                            raise ValueError("Receiving installation refused attachment")
                        status = response.json().get("status")
                        if status not in ("created", "skipped"):
                            raise ValueError("Receiving installation did not acknowledge attachment")
                        result.record(status)
                    except Exception:
                        result.errors.append("Attachment delivery incomplete")
                        result.attention.append({
                            "id": key, "label": label,
                            "reason": "Document attachment needs attention",
                        })
            after = page[-1][0]
            if len(page) < _PAGE:
                break
    return result
