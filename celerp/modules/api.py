# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Stable API surface for Celerp module authors.

Module authors may import from this file. Celerp internals remain private to the
application and can change without becoming part of the module contract.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def ai_query(
    query: str,
    company_id: str,
    db_session: "AsyncSession",
) -> dict:
    """Run an AI query for a company through the active Celerp service."""
    from fastapi import HTTPException, status
    from celerp.gateway.state import get_session_token

    if not get_session_token():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "No active Celerp Connect session. Reconnect web access under "
                "Settings > Web Access, then try again."
            ),
        )

    from celerp.ai.service import AIResponse, run_query

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
