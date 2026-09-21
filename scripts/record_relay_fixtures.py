#!/usr/bin/env python3
# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Record real relay wire shapes into the test fixtures, once, by hand.

CI never runs this and never spends: the fixtures it writes are replayed with
respx so the app suite proves the client parses what the relay really sends
without a live call. Re-run it only when the relay contract changes; it
overwrites the fixtures in place.

Usage:
    RELAY_HTTP_URL=https://relay.celerp.com \
    CELERP_SESSION_TOKEN=... CELERP_INSTANCE_ID=... \
        python scripts/record_relay_fixtures.py

The session token and instance id belong to a dedicated test instance connected
to the deployed relay (Settings > Web Access issues the token). The three
success shapes cost a few flash-priced credits in total, once.

Volatile identifiers (the fresh reservation id, the per-call tool-call id) are
normalised to the stable placeholders the replay tests key off, so a re-record
changes only the wire shape, never those anchors, and no secret or echoed header
can enter a committed fixture. Error bodies are recorded only when the relay
actually returns their status; a shape that could not be induced is reported and
its fixture left untouched.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

_FIXTURES = Path(__file__).resolve().parents[1] / "default_modules" / "celerp-ai" / "tests" / "fixtures" / "relay"

# Stable anchors the replay tests assert against; a re-record must not churn them.
_RESERVATION_ID = "11111111-1111-4111-8111-111111111111"
_READ_CALL_ID = "call_read_0001"
_WRITE_CALL_ID = "call_write_0001"

_LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "list_items",
        "description": "List inventory items, optionally filtered by a search term.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                },
            },
        },
    },
}

_CREATE_CONTACT_TOOL = {
    "type": "function",
    "function": {
        "name": "create_contact",
        "description": "Create a contact (customer or supplier).",
        "parameters": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "contact_type": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
            "required": ["body"],
        },
    },
}


def _normalise(envelope: dict, *, call_id: str | None) -> dict:
    """Pin the volatile identifiers to their stable anchors, leaving the wire
    shape and metered usage exactly as the relay returned them."""
    out = dict(envelope)
    if out.get("reservation_id"):
        out["reservation_id"] = _RESERVATION_ID
    message = out.get("message")
    if isinstance(message, dict) and message.get("tool_calls") and call_id:
        calls = [dict(c) for c in message["tool_calls"]]
        calls[0] = {**calls[0], "id": call_id}
        out["message"] = {**message, "tool_calls": calls}
    return out


def _write(name: str, body: dict) -> None:
    path = _FIXTURES / f"{name}.json"
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path}")


def _complete(client: httpx.Client, payload: dict) -> httpx.Response:
    return client.post("/ai/complete", json={"max_tokens": 128, **payload})


def _record_success(client: httpx.Client, name: str, payload: dict, *, call_id: str | None) -> None:
    r = _complete(client, payload)
    r.raise_for_status()
    _write(name, _normalise(r.json(), call_id=call_id))


def _record_error(client: httpx.Client, name: str, status: int, payload: dict) -> None:
    """Record an error body only if the relay actually returns that status."""
    r = _complete(client, payload)
    if r.status_code != status:
        print(f"skipped {name}: relay returned {r.status_code}, not {status} "
              f"(induce the {status} condition on the test instance and re-run)")
        return
    _write(name, r.json())


def main() -> int:
    base = os.environ.get("RELAY_HTTP_URL")
    token = os.environ.get("CELERP_SESSION_TOKEN")
    instance = os.environ.get("CELERP_INSTANCE_ID")
    if not (base and token and instance):
        print("RELAY_HTTP_URL, CELERP_SESSION_TOKEN and CELERP_INSTANCE_ID are "
              "required to record fixtures", file=sys.stderr)
        return 1

    headers = {"X-Session-Token": token, "X-Instance-ID": instance}
    with httpx.Client(base_url=base.rstrip("/"), headers=headers, timeout=60.0) as client:
        _record_success(client, "text_completion", {
            "messages": [{"role": "user", "content": "How many items do I have in stock?"}],
        }, call_id=None)

        _record_success(client, "read_tool_call", {
            "messages": [{"role": "user", "content": "Find widgets in stock."}],
            "tools": [_LIST_TOOL],
        }, call_id=_READ_CALL_ID)

        _record_success(client, "mutation_tool_call", {
            "messages": [{"role": "user", "content": "Add Acme Supplies Ltd as a supplier."}],
            "tools": [_CREATE_CONTACT_TOOL],
        }, call_id=_WRITE_CALL_ID)

        # 409: a continuation against an unknown or expired reservation.
        _record_error(client, "error_409", 409, {
            "messages": [{"role": "user", "content": "continue"}],
            "reservation_id": "00000000-0000-4000-8000-000000000000",
        })
        # 402 (quota exhausted) and 429 (rate limited) depend on the instance's
        # live state; recorded when the test instance is in that condition.
        _record_error(client, "error_402", 402, {
            "messages": [{"role": "user", "content": "hello"}],
        })
        _record_error(client, "error_429", 429, {
            "messages": [{"role": "user", "content": "hello"}],
        })

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
