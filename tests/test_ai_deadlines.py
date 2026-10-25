# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The app's AI clocks come from one module, celerp.ai.deadlines, and from the limits
the AI service publishes. Every model call declares its step budget to the service,
so a stale or missing copy of the limits only ever shortens a step; every clock the
app holds is ordered after the service's own end of the same step.
"""
from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import httpx
import pytest

from celerp.ai import deadlines, llm
from celerp.gateway.state import RELAY_CONNECT_ATTEMPTS, RELAY_CONNECT_TIMEOUT_S, set_session_token
from celerp.services.cloud_entitlement import RELAY_ENTITLEMENT_TIMEOUT

REPO = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def _limits(d=120.0, s=15.0, g=5.0, w=86400.0, b=20 * MIB, t=4000) -> "deadlines.Limits":
    return deadlines.Limits(attempt_deadline_s=d, settle_margin_s=s, guard_s=g, retry_window_s=w,
                            max_body_bytes=b, max_tokens=t)


# ── The constants the contract names ──────────────────────────────────────────

def test_current_relay_defaults_mirror_the_service_contract():
    """The cloud's config is authoritative; this is its mirror (contract doc values)."""
    assert deadlines.CURRENT_RELAY_DEFAULTS == _limits()


def test_older_relay_limits_are_the_pairing_that_relay_shipped_with():
    """An older relay runs 60 s per phase and the app called it with 90 s."""
    older = deadlines._OLDER_RELAY_LIMITS
    assert older.attempt_deadline_s == 60
    assert deadlines.call_budget(older, 0).total_s == 90


def test_app_constants():
    assert deadlines.RESPONSE_LATENCY_S == 10
    assert deadlines.FLOOR_UPLOAD_BYTES_PER_S == 250_000
    assert deadlines.UI_REQUEST_S == 10
    assert deadlines.TOOL_STEP_S == 30
    assert deadlines.PROMOTE_AFTER_S == 120
    assert deadlines.SYNC_WAIT_S == 120
    assert deadlines.REMOTE_PROXY_TIMEOUT_S == 180
    assert deadlines.ACTIONS_TIMEOUT_S == 150


def test_default_budgets():
    current = deadlines.CURRENT_RELAY_DEFAULTS
    connect = RELAY_CONNECT_ATTEMPTS * RELAY_CONNECT_TIMEOUT_S + RELAY_ENTITLEMENT_TIMEOUT
    assert deadlines.upload_budget_s(0) == connect == 12
    assert deadlines.call_budget(current, 0).total_s == 157
    assert deadlines.upload_budget_s(20 * MIB) == pytest.approx(96, abs=1)
    assert deadlines.call_budget(current, 20 * MIB).total_s == pytest.approx(241, abs=1)
    assert deadlines.status_timeout_s(current) == 25


def test_synchronous_wait_fits_under_the_remote_access_proxy():
    assert deadlines.SYNC_WAIT_S + deadlines.UI_REQUEST_S <= deadlines.REMOTE_PROXY_TIMEOUT_S - 30


# ── The ordering, over generated limits (invariant 5) ─────────────────────────

def _generated(n: int = 2000):
    rng = random.Random(20261025)
    for _ in range(n):
        b = rng.randint(1, 64) * MIB
        yield (_limits(d=rng.uniform(10, 900), s=rng.uniform(1, 120), g=rng.uniform(0.5, 60), b=b),
               rng.randint(0, b))


def test_every_app_clock_ends_after_the_service_ends_the_same_step():
    for limits, nbytes in _generated():
        budget = deadlines.call_budget(limits, nbytes)
        u = deadlines.upload_budget_s(nbytes)
        assert budget.upload_s == u
        # The app declares the budget it holds; the service runs min(D, declared).
        assert budget.declared_s == limits.attempt_deadline_s
        # The reply can only come after the service ended and settled the attempt.
        cloud_last = u + budget.declared_s + limits.settle_margin_s
        assert budget.total_s == pytest.approx(cloud_last + deadlines.RESPONSE_LATENCY_S)
        # The row is read back only after the service's last possible settlement.
        assert deadlines.reconcile_delay_s(limits, nbytes) > cloud_last
        assert deadlines.reconcile_delay_s(limits, nbytes) == pytest.approx(budget.total_s + limits.guard_s)
        assert deadlines.status_timeout_s(limits) == pytest.approx(
            limits.settle_margin_s + deadlines.RESPONSE_LATENCY_S)


# ── Source guards: no AI clock outside deadlines.py ───────────────────────────

_GONE = ("MODEL_CALL_TIMEOUT_S", "STATUS_TIMEOUT_S", "MAX_IN_PROGRESS_WAIT_S", "AGENT_RUN_TIMEOUT_S",
         "CAPABILITY_TIMEOUT_S", "AI_QUERY_TIMEOUT_S", "RECONCILE_AFTER", "BATCH_CONCURRENCY",
         "fail_interrupted_jobs", "ask_unattended", "_refuse_if_answering", "_answering")


def _ai_sources() -> list[Path]:
    files = [p for p in (REPO / "celerp" / "ai").glob("*.py") if p.name != "deadlines.py"]
    return files + [REPO / "default_modules" / "celerp-ai" / "celerp_ai" / "routes.py",
                    REPO / "ui" / "api_client.py", REPO / "celerp" / "main.py"]


