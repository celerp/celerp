# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Authenticated attachment delivery (local backend).

Attachments live under ``data_dir/static/attachments/<company_id>/``. They are
served only through this route, which scopes every request to the caller's own
company via the validated bearer token, so one tenant can never read another's
files. S3 logical-URL delivery is handled separately by the storage backend and
is not routed here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from celerp.services.attachments import local_attachment_path
from celerp.services.auth import get_current_company_id

router = APIRouter(prefix="/static/attachments", tags=["attachments"])


@router.get("/{company_id}/{filename}")
async def get_attachment(
    company_id: str,
    filename: str,
    ctx_company_id: uuid.UUID = Depends(get_current_company_id),
) -> FileResponse:
    """Serve one attachment belonging to the caller's company.

    A path addressed to any other company, or a name that escapes the company
    directory, is indistinguishable from a missing file to this caller: 404.
    """
    if company_id != str(ctx_company_id):
        raise HTTPException(status_code=404, detail="Not found")
    path = local_attachment_path(str(ctx_company_id), filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)
