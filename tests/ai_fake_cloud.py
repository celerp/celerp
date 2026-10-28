# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The AI service played over HTTP by its step rules, for the job runner tests.

Every model call names one step identity: a first turn's ``operation_id``, or a later
turn's ``step_id`` beside the question's ``reservation_id``. The service answers an
identity by what it holds for it:

- unseen: the model runs (counted in ``executions``) and the reply is stored;
- a stored reply: replayed, no model run;
- running: 409 ``operation_in_progress``;
- an outcome it cannot report (the reply was lost): 503 ``outcome_unknown`` with
  ``charged`` true or false, never run again;
- fenced (a status read asked about it before it was ever sent): 409
  ``operation_expired``, never run.

A call held on ``hold[identity]`` stays in the model until the event is set. When the
app gives up on a held call (its task is cancelled), the service goes on as the real
attempt runner does and ends the step by ``on_disconnect[identity]``: ``"store"``
(answered, reply kept), ``"lost_charged"`` or ``"lost_uncharged"``.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from typing import Awaitable, Callable

import httpx

LIMITS = {"attempt_deadline_s": 120, "settle_margin_s": 15, "guard_s": 5, "retry_window_s": 86400,
          "max_body_bytes": 20 * 1024 * 1024, "max_tokens": 4000}

Reply = dict  # an assistant message


def answer(content: str = "Done.") -> Reply:
    return {"role": "assistant", "content": content}


def tool_turn(*calls: tuple[str, dict, str]) -> Reply:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for name, args, cid in calls]}


class FakeCloud:
    def __init__(self, script: Callable[[str, dict], Reply] | None = None):
        self.script = script or (lambda identity, body: answer())
        self.state: dict[str, str] = {}        # identity -> running | done | lost_charged | lost_uncharged | fenced
        self.replies: dict[str, Reply] = {}
        self.executions: Counter = Counter()   # model runs per identity
        self.sends: Counter = Counter()        # /ai/complete requests per identity
        self.bodies: list[dict] = []
        self.hold: dict[str, asyncio.Event] = {}
        self.on_disconnect: dict[str, str] = {}
        self.in_model = 0
        self.max_in_model = 0
        self.arrived: list[str] = []           # identities in the order the model started them
        self.status_asks: list[list[str]] = []

    # ── wiring ────────────────────────────────────────────────────────────────
    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ai/limits":
            return httpx.Response(200, json=LIMITS)
        if path == "/ai/operations/status":
            return self._status(json.loads(request.content)["operation_ids"])
        assert (request.method, path) == ("POST", "/ai/complete"), path
        body = json.loads(request.content)
        self.bodies.append(body)
        identity = body.get("step_id") or body.get("operation_id") or str(uuid.uuid4())
        self.sends[identity] += 1
        return await self._complete(identity, body)

    # ── the step rules ────────────────────────────────────────────────────────
    async def _complete(self, identity: str, body: dict) -> httpx.Response:
        state = self.state.get(identity)
        if state == "done":
            return self._reply(identity, body, replayed=True)
        if state == "running":
            return _refusal(409, "operation_in_progress", retry_after=0)
        if state in ("lost_charged", "lost_uncharged"):
            charged = state == "lost_charged"
            return _refusal(503, "outcome_unknown", charged=charged, credits=1 if charged else 0)
        if state == "fenced":
            return _refusal(409, "operation_expired")
        self.state[identity] = "running"
        self.executions[identity] += 1
        self.arrived.append(identity)
        self.in_model += 1
        self.max_in_model = max(self.max_in_model, self.in_model)
        try:
            reply = self.script(identity, body)
            if isinstance(reply, BaseException):
                raise reply
            if identity in self.hold:
                await self.hold[identity].wait()
        except asyncio.CancelledError:
            end = self.on_disconnect.get(identity, "store")
            if end == "store":
                self.replies[identity] = reply
                self.state[identity] = "done"
            else:
                self.state[identity] = end
            raise
        finally:
            self.in_model -= 1
        self.replies[identity] = reply
        self.state[identity] = "done"
        return self._reply(identity, body, replayed=False)

    def _reply(self, identity: str, body: dict, *, replayed: bool) -> httpx.Response:
        question = body.get("reservation_id") or body.get("operation_id")
        return httpx.Response(200, json={
            "message": self.replies[identity], "model_used": "test-model", "usage": {"credits": 1},
            "reservation_id": question, "remaining": 9, "operation_credits": 1, "replayed": replayed})

    def _status(self, ids: list[str]) -> httpx.Response:
        self.status_asks.append(list(ids))
        out = []
        for op in ids:
            state = self.state.get(op)
            if state is None:
                self.state[op] = "fenced"
                out.append({"operation_id": op, "state": "unknown_id", "credits": 0})
            elif state in ("done", "lost_charged"):
                out.append({"operation_id": op, "state": "charged", "credits": 1})
            elif state == "running":
                out.append({"operation_id": op, "state": "in_progress", "credits": 0})
            else:
                out.append({"operation_id": op, "state": "not_charged", "credits": 0})
        return httpx.Response(200, json={"operations": out})

    # ── helpers for tests ─────────────────────────────────────────────────────
    def held(self, *identities: str, end: str = "store") -> None:
        for identity in identities:
            self.hold[identity] = asyncio.Event()
            self.on_disconnect[identity] = end

    def release(self, *identities: str) -> None:
        for identity in identities:
            self.hold.pop(identity).set()

    async def until(self, predicate: Callable[[], bool], timeout: float = 10.0) -> None:
        await until(predicate, timeout)


def _refusal(status: int, code: str, **detail) -> httpx.Response:
    return httpx.Response(status, json={"detail": {"code": code, "message": code, **detail}})


async def until(predicate: Callable[[], bool | Awaitable[bool]], timeout: float = 10.0) -> None:
    """Wait for ``predicate`` (sync or async), polling; fails the test at ``timeout``."""
    async def _wait():
        while True:
            value = predicate()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_wait(), timeout)


def install(monkeypatch, cloud: FakeCloud) -> FakeCloud:
    """Route every app call to the AI service through ``cloud``."""
    from celerp.ai import deadlines, llm
    from celerp.gateway.state import set_session_token

    real = httpx.AsyncClient
    set_session_token("s" * 64)
    monkeypatch.setattr(llm, "relay_http_url", lambda: "https://relay.test")
    monkeypatch.setattr(deadlines, "relay_http_url", lambda: "https://relay.test")
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(cloud.handle), **kw))
    deadlines.forget_limits()
    return cloud