def test_the_replaced_clocks_and_runners_are_gone():
    hits = [f"{p.relative_to(REPO)}: {name}" for p in _ai_sources()
            for name in _GONE if name in p.read_text()]
    assert hits == []


def _literal_timeouts(tree: ast.AST, only_ai_functions: bool) -> list[int]:
    lines = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if only_ai_functions and "ai" not in fn.name.split("_"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.keyword) and node.arg in ("timeout", "total_s") \
                    and isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
                lines.append(node.lineno)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "sleep" \
                    and node.args and isinstance(node.args[0], ast.Constant):
                lines.append(node.lineno)
    return lines


def test_no_ai_clock_literal_outside_deadlines():
    found = {}
    for p in (REPO / "celerp" / "ai").glob("*.py"):
        if p.name != "deadlines.py":
            found[p.name] = _literal_timeouts(ast.parse(p.read_text()), only_ai_functions=False)
    found["ui/api_client.py"] = _literal_timeouts(
        ast.parse((REPO / "ui" / "api_client.py").read_text()), only_ai_functions=True)
    assert {k: v for k, v in found.items() if v} == {}


def test_one_ai_concurrency_setting():
    """AI_MAX_CONCURRENT is read in jobs.py only (one gate)."""
    readers = [p.name for p in (REPO / "celerp" / "ai").glob("*.py") if "AI_MAX_CONCURRENT" in p.read_text()]
    assert readers == ["jobs.py"]


# ── Limits from the service, stale limits (red 21) ────────────────────────────

class _Relay:
    def __init__(self, limits: dict | None | Exception, header: str | None = None):
        self.limits = limits
        self.header = header
        self.limit_reads = 0
        self.declared: list[str | None] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ai/limits":
            self.limit_reads += 1
            if isinstance(self.limits, Exception):
                raise self.limits
            if self.limits is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json=self.limits)
        assert request.url.path == "/ai/complete"
        self.declared.append(request.headers.get("X-AI-Attempt-Budget-S"))
        headers = {"X-AI-Limits": self.header} if self.header else {}
        return httpx.Response(200, headers=headers, json={
            "message": {"role": "assistant", "content": "ok"}, "model_used": "m",
            "usage": {"credits": 1}, "reservation_id": None, "remaining": 9, "operation_credits": 1})


def _published(d=120, s=15, g=5, w=86400) -> dict:
    return {"attempt_deadline_s": d, "settle_margin_s": s, "guard_s": g, "retry_window_s": w,
            "max_body_bytes": 20 * MIB, "max_tokens": 4000}


@pytest.fixture
def relay(monkeypatch):
    holder = {}
    real = httpx.AsyncClient
    set_session_token("s" * 64)
    monkeypatch.setattr(llm, "relay_http_url", lambda: "https://relay.test")
    monkeypatch.setattr(deadlines, "relay_http_url", lambda: "https://relay.test")
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(holder["relay"].handle), **kw))
    deadlines.forget_limits()

    def _make(limits, header=None) -> _Relay:
        holder["relay"] = _Relay(limits, header)
        return holder["relay"]
    yield _make
    deadlines.forget_limits()


async def _call():
    from celerp.ai import jobs
    async with jobs.gate.slot("interactive"):
        return await llm.complete([{"role": "user", "content": "hi"}])


async def test_stale_limits_declare_the_budget_the_app_holds(relay):
    """App holding D = 120 against a service now at D = 150: it declares 120, so the
    service's attempt ends by 120 whatever the service would allow."""
    r = relay(_published(d=120), header="150,15,5,86400")
    await _call()
    assert r.declared == ["120"]
    assert r.limit_reads == 1
    # The reply's header differs from what the app holds: the next call refetches.
    r.limits = _published(d=150)
    await _call()
    assert r.limit_reads == 2
    assert r.declared[-1] == "150"


async def test_limits_are_fetched_once_while_the_header_matches(relay):
    r = relay(_published(), header="120,15,5,86400")
    for _ in range(3):
        await _call()
    assert r.limit_reads == 1


@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
async def test_a_failed_limits_fetch_uses_the_current_defaults_and_retries(relay, failure):
    r = relay(failure)
    assert await deadlines.limits() == deadlines.CURRENT_RELAY_DEFAULTS
    r.limits = _published(d=200)
    assert (await deadlines.limits()).attempt_deadline_s == 200
    assert r.limit_reads == 2


async def test_a_5xx_limits_fetch_uses_the_current_defaults(relay, monkeypatch):
    r = relay(_published())
    r.handle_orig = r.handle
    r.handle = lambda req: httpx.Response(503) if req.url.path == "/ai/limits" else r.handle_orig(req)
    assert await deadlines.limits() == deadlines.CURRENT_RELAY_DEFAULTS


async def test_an_older_relay_gets_the_older_pairing(relay):
    relay(None)
    assert await deadlines.limits() == deadlines._OLDER_RELAY_LIMITS


def test_limits_body_is_parsed_strictly():
    """A body missing a field is a failed fetch, never a guessed value."""
    with pytest.raises(ValueError):
        deadlines.parse_limits(json.loads('{"attempt_deadline_s": 120}'))
