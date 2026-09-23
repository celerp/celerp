# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Loaders for the recorded relay wire fixtures.

Every fake-model test builds its scripted responses from the JSON captured in
fixtures/relay/*.json so the loop is exercised against the exact envelope the
relay sends, including tool-call arguments arriving as a JSON string. Recording
is done once by a person (scripts/record_relay_fixtures.py); tests never call
the relay.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from celerp.ai.llm import ModelResult

_RELAY_DIR = Path(__file__).parent / "fixtures" / "relay"


def load_relay(name: str) -> dict:
    """Return the parsed relay fixture envelope (or error body) by base name."""
    return json.loads((_RELAY_DIR / f"{name}.json").read_text())


def model_result(envelope: dict) -> ModelResult:
    """Build a ModelResult from a recorded relay envelope, as complete() would."""
    message = envelope.get("message")
    if not isinstance(message, dict):
        message = {"role": "assistant", "content": envelope.get("answer", "")}
    return ModelResult(
        message=message,
        model_used=envelope.get("model_used", ""),
        usage=envelope.get("usage") or {},
        reservation_id=envelope.get("reservation_id"),
        remaining=envelope.get("remaining"),
    )


_TOOL_CALL_TEMPLATE = load_relay("read_tool_call")["message"]["tool_calls"][0]


def tool_call(name: str, arguments: dict, call_id: str) -> dict:
    """Clone the recorded tool_call wire shape with a new name/arguments/id.

    Arguments are serialized to the JSON string form the model emits, matching
    the recorded fixture exactly.
    """
    call = copy.deepcopy(_TOOL_CALL_TEMPLATE)
    call["id"] = call_id
    call["function"]["name"] = name
    call["function"]["arguments"] = json.dumps(arguments)
    return call


def raw_tool_call(name: str, raw_arguments: str, call_id: str) -> dict:
    """As tool_call, but with a caller-supplied raw arguments string.

    For the invalid-JSON and shape-hint cases that need a non-object or malformed
    arguments string in the recorded wire form.
    """
    call = copy.deepcopy(_TOOL_CALL_TEMPLATE)
    call["id"] = call_id
    call["function"]["name"] = name
    call["function"]["arguments"] = raw_arguments
    return call


def tool_result(
    *,
    content: str = "",
    calls: list[dict] | None = None,
    model_used: str = "z-ai/glm-5.3-flash",
    reservation_id: str | None = "11111111-1111-4111-8111-111111111111",
    remaining: int | None = 199,
    usage: dict | None = None,
) -> ModelResult:
    """Build a scripted ModelResult (message with optional tool_calls)."""
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return ModelResult(
        message=message,
        model_used=model_used,
        usage=usage or {"total_tokens": 128, "credits": 1, "cost": 0.0002},
        reservation_id=reservation_id,
        remaining=remaining,
    )
