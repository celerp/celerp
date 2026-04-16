# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Public API surface for Celerp module authors.

Module authors MAY import from this file.
Module authors MUST NOT import from celerp.ai.*, celerp.session_gate, or
any other celerp.* internal. The loader will reject modules that do so.

Current public API
------------------
ai_query(query, company_id, session_token, db_session)
    Run an AI query through the Celerp Cloud AI service.
    Requires an active Cloud+AI subscription.
    Raises HTTPException(401) if not subscribed.
    Raises HTTPException(402) if quota exceeded.

Documentation: https://celerp.com/docs/modules/ai-api
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def ai_query(
    query: str,
    company_id: str,
    session_token: str,
    db_session: "AsyncSession",
) -> dict:
    """Run an AI query through the Celerp Cloud AI service.

    Routes through session_gate + quota check. Quota is decremented from
    the Cloud+AI subscription associated with this instance.

    Args:
        query:         Natural language question (1-2000 chars).
        company_id:    Company ID for scoped ERP data access.
        session_token: Gateway session token (from X-Session-Token header).
        db_session:    Active async SQLAlchemy session.

    Returns:
        dict with keys: answer (str), model_used (str), tools_called (list[str])

    Raises:
        HTTPException(401): No active Cloud+AI subscription or invalid token.
        HTTPException(402): AI query quota exceeded.
        HTTPException(400): Query too short/long.

    Example (in a module route handler)::

        from celerp.modules.api import ai_query
        from fastapi import Depends, Request
        from celerp.db import get_session

        @router.post("/my-module/ai")
        async def my_ai_endpoint(
            body: MyRequest,
            request: Request,
            session: AsyncSession = Depends(get_session),
        ):
            session_token = request.headers.get("X-Session-Token", "")
            result = await ai_query(
                query=body.question,
                company_id=body.company_id,
                session_token=session_token,
                db_session=session,
            )
            return {"answer": result["answer"]}
    """
    # Validate session token (revenue gate — runs against live gateway token)
    from celerp.gateway.state import get_session_token
    from fastapi import HTTPException, status

    if not session_token:
        from celerp.config import settings
        subscribe_base = "https://celerp.com/subscribe"
        iid = settings.gateway_instance_id
        url = f"{subscribe_base}?instance_id={iid}#ai" if iid else f"{subscribe_base}#ai"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "AI queries require an active Celerp Cloud+AI subscription. "
                f"Subscribe at {url}"
            ),
        )

    current = get_session_token()
    if not current or session_token != current:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Session token is invalid or expired. "
                "Ensure Cloud Relay is enabled in Settings > Cloud Relay."
            ),
        )

    # Quota check (decrements from Cloud+AI subscription)
    from celerp.ai.quota import check_ai_quota
    await check_ai_quota()

    # Run query through the AI service
    from celerp.ai.service import run_query, AIResponse
    result: AIResponse = await run_query(
        query=query,
        session=db_session,
        company_id=company_id,
    )

    return {
        "answer": result.answer,
        "model_used": result.model_used,
        "tools_called": result.tools_called,
    }
