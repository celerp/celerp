# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Whether a period is billed hangs on its document's status, so every change of a document's
status reaches the subscriptions check (billing.guard_bill_status, the doc_status_guard slot).
The check sits in the one door every live event goes through: emit_event builds and applies
each event, and calls the doc_status_guard handlers whenever a document's status changes. These
tests hold the door shut: nothing else builds or applies a live event, every Documents route
that writes a status writes it through emit_event, and each status change a bill can go
through reaches the check."""
from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from agreement_kit import agreement, freeze_day, generate_due, register

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "default_modules" / "celerp-docs" / "celerp_docs"
ENGINE = ROOT / "celerp" / "events" / "engine.py"
# Applies events outside emit_event, and why that can never change a document's status.
APPLIES_OUTSIDE_THE_DOOR = {
    DOCS / "received_legacy.py": "moves legacy rows to received_doc, a separate entity, at startup",
}


def _sources():
    for top in ("celerp", "default_modules", "premium_modules"):
        for path in sorted((ROOT / top).rglob("*.py")):
            if "tests" not in path.parts:
                yield path, ast.parse(path.read_text(encoding="utf-8"))


def _callee(call: ast.Call) -> str | None:
    return call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", None)


def _status_events() -> set[str]:
    """Every event type whose Documents projection branch writes ``current["status"]``."""
    tree = ast.parse((DOCS / "doc_projections.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "apply_documents_event")
    found, branch = set(), next(n for n in fn.body if isinstance(n, ast.If))
    while branch is not None:
        writes = any(isinstance(t, ast.Subscript) and getattr(t.slice, "value", None) == "status"
                     and getattr(t.value, "id", None) == "current"
                     for n in ast.walk(ast.Module(body=branch.body, type_ignores=[]))
                     if isinstance(n, ast.Assign) for t in n.targets)
        test = branch.test
        if writes and isinstance(test, ast.Compare):
            named = test.comparators[0]
            found |= {e.value for e in (named.elts if isinstance(named, ast.Tuple) else [named])
                      if isinstance(e, ast.Constant)}
        branch = branch.orelse[0] if len(branch.orelse) == 1 and isinstance(branch.orelse[0], ast.If) else None
    return found


def test_only_emit_event_builds_or_applies_a_live_event():
    """A writer that built its own LedgerEntry or applied one itself would change a status
    without passing the check."""
    builds, applies = set(), set()
    for path, tree in _sources():
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and _callee(n) == "LedgerEntry":
                builds.add(path)
            if isinstance(n, ast.Call) and _callee(n) == "apply_event" and isinstance(n.func, ast.Attribute):
                applies.add(path)
    assert builds == {ENGINE}
    assert applies == {ENGINE, *APPLIES_OUTSIDE_THE_DOOR}


def test_emit_event_hands_every_document_status_change_to_the_guard():
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    applied = [n for n in ast.walk(fns["emit_event"]) if isinstance(n, ast.Call) and _callee(n) == "apply_event"]
    assert len(applied) == 1
    assert "_doc_applied" in {_callee(n) for n in ast.walk(fns["emit_event"]) if isinstance(n, ast.Call)}
    reads = {n.value for n in ast.walk(fns["_doc_applied"]) if isinstance(n, ast.Constant)}
    assert "doc_status_guard" in reads


def _docs_routes():
    """Every Documents route that writes something, with the status writers it reaches through
    the package's own functions: emit_event of a status event (or of an event type it works
    out), or any other way of changing a stored document."""
    status, funcs, routes = _status_events(), {}, []
    for path in sorted(DOCS.glob("*.py")):
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault(n.name, []).append(n)
                routes += [(f"{d.func.attr.upper()} {d.args[0].value}", n) for d in n.decorator_list
                           if isinstance(d, ast.Call) and getattr(d.func, "attr", None) in ("post", "put", "patch", "delete")
                           and d.args and isinstance(d.args[0], ast.Constant)]

    def writers(fn) -> tuple[set[str], set[str]]:
        found, calls = set(), set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                name = _callee(n)
                if name == "emit_event":
                    kind = {k.arg: k.value for k in n.keywords}.get("event_type")
                    if not isinstance(kind, ast.Constant) or kind.value in status:
                        found.add("emit_event")
                elif name in ("apply_event", "LedgerEntry"):
                    found.add(name)
                elif name in funcs:
                    calls.add(name)
            elif isinstance(n, ast.Assign) and any(getattr(t, "attr", None) == "state" for t in n.targets):
                found.add("state assignment")
        return found, calls

    reached: dict[str, set[str]] = {}

    def reach(name: str, seen: frozenset) -> set[str]:
        if name not in reached and name not in seen:
            out = set()
            for fn in funcs[name]:
                found, calls = writers(fn)
                out |= found
                for callee in calls:
                    out |= reach(callee, seen | {name})
            reached[name] = out
        return reached.get(name, set())

    return {f"{label} ({fn.name})": reach(fn.name, frozenset()) for label, fn in routes}


def test_every_docs_route_that_changes_a_status_reaches_the_guard():
    """A new route that wrote a status any way but emit_event would skip the check: it fails here."""
    routes = {label: found for label, found in _docs_routes().items() if found}
    assert {label: found for label, found in routes.items() if found != {"emit_event"}} == {}
    # The routes that can bring a bill back into billing are among them.
    for route in ("finalize_doc", "send_doc", "unvoid_doc", "revert_doc_to_draft", "void_doc", "record_payment",
                  "receive_po", "reopen_doc"):
        assert any(f"({route})" in label for label in routes), route


def _key() -> dict:
    return {"idempotency_key": str(uuid.uuid4())}


@pytest.mark.asyncio
async def test_each_status_change_a_bill_goes_through_reaches_the_guard(client, monkeypatch):
    """Live: every route that moves a subscription bill between statuses hands the change to
    the subscriptions check, with the status before and after."""
    import celerp_subscriptions.billing as billing

    seen: list[tuple[str | None, str]] = []
    check = billing.guard_bill_status

    async def spy(*, session, entry, transition):
        seen.append(((transition.before or {}).get("status"), transition.after.get("status")))
        await check(session=session, entry=entry, transition=transition)

    monkeypatch.setattr(billing, "guard_bill_status", spy)
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    doc_id = (await generate_due(client, hd, eid)).json()["doc_id"]
    steps = [
        ("void", {"reason": "x", **_key()}),
        ("unvoid", _key()),
        ("revert-to-draft", _key()),
        ("finalize", None),
        ("payment", {"amount": 1, "payment_date": "2026-03-01", "bank_account": "1111", **_key()}),
    ]
    for route, body in steps:
        seen.clear()
        r = await client.post(f"/docs/{doc_id}/{route}", headers=hd, **({"json": body} if body is not None else {}))
        assert r.status_code == 200, (route, r.text)
        assert seen and all(before != after for before, after in seen), (route, seen)
