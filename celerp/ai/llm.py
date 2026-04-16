# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""OpenRouter LLM client - single entry point for all model calls.

Uses the OpenAI-compatible chat completions API at openrouter.ai.
Supports text-only and multimodal (image/PDF) messages.
Handles retry with exponential backoff on 429s.
Concurrency-limited via a module-level semaphore.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
_MAX_RETRIES = 3
_MAX_CONCURRENT = int(os.getenv("AI_MAX_CONCURRENT", "3"))
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)


def _api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured.")
    return key


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
    timeout: float = 45.0,
) -> str:
    """Call OpenRouter and return the assistant's text response.

    Args:
        history: Optional prior conversation messages [{"role": "user"|"assistant", "content": "..."}].
                 Inserted between system and the current user message.

    Raises RuntimeError on permanent failures (missing key, non-retryable errors,
    exhausted retries). Retries on 429 with exponential backoff + jitter.
    """
    api_key = _api_key()
    user_content = _build_user_content(user_text, files)

    messages: list[dict] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_content})

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://celerp.com",
        "X-Title": "Celerp AI",
    }

    async with _semaphore:
        for attempt in range(_MAX_RETRIES + 1):
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(_BASE_URL, headers=headers, json=payload)

            if resp.status_code == 200:
                body = resp.json()
                choices = body.get("choices", [])
                if not choices:
                    raise RuntimeError("LLM returned empty choices.")
                return choices[0]["message"]["content"]

            if resp.status_code == 429:
                if attempt >= _MAX_RETRIES:
                    raise RuntimeError(
                        f"LLM rate limit exceeded after {_MAX_RETRIES} retries."
                    )
                retry_after = float(resp.headers.get("retry-after", 2 ** attempt))
                jitter = random.uniform(0, 0.5)
                wait = retry_after + jitter
                log.warning(
                    "LLM rate limited (attempt %d/%d). Waiting %.1fs.",
                    attempt + 1, _MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                continue

            raise RuntimeError(
                f"LLM API error {resp.status_code}: {resp.text[:400]}"
            )

    raise RuntimeError("call_llm: exhausted retry loop unexpectedly")
