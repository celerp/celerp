# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Stable API surface for Celerp module authors.

Module authors may import from this file. Celerp internals remain private to the
application and can change without becoming part of the module contract.
"""
from __future__ import annotations

import sys
import uuid
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
    token = credential.strip() if scheme.lower() == "bearer" else ""
    token = token or ui.config.get_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=ui.config.API_BASE, timeout=_API_REQUEST_TIMEOUT,
                                 follow_redirects=False, trust_env=False) as client:
        return await client.request(method, path, json=json, params=params, headers=headers)


def read_resource(module_file: str, relative_path: str) -> bytes:
    """Read a file shipped with the calling module.

    Call it from the module's own code. ``module_file`` is the caller's own
    ``__file__``; ``relative_path`` names a file in that file's folder or below it,
    such as "templates/invoice.html". Raises ValueError for any other file or caller.
    """
    from celerp.modules.loader import admitted_module_root

    frame = sys._getframe(1)
    name = frame.f_globals.get("__name__")
    root = admitted_module_root(name) if isinstance(name, str) else None
    if root is None:
        raise ValueError("read_resource reads files shipped with a loaded module only.")
    caller = Path(frame.f_code.co_filename).resolve()
    if Path(module_file).resolve() != caller or not caller.is_relative_to(root.resolve()):
        raise ValueError("read_resource takes the calling file's own __file__.")
    if Path(relative_path).is_absolute():
        raise ValueError(f"read_resource takes a path relative to the module, not {relative_path!r}.")
    target = (caller.parent / relative_path).resolve()
    if not target.is_relative_to(caller.parent):
        raise ValueError(f"{relative_path!r} is not inside the module.")
    if not target.is_file():
        raise ValueError(f"{relative_path!r} is not a file shipped with the module.")
    return target.read_bytes()


async def _ai_caller(company_id: str, db_session: "AsyncSession | None"):
    """The signed-in user a module's AI call is made for, allowed to use the assistant."""
    from celerp.services.auth import signed_request_context
    from celerp.services.permissions import assert_role_permission, read_authority

    caller = await signed_request_context(db_session) if db_session is not None else None
    if caller is None or str(caller.company_id) != str(company_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI queries run only for the company of the signed-in request.",
        )
    role, settings = await read_authority(db_session, caller.company_id, caller.user.id)
    assert_role_permission(settings, role, "use_ai_assistant")
    return caller


def _ai_outcome(out: dict) -> dict:
    """What a module is told of a question: ``running`` (or ``interrupted``) with its
    ``job_id``, or how it ended with the answer. A refusal is raised as it was given."""
    if "refusal" in out:
        refusal = out["refusal"]
        raise HTTPException(status_code=refusal["status"], detail=refusal["detail"], headers=refusal["headers"])
    if out["status"] in ("running", "interrupted"):
        return {"status": out["status"], "job_id": out["job_id"]}
    return {"status": out["status"], "job_id": out["job_id"], "answer": out["error"] or out["answer"],
            "model_used": out["model_used"], "tools_called": out["tools_called"]}


async def ai_query(
    query: str,
    company_id: str,
    session_token: str | None = None,
    db_session: "AsyncSession | None" = None,
) -> dict:
    """Ask the AI assistant a question for a company through the active Celerp service.

    ``db_session`` is the database session of the request the query is made for;
    the query runs only for that request's company and for the user its signed
    access token names, who must be allowed to use the AI assistant.
    ``session_token`` is optional: without it, the installation's own Celerp
    Connect session is used.

    The question runs as a job. Its answer comes back when it ends within the
    synchronous wait (``{"status": "completed" | "failed", "answer", "model_used",
    "tools_called", "job_id"}``); otherwise ``{"status": "running", "job_id"}`` and it
    keeps working in the background: read it later with ``ai_job``.
    """
    import time

    from celerp.ai import jobs
    from celerp.session_gate import require_active_session, validate_session_token

    started = time.monotonic()
    caller = await _ai_caller(company_id, db_session)
    if session_token is None:
        await require_active_session()
    else:
        validate_session_token(session_token)
    return _ai_outcome(await jobs.ask(company_id=caller.company_id, user_id=caller.user.id, query=query,
                                      started=started))


async def ai_job(job_id: str, *, company_id: str, db_session: "AsyncSession | None" = None) -> dict:
    """Where a question ``ai_query`` left running stands, in the same shape: the same
    signed-in request checks apply. An unknown job id, or another company's, is 404."""
    from celerp.ai import jobs

    caller = await _ai_caller(company_id, db_session)
    out = await jobs.question_result(uuid.UUID(str(job_id)), caller.company_id)
    if out is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such AI question.")
    return _ai_outcome(out)
