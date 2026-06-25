# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""LLM client - single entry point for all model calls.

Calls are served through the cloud gateway, which selects the model and meters
usage. Supports text-only and multimodal (image/PDF) messages.
Concurrency-limited via a module-level semaphore.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from celerp.gateway.state import relay_http_url, relay_session_headers

log = logging.getLogger(__name__)

_MAX_CONCURRENT = int(os.getenv("AI_MAX_CONCURRENT", "3"))
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)


def _build_user_content(
    text: str,
    files: list[dict] | None = None,
) -> str | list[dict]:
    """Build the user message content block.

    Text-only: returns a plain string.
    With files: returns a list of content parts (image_url or text).
    Each file dict must have keys: media_type (str), data (base64 str).
    """
    if not files:
        return text

    parts: list[dict] = []
    for f in files:
        data_uri = f"data:{f['media_type']};base64,{f['data']}"
        parts.append({"type": "image_url", "image_url": {"url": data_uri}})
    parts.append({"type": "text", "text": text})
    return parts


async def call_llm(
    model: str,
    system: str,
    user_text: str,
    files: list[dict] | None = None,
    max_tokens: int = 2048,
    history: list[dict[str, str]] | None = None,
    timeout: float = 60.0,
) -> str:
    """Run a completion through the gateway and return the assistant's text.

    Args:
        model: advisory only — the gateway selects the served model.
        history: Optional prior conversation messages [{"role": ..., "content": ...}].

    Raises HTTPException(402) when the plan's quota is exhausted.
    Raises RuntimeError on other failures (no session, gateway error).
    """
    user_content = _build_user_content(user_text, files)

    messages: list[dict] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_content})

    file_count = len(files) if files else 0
    body = {
        "messages": messages,
        "hints": {
            "query": user_text[:500],
            "file_count": file_count,
            "is_batch": file_count > 1,
        },
        "max_tokens": max_tokens,
    }

    headers = relay_session_headers()
    if not headers.get("X-Session-Token"):
        raise RuntimeError("The AI service is not available - no active cloud session.")

    url = f"{relay_http_url()}/ai/complete"

    async with _semaphore:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=body)

    if resp.status_code == 200:
        return resp.json().get("answer", "")

    if resp.status_code == 402:
        from fastapi import HTTPException
        try:
            detail = resp.json().get("detail", {})
        except Exception:
            detail = {}
        if not isinstance(detail, dict):
            detail = {"code": "quota_exceeded", "message": str(detail)}
        raise HTTPException(status_code=402, detail=detail)

    if resp.status_code in (429, 503):
        raise RuntimeError("The AI service is temporarily busy.")

    raise RuntimeError(f"LLM gateway error {resp.status_code}")
