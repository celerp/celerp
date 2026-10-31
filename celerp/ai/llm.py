# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""LLM client - single entry point for all model calls.

Calls are served through the cloud gateway, which selects the model and meters
usage. Supports text-only and multimodal (image/PDF) messages, plus structured
tool calling for the agent loop. Every call runs inside a slot of the one model
gate (celerp.ai.jobs) and within the budget celerp.ai.deadlines derives for it.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from fastapi import HTTPException

from celerp.ai import deadlines, jobs
from celerp.ai.files import XLSX_CONTENT_TYPE
from celerp.gateway.state import relay_http_url, relay_session_headers, with_relay_client
from ui.i18n import t

log = logging.getLogger(__name__)

# An operation status read reports these states; anything else is an unexpected reply.
OPERATION_STATES = frozenset({"charged", "in_progress", "not_charged", "unknown_id"})
# Bytes per chunk of an uploaded request body.
_UPLOAD_CHUNK = 64 * 1024


class RelayError(RuntimeError):
    """A gateway call failed. ``code`` names the failure for the caller.

    Codes: ``no_session`` (signed out of Celerp Connect), ``no_account`` (no
    Celerp account key on this install), ``unexpected_reply`` (a
    200 without a structured assistant message), ``continuation_expired``
    (the relay dropped the reservation), ``service_unavailable`` (the model
    surely did not run; ask again with the same question id),
    ``outcome_unknown`` (the model may have run but no answer came back; the
    question is charged ``credits``, and the same question id only ever gets this
    outcome again: ask as a new question), ``operation_in_progress`` (the first attempt of this
    question is still running; ask again after ``retry_after`` seconds),
    ``operation_expired`` (the question id can no longer be used; ask as a new
    question), ``operation_mismatch`` (the question id was first sent with
    different content; ask as a new question), ``busy`` (rate limited, or the service is overloaded), and
    ``gateway_error`` (any other non-2xx status, carried in ``status``).
    """

    def __init__(
        self, code: str, message: str, *, status: int | None = None,
        credits: int = 0, retry_after: float | None = None, operation_credits: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.credits = credits
        self.operation_credits = operation_credits
        self.retry_after = retry_after


@dataclass
class ModelResult:
    message: dict            # full choices[0].message from the gateway
    model_used: str
    usage: dict
    reservation_id: str | None
    remaining: int | None
    # A first turn's whole charge for its operation, named by the service on every
    # reply to it (a retry included); None from a service that does not name it.
    operation_credits: int | None = None


# Refusals the caller shows as their own states, raised as an HTTPException
# carrying the service's detail: every 402 (``quota_exceeded``, ``not_available``,
# ``plan_limit``) plus these free-account refusals.
_FREE_REFUSALS = {(413, "free_text_too_large"), (503, "free_paused")}
# An older service sends some 402s without a code; this is its text for a plan
# without AI. Any other untyped 402 is a plan limit.
_NOT_AVAILABLE = "AI is not available on this plan"
# Typed failures that are not refusals, by status and code.
_RELAY_CODES = {
    (503, "service_unavailable"): "service_unavailable",
    (503, "outcome_unknown"): "outcome_unknown",
    (503, "busy"): "busy",
    (429, "rate_limited"): "busy",
    (429, "too_many_concurrent"): "busy",
    (409, "continuation_expired"): "continuation_expired",
    (409, "operation_in_progress"): "operation_in_progress",
    (409, "operation_expired"): "operation_expired",
    (422, "operation_mismatch"): "operation_mismatch",
}


def _retry_after(resp: httpx.Response, detail: dict) -> float:
    """Seconds to wait before asking again, from the detail or the header; 1 when neither says."""
    for value in (detail.get("retry_after"), resp.headers.get("Retry-After")):
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return seconds
    return 1.0


def _error_detail(resp: httpx.Response) -> dict:
    """Return the relay detail without parsing a failure response more than once."""
    try:
        payload = resp.json()
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    value = payload.get("detail")
    if isinstance(value, dict):
        return value
    if value not in (None, ""):
        return {"message": str(value)}
    return {}


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
    - anything else raises ValueError naming the kinds of file that work.

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
            raise ValueError(t("error.ai_file_type"))

    if text:
        parts.append({"type": "text", "text": text})
    return parts


def _upload(body: bytes, deadline: float) -> Callable[[], object]:
    """A fresh body stream per connection attempt; a stream still sending at
    ``deadline`` (loop time) is aborted, so the service never received the whole
    request and cannot have run it."""
    async def _chunks():
        loop = asyncio.get_running_loop()
        for start in range(0, len(body), _UPLOAD_CHUNK):
            if loop.time() > deadline:
                raise RelayError("service_unavailable", "The question could not be sent in time.")
            yield body[start:start + _UPLOAD_CHUNK]
    return _chunks


async def _relay_request(method: str, path: str, *, body: dict | None, budget: deadlines.CallBudget,
                         headers: dict[str, str] | None = None) -> httpx.Response:
    """One AI service call within ``budget`` (connect retries, sign-in, upload and
    reply under one total), with the session headers, or with the install's account
    key when there is no Connect session (a free account)."""
    payload = b"" if body is None else jsonlib.dumps(body).encode()
    upload = _upload(payload, asyncio.get_running_loop().time() + budget.upload_s)
    extra = {**(headers or {}), "Content-Type": "application/json", "Content-Length": str(len(payload))}
    session = relay_session_headers()
    if session.get("X-Session-Token"):
        async def _op(client):
            return await client.request(method, f"{relay_http_url()}{path}", headers={**session, **extra},
                                        content=upload())
        resp = await with_relay_client(budget.total_s, _op)
    else:
        from celerp.config import settings
        if settings.cloud_disconnected:
            raise RelayError("no_session", "The AI service is not available - signed out of Celerp Connect.")
        from celerp.services.cloud_entitlement import authenticated_request
        resp = await authenticated_request(method, path, content=upload, headers=extra, total_s=budget.total_s)
        if resp is None:
            raise RelayError("no_account", "The AI service is not available - no Celerp account.")
    deadlines.note_header(resp.headers.get("X-AI-Limits"))
    return resp


def _count(value) -> int | None:
    """A credit count the service reported, or None when it reported none."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


async def operations_status(operation_ids: list[uuid.UUID]) -> list[dict]:
    """What the service charged for questions this install asked, read only: one
    {operation_id, state, outcome, credits, charged_at} per id, state one of
    OPERATION_STATES. Raises RelayError, or httpx errors, when the service cannot be asked or does not
    answer as described."""
    body = {"operation_ids": [str(i) for i in operation_ids]}
    budget = deadlines.status_budget(await deadlines.limits(), len(jsonlib.dumps(body)))
    resp = await _relay_request("POST", "/ai/operations/status", body=body, budget=budget)
    if resp.status_code != 200:
        raise RelayError("gateway_error", f"LLM gateway error {resp.status_code}", status=resp.status_code)
    reply = resp.json()
    rows = reply.get("operations") if isinstance(reply, dict) else None
    asked = {str(i) for i in operation_ids}
    if (not isinstance(rows, list) or any(
            not isinstance(r, dict) or r.get("operation_id") not in asked
            or r.get("state") not in OPERATION_STATES or _count(r.get("credits")) is None for r in rows)):
        raise RelayError("unexpected_reply", "The AI service returned an unexpected reply.", status=200)
    return rows


async def complete(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    reservation_id: str | None = None,
    operation_id: uuid.UUID | None = None,
    step_id: uuid.UUID | None = None,
    hints: dict | None = None,
    max_tokens: int = 2048,
    before_send: Callable[[int], Awaitable[None]] | None = None,
) -> ModelResult:
    """Run a structured completion through the gateway.

    Posts to {relay}/ai/complete with the session headers, or with the
    install's account key when there is no Connect session. tools,
    tool_choice, reservation_id, operation_id and step_id are sent only when not None.
    The caller holds a slot of the model gate (celerp.ai.jobs.gate); the call's
    budget is computed once the slot is held and declared to the service.
    ``before_send(nbytes)`` is awaited after the budget and before the request
    leaves, with the body's size (a job step journals the send there).

    ``operation_id`` names a question on its first turn, so that asking it again
    after a lost answer is not charged twice; it is never sent with a
    ``reservation_id``, which names the later turns of the same question, each
    named by its own ``step_id`` so a lost later turn is replayed, not rerun.

    Raises HTTPException carrying the relay's detail when the relay refuses
    the question (402, and the free-account 413 and 503, see _FREE_REFUSALS)
    and RelayError for every other gateway failure (see RelayError.code).
    httpx.TimeoutException propagates when the gateway does not answer within the
    call's budget.
    """
    assert jobs.holding_slot(), "a model call runs inside a slot of celerp.ai.jobs.gate"
    body: dict = {"messages": messages, "max_tokens": max_tokens}
    if hints is not None:
        body["hints"] = hints
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if reservation_id is not None and operation_id is not None:
        raise ValueError("A first turn has an operation_id; a later turn has a reservation_id, never both.")
    if reservation_id is not None:
        body["reservation_id"] = reservation_id
    if operation_id is not None:
        body["operation_id"] = str(operation_id)
    if step_id is not None:
        body["step_id"] = str(step_id)

    nbytes = len(jsonlib.dumps(body))
    budget = deadlines.call_budget(await deadlines.limits(), nbytes)
    if before_send is not None:
        await before_send(nbytes)
    resp = await _relay_request("POST", "/ai/complete", body=body, budget=budget,
                                headers=deadlines.declared_header(budget))

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
            operation_credits=_count(data.get("operation_credits")),
        )

    detail = _error_detail(resp)

    if resp.status_code == 402:
        if "code" not in detail:
            # The relay's plain-text 402s: AI is off for the plan, or the
            # question breaks a plan limit (its text says which).
            code = "not_available" if detail.get("message") == _NOT_AVAILABLE else "plan_limit"
            detail = {"code": code, **detail}
        raise HTTPException(status_code=402, detail=detail)

    code = detail.get("code")
    if (resp.status_code, code) in _FREE_REFUSALS:
        raise HTTPException(status_code=resp.status_code, detail=detail)

    known = _RELAY_CODES.get((resp.status_code, code))
    if known is None:
        # Any other 409 is an expired continuation (an older service sends it
        # without a code), and any other 429 or 503 is a busy service.
        known = {409: "continuation_expired", 429: "busy", 503: "busy"}.get(resp.status_code)
    if known is not None:
        raise RelayError(
            known,
            detail.get("message") or "The AI service could not answer.",
            status=resp.status_code,
            credits=int(detail.get("credits") or 0) if known == "outcome_unknown" else 0,
            retry_after=_retry_after(resp, detail) if known == "operation_in_progress" else None,
            operation_credits=_count(detail.get("operation_credits")),
        )

    raise RelayError(
        "gateway_error", f"LLM gateway error {resp.status_code}", status=resp.status_code,
    )
