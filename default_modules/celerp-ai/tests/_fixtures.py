# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Synthetic model responses used by AI tests."""
from __future__ import annotations

import copy
import json

from celerp.ai.llm import ModelResult


_SYNTHETIC_RESPONSES = {
    "text_completion": {
        "message": {
            "role": "assistant",
            "content": "You currently have 42 test items in stock.",
        },
        "model_used": "test-model",
        "usage": {"total_tokens": 187},
        "reservation_id": "test-reservation",
        "remaining": 199,
    },
    "read_tool_call": {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_read_0001",
                "type": "function",
                "function": {
                    "name": "list_items",
                    "arguments": json.dumps({"query": {"q": "widget"}}),
                },
            }],
        },
        "model_used": "test-model",
        "usage": {"total_tokens": 204},
        "reservation_id": "test-reservation",
        "remaining": 199,
    },
    "mutation_tool_call": {
        "message": {
            "role": "assistant",
            "content": "I can create that test contact.",
            "tool_calls": [{
                "id": "call_write_0001",
                "type": "function",
                "function": {
                    "name": "create_contact",
                    "arguments": json.dumps({
                        "body": {
                            "name": "Example Supplier",
                            "contact_type": "supplier",
                        }
                    }),
                },
            }],
        },
        "model_used": "test-model",
        "usage": {"total_tokens": 231},
        "reservation_id": "test-reservation",
        "remaining": 198,
    },
    "error_402": {
        "detail": {
            "code": "quota_exceeded",
            "message": "No test credits remain.",
        }
    },
    "error_409": {
        "detail": {
            "code": "continuation_expired",
            "message": "Start a new question.",
        }
    },
    "error_429": {
        "detail": {
            "code": "rate_limited",
            "message": "The service is busy. Try again.",
        }
    },
}


def synthetic_response(name: str) -> dict:
    """Return an isolated synthetic response by test-case name."""
    return copy.deepcopy(_SYNTHETIC_RESPONSES[name])


def model_result(envelope: dict) -> ModelResult:
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


_TOOL_CALL_TEMPLATE = _SYNTHETIC_RESPONSES["read_tool_call"]["message"]["tool_calls"][0]


def tool_call(name: str, arguments: dict, call_id: str) -> dict:
    call = copy.deepcopy(_TOOL_CALL_TEMPLATE)
    call["id"] = call_id
    call["function"]["name"] = name
    call["function"]["arguments"] = json.dumps(arguments)
    return call


def raw_tool_call(name: str, raw_arguments: str, call_id: str) -> dict:
    call = copy.deepcopy(_TOOL_CALL_TEMPLATE)
    call["id"] = call_id
    call["function"]["name"] = name
    call["function"]["arguments"] = raw_arguments
    return call


def tool_result(
    *,
    content: str = "",
    calls: list[dict] | None = None,
    model_used: str = "test-model",
    reservation_id: str | None = "test-reservation",
    remaining: int | None = 199,
    usage: dict | None = None,
) -> ModelResult:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return ModelResult(
        message=message,
        model_used=model_used,
        usage=usage or {"total_tokens": 128},
        reservation_id=reservation_id,
        remaining=remaining,
    )
