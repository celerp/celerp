# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""LLM client - single entry point for all model calls.

Calls are served through the cloud gateway, which selects the model and meters
usage. Supports text-only and multimodal (image/PDF) messages, plus structured
tool calling for the agent loop. Concurrency-limited via a module-level
semaphore.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

from celerp.ai.files import XLSX_CONTENT_TYPE
from celerp.gateway.state import relay_http_url, relay_session_headers

log = logging.getLogger(__name__)

_MAX_CONCURRENT = int(os.getenv("AI_MAX_CONCURRENT", "3"))
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

# One model call may run this long; the agent run budget in celerp.ai.service
# is wider so that several calls fit inside one run.
MODEL_CALL_TIMEOUT_S = 90.0


class RelayError(RuntimeError):
    """A gateway call failed. ``code`` names the failure for the caller.

    Codes: ``no_session`` (no active cloud session), ``unexpected_reply`` (a
    200 without a structured assistant message), ``continuation_expired``
    (the relay dropped the reservation), ``busy`` (429 or 503), and
    ``gateway_error`` (any other non-2xx status, carried in ``status``).
    """

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class ModelResult:
    message: dict            # full choices[0].message from the gateway
    model_used: str
    usage: dict
    reservation_id: str | None
    remaining: int | None


def _build_user_content(
    text: str,
    files: list[dict] | None = None,
) -> str | list[dict]:
    """Build the user message content block.

    Text-only: returns a plain string.
    With files: returns a list of content parts. Each file dict has keys
    media_type, data (base64), filename and file_id.

    - image/* becomes an image_url part;
    - application/pdf becomes a file part carrying the base64 data-uri;
    - text/csv and xlsx become a text part naming the file and its file_id so
      the model can call the import-preview capability - the bytes are never
      sent to the model;
    - anything else raises ValueError("unsupported file type").

    Empty text with files is valid: no trailing text part is appended.
    """
    if not files:
        return text

    parts: list[dict] = []
    for f in files:
        media_type = f["media_type"]
        if media_type.startswith("image/"):
            data_uri = f"data:{media_type};base64,{f['data']}"
            parts.append({"type": "image_url", "image_url": {"url": data_uri}})
        elif media_type == "application/pdf":
            data_uri = f"data:{media_type};base64,{f['data']}"
            parts.append({
                "type": "file",
                "file": {"filename": f.get("filename") or "document.pdf", "file_data": data_uri},
            })
        elif media_type in ("text/csv", XLSX_CONTENT_TYPE):
            parts.append({
                "type": "text",
                "text": f"Attached file: {f.get('filename')} (file_id {f.get('file_id')})",
            })
        else:
            raise ValueError("unsupported file type")

    if text:
        parts.append({"type": "text", "text": text})
    return parts


async def complete(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    reservation_id: str | None = None,
    hints: dict | None = None,
    max_tokens: int = 2048,
    timeout: float = MODEL_CALL_TIMEOUT_S,
) -> ModelResult:
    """Run a structured completion through the gateway.

    Posts to {relay}/ai/complete with the session headers. tools, tool_choice
    and reservation_id are sent only when not None.

    Raises HTTPException(402) when the plan's quota is exhausted and
    RelayError for every other gateway failure (see RelayError.code).
    httpx.TimeoutException propagates when the gateway does not answer in
    ``timeout`` seconds.
    """
    body: dict = {"messages": messages, "max_tokens": max_tokens}
    if hints is not None:
        body["hints"] = hints
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if reservation_id is not None:
        body["reservation_id"] = reservation_id

    headers = relay_session_headers()
    if not headers.get("X-Session-Token"):
        raise RelayError("no_session", "The AI service is not available - no active cloud session.")

    url = f"{relay_http_url()}/ai/complete"

    async with _semaphore:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=body)

    if resp.status_code == 200:
        data = resp.json()
        message = data.get("message")
        if not isinstance(message, dict):
            raise RelayError("unexpected_reply", "The AI service returned an unexpected reply.", status=200)
        return ModelResult(
            message=message,
            model_used=data.get("model_used", ""),
            usage=data.get("usage") or {},
            reservation_id=data.get("reservation_id"),
            remaining=data.get("remaining"),
        )

    if resp.status_code == 402:
        from fastapi import HTTPException
        try:
            detail = resp.json().get("detail", {})
        except Exception:
            detail = {}
        if not isinstance(detail, dict):
            detail = {"code": "quota_exceeded", "message": str(detail)}
        raise HTTPException(status_code=402, detail=detail)

    if resp.status_code == 409:
        raise RelayError("continuation_expired", "continuation_expired", status=409)

    if resp.status_code in (429, 503):
        raise RelayError("busy", "The AI service is temporarily busy.", status=resp.status_code)

    raise RelayError(
        "gateway_error", f"LLM gateway error {resp.status_code}", status=resp.status_code,
    )


async def call_llm(
    model: str,
    system: str,
    user_text: str,
    files: list[dict] | None = None,
    max_tokens: int = 2048,
    history: list[dict[str, str]] | None = None,
    timeout: float = MODEL_CALL_TIMEOUT_S,
) -> ModelResult:
    """Run a text completion through the gateway.

    The assistant text is ``result.message["content"]``; ``result.usage`` carries
    the credits the gateway metered for the call.

    Args:
        model: advisory only - the gateway selects the served model.
        history: Optional prior conversation messages [{"role": ..., "content": ...}].

    Raises HTTPException(402) when the plan's quota is exhausted and
    RelayError on other gateway failures.
    """
    user_content = _build_user_content(user_text, files)

    messages: list[dict] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_content})

    file_count = len(files) if files else 0
    hints = {
        "query": user_text[:500],
        "file_count": file_count,
        "is_batch": file_count > 1,
    }

    return await complete(messages, hints=hints, max_tokens=max_tokens, timeout=timeout)
