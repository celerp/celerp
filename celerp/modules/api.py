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
    session_token: str,
    db_session: "AsyncSession",
) -> dict:
    """Run an AI query for a company through the active Celerp service."""
    from celerp.session_gate import validate_session_token

    validate_session_token(session_token)

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
