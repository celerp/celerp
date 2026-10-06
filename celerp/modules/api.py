# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Stable API surface for Celerp module authors.

Module authors may import from this file. Celerp internals remain private to the
application and can change without becoming part of the module contract.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import HTTPException, status

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from starlette.requests import Request

_API_REQUEST_TIMEOUT = 10.0


async def api_request(
    request: "Request",
    method: str,
    path: str,
    *,
    json: Any = None,
    params: dict | None = None,
) -> httpx.Response:
    """Call the Celerp API as the user signed in on ``request``.

    ``path`` is a path inside Celerp, such as "/companies/me"; the address, the
    timeout and the sign-in come from Celerp. Raises ValueError for any other path.
    """
    import ui.config
    from celerp.services.app_paths import is_app_local_path

    if not is_app_local_path(path):
        raise ValueError(f"api_request takes a path inside Celerp, such as /companies/me, not {path!r}.")
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    token = credential.strip() if scheme.lower() == "bearer" else ui.config.get_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=ui.config.API_BASE, timeout=_API_REQUEST_TIMEOUT,
                                 follow_redirects=False, trust_env=False) as client:
        return await client.request(method, path, json=json, params=params, headers=headers)


def read_resource(module_file: str, relative_path: str) -> bytes:
    """Read a file shipped with the calling module.

    ``module_file`` is the caller's own ``__file__``; ``relative_path`` names a file
    in that file's folder or below it, such as "templates/invoice.html". Raises
    ValueError for any other file.
    """
    caller = Path(sys._getframe(1).f_code.co_filename).resolve()
    if Path(module_file).resolve() != caller:
        raise ValueError("read_resource takes the calling file's own __file__.")
    if Path(relative_path).is_absolute():
        raise ValueError(f"read_resource takes a path relative to the module, not {relative_path!r}.")
    target = (caller.parent / relative_path).resolve()
    if not target.is_relative_to(caller.parent):
        raise ValueError(f"{relative_path!r} is not inside the module.")
    return target.read_bytes()


async def ai_query(
    query: str,
    company_id: str,
    session_token: str | None = None,
    db_session: "AsyncSession | None" = None,
) -> dict:
    """Run an AI query for a company through the active Celerp service.

    ``db_session`` is the database session of the request the query is made for;
    the query runs only for that request's company and for a user allowed to use
    the AI assistant. ``session_token`` is optional: without it, the installation's
    own Celerp Connect session is used.
    """
    from celerp.services.permissions import assert_role_permission, read_authority, request_authority
    from celerp.session_gate import require_active_session, validate_session_token

    authority = request_authority(db_session, company_id) if db_session is not None else None
    if authority is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI queries run only for the company of the signed-in request.",
        )
    role, settings = await read_authority(db_session, authority.company_id, authority.user_id)
    assert_role_permission(settings, role, "use_ai_assistant")
    if session_token is None:
        await require_active_session()
    else:
        validate_session_token(session_token)

    from celerp.ai.service import AIResponse, run_query

    result: AIResponse = await run_query(
        query=query,
        session=db_session,
        company_id=authority.company_id,
    )
    return {
        "answer": result.answer,
        "model_used": result.model_used,
        "tools_called": result.tools_called,
    }
