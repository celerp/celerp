# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Every change the assistant can make, sent again after its answer was lost.

One test per module walks every route of that module that changes data and is not
kept off the assistant. Each must be an assistant tool, and its retry must follow
the rule for its key store:

- Keyed (the route looks up the card's key): a lost answer leaves the card
  retryable, and the retry carries the same identity and the same key, so the
  route's own lookup answers with the first result.
- Unkeyed: a lost answer, or a claim left executing by a crash, makes the outcome
  unknown. The card names the screen to check, and neither a plain confirm nor
  confirm-all sends the change a second time.

The routes are never reached: the tool's request is caught at the transport and
answered with a 502, so every row runs without data of its own. That a keyed
route's lookup really answers the first result is shown on real routes in
test_action_outcomes.
"""

from __future__ import annotations

import json
import os
import types
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import httpx
import pytest
from sqlalchemy import select

from celerp.ai import tools as ai_tools
from celerp.ai.conversations import EXECUTING_STALE_S, add_message
from celerp.ai.service import PendingAction
from celerp.models.ai import AIMessage
from test_helpers import answered_continuation, perm_setup

from test_workflows import _new_conversation, agent_caps, ai_session  # noqa: F401

_MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}
_MODULES = ["celerp", *sorted(
    p.name for p in Path(__file__).resolve().parents[2].iterdir()
    if p.is_dir() and (p / "__init__.py").exists()
)]


def _write_routes(app) -> dict[str, list[tuple[str, str]]]:
    """(method, path) of every route that changes data and is not kept off the
    assistant, by the module that owns it. A preview (confirm off) changes nothing."""
    by_module: dict[str, list[tuple[str, str]]] = {}
    for path, methods, route in ai_tools._api_routes(app):
        extra = route.openapi_extra or {}
        if not route.include_in_schema or "x-celerp-agent-excluded" in extra or extra.get("x-celerp-agent-confirm") is False:
            continue
        owner = ai_tools._agent_route_owner(route.endpoint, {})
        for method in sorted(methods & _MUTATIONS):
            by_module.setdefault(owner or "?", []).append((method, path))
    return by_module


def _one_row(dotted: str) -> dict:
    """A body holding one row on each list ``dotted`` passes through (``records[].key``
    gives ``{"records": [{}]}``), so the executor has a row to fill a per-row key into."""
    head, _, rest = dotted.partition(".")
    if not rest:
        return {}
    inner = _one_row(rest)
    return {head.removesuffix("[]"): [inner] if head.endswith("[]") else inner}


def _selected(dotted: str) -> dict:
    """A body naming one record on the selection path ``dotted`` (``decisions[].line_ids``
    gives ``{"decisions": [{"line_ids": ["1"]}]}``)."""
    head, _, rest = dotted.partition(".")
    if not rest:
        return {head.removesuffix("[]"): ["1"]}
    inner = _selected(rest)
    return {head.removesuffix("[]"): [inner] if head.endswith("[]") else inner}


def _arguments(capability: dict) -> dict:
    """Arguments the executor accepts: every path value, one selected record, one row on
    the list a per-row key sits in, one file."""
    arguments: dict = {}
    if capability["path_names"]:
        arguments["path"] = {name: "1" for name in capability["path_names"]}
    key_rows = _one_row(str(capability.get("key_field") or ""))
    if capability.get("selection") and capability["selection"] not in key_rows:
        arguments["body"] = _selected(capability["selection"].split()[0])
    elif capability.get("expects_body"):
        arguments["body"] = key_rows
    if capability.get("uploads"):
        arguments["file_ids"] = ["f"] * len(capability["uploads"])
    return arguments


class _LostAnswers:
    """Catches every request a tool sends and answers it with a 502, as when the
    connection drops after the request left."""

    def __init__(self):
        self.sent: list[httpx.Request] = []

    def __enter__(self):
        def answer(request: httpx.Request) -> httpx.Response:
            self.sent.append(request)
            return httpx.Response(502, json={"detail": ai_tools.keyed_error("bad_gateway", "api.unreachable")})
        caught = types.SimpleNamespace(**{**vars(httpx), "ASGITransport": lambda **_kw: httpx.MockTransport(answer)})
        self._patches = [
            patch.object(ai_tools, "httpx", caught),
            patch("celerp.ai.files.load_file", lambda *_a: (b"x", {"filename": "a.txt", "content_type": "text/plain"})),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


async def _card(session, conversation_id: str, capability: dict, **state) -> tuple[str, str]:
    """Store one card for ``capability`` and return (message id, tool call id)."""
    now = datetime.now(timezone.utc)
    arguments = _arguments(capability)
    call_id = f"call-{uuid.uuid4().hex[:12]}"
    record = asdict(PendingAction(
        id=call_id, name=capability["name"], arguments=arguments,
        created_at=now.isoformat(), expires_at=(now + timedelta(hours=1)).isoformat(),
        title=capability["name"], keyed=capability["keyed"] is True,
        check_screen=ai_tools.screen_of(capability, arguments, "check_screen"),
    ))
    msg = await add_message(session, uuid.UUID(conversation_id), "assistant", "",
                            tools_called=[{**record, **state}])
    await session.commit()
    return str(msg.id), call_id


async def _status(session, message_id: str, call_id: str) -> dict:
    session.expire_all()
    msg = (await session.execute(select(AIMessage).where(AIMessage.id == uuid.UUID(message_id)))).scalar_one()
    return next(r for r in msg.tools_called if isinstance(r, dict) and r.get("id") == call_id)


async def _confirm(client, headers, conversation_id, message_id, call_id):
    return await client.post(f"/ai/conversations/{conversation_id}/confirm", headers=headers,
                             json={"message_id": message_id, "tool_call_id": call_id})


async def _confirm_all(client, headers, conversation_id, message_id):
    return await client.post(f"/ai/conversations/{conversation_id}/confirm-all", headers=headers,
                             json={"message_id": message_id})


async def _keyed_retry(client, session, headers, conversation_id, capability) -> list[str]:
    message_id, call_id = await _card(session, conversation_id, capability)
    with _LostAnswers() as lost:
        first = await _confirm(client, headers, conversation_id, message_id, call_id)
        status = (await _status(session, message_id, call_id))["status"]
        second = await _confirm(client, headers, conversation_id, message_id, call_id)
    name = capability["name"]
    if first.status_code != 200 or status != "retryable":
        return [f"{name}: a lost answer left {status!r} ({first.status_code}), not retryable"]
    if second.status_code != 200 or len(lost.sent) != 2:
        return [f"{name}: the retry sent {len(lost.sent) - 1} requests ({second.status_code})"]
    if lost.sent[0].content != lost.sent[1].content:
        return [f"{name}: the retry changed what it sent"]
    key_field = capability.get("key_field")
    if key_field and ai_tools.tool_call_key(f"{message_id}:{call_id}").encode() not in lost.sent[0].content:
        return [f"{name}: {key_field} was not filled from the card's tool call"]
    return []


async def _unknown_never_resent(client, session, headers, conversation_id, capability) -> list[str]:
    name = capability["name"]
    problems = []
    message_id, call_id = await _card(session, conversation_id, capability)
    with _LostAnswers() as lost:
        first = await _confirm(client, headers, conversation_id, message_id, call_id)
        record = await _status(session, message_id, call_id)
        again = await _confirm(client, headers, conversation_id, message_id, call_id)
        everything = await _confirm_all(client, headers, conversation_id, message_id)
    if first.status_code != 200 or record["status"] != "unknown":
        return [f"{name}: a lost answer left {record['status']!r} ({first.status_code}), not unknown"]
    if not record.get("check_screen"):
        problems.append(f"{name}: the unknown card names no screen to check")
    if again.status_code != 409 or (again.json().get("detail") or {}).get("check_screen") != record.get("check_screen"):
        problems.append(f"{name}: a plain confirm of an unknown outcome answered {again.status_code}")
    if len(lost.sent) != 1 or everything.status_code != 200:
        problems.append(f"{name}: an unknown outcome was sent {len(lost.sent)} times")

    since = (datetime.now(timezone.utc) - timedelta(seconds=EXECUTING_STALE_S + 60)).isoformat()
    message_id, call_id = await _card(session, conversation_id, capability,
                                      status="executing", executing_since=since)
    with _LostAnswers() as lost:
        stale = await _confirm(client, headers, conversation_id, message_id, call_id)
        await _confirm_all(client, headers, conversation_id, message_id)
    if stale.status_code != 409 or lost.sent:
        problems.append(f"{name}: a crashed claim was sent again ({stale.status_code}, {len(lost.sent)} requests)")
    return problems


# Each module's tools replay in this many slices (every SLICES-th tool, in name order), so a
# test's time stays well under the per-test timeout however many tools a module has.
SLICES = 4


@pytest.mark.asyncio
@pytest.mark.parametrize("module,part", [(m, k) for m in _MODULES for k in range(SLICES)])
async def test_agent_replay(client, session, agent_caps, ai_session, module, part):
    """Each write route of ``module`` is a tool whose retry follows its key store."""
    from celerp.main import app
    by_module = _write_routes(app)
    routes = by_module.get(module, [])
    tools = {(c["method"], c["path"]): c for c in agent_caps.values() if c["requires_confirmation"]}
    problems = [f"{m} {p}: not a tool" for m, p in routes if (m, p) not in tools]
    if module == "celerp":
        problems += [f"{m} {p}: no module owns it" for m, p in by_module.get("?", [])]
    owned = {key for key, c in tools.items() if c["module"] == module}
    assert owned <= set(routes), sorted(owned - set(routes))

    headers = (await perm_setup(client, session))["admin_h"]
    conversation_id = await _new_conversation(client, headers)
    async with answered_continuation():
        for key in sorted(owned)[part::SLICES]:
            capability = tools[key]
            run = _keyed_retry if capability["keyed"] else _unknown_never_resent
            problems += await run(client, session, headers, conversation_id, capability)
    assert problems == [], json.dumps(problems, indent=1)
