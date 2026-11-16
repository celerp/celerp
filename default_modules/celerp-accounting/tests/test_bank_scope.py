# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What the bank routes accept and how the assistant proposes them: every record id
goes through one ownership check, invalid values are refused before any write, the
replaced routes are gone, the cards carry their warnings, and every call outside the
database has a failure path with its test."""

from __future__ import annotations

import ast
import functools
import json
import pathlib
import re

import pytest

import bank_statement_helpers as B
from celerp.ai import tools as ai_tools
from test_helpers import real_agent_app

pytestmark = pytest.mark.asyncio

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent / "celerp_accounting"

REMOVED = [
    ("POST", "/accounting/reconciliation/{session_id}/complete"),
    ("POST", "/accounting/reconciliation/{session_id}/import-csv"),
    ("POST", "/accounting/reconciliation/{session_id}/import-file"),
    ("POST", "/accounting/reconciliation/{session_id}/auto-match"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/match"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/unmatch"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/create"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/split"),
    ("PATCH", "/accounting/reconciliation/{session_id}/lines/{line_id}"),
    ("POST", "/accounting/reconciliation/{session_id}/bulk-confirm"),
]

NEW = [
    ("POST", "/accounting/reconciliation/read-statement"),
    ("GET", "/accounting/reconciliation/statement-preview/{job_id}"),
    ("POST", "/accounting/reconciliation/import-statement"),
    ("POST", "/accounting/reconciliation/bank/{bank_id}/suggest"),
    ("GET", "/accounting/reconciliation/bank/{bank_id}/groups"),
    ("POST", "/accounting/reconciliation/bank/{bank_id}/resolve"),
    ("POST", "/accounting/reconciliation/bank/{bank_id}/unmatch"),
    ("POST", "/accounting/reconciliation/bank/{bank_id}/complete"),
    ("POST", "/accounting/reconciliation/bank/{bank_id}/ask"),
    ("POST", "/accounting/reconciliation/bank/{bank_id}/withdraw-question"),
    ("DELETE", "/accounting/reconciliation/{session_id}"),
    ("POST", "/accounting/reconciliation/lines/{line_id}/reclassify"),
    ("GET", "/accounting/bank-questions"),
    ("POST", "/accounting/bank-questions/{line_id}/answer"),
    ("PUT", "/accounting/bank-accounts/{bank_id}/opening"),
]
CHANGED = [("POST", "/accounting/transfers"), ("POST", "/accounting/bank-accounts"),
           ("POST", "/accounting/rules"), ("PATCH", "/accounting/rules/{rule_id}")]

_ID_FIELD = re.compile(r"(_id|_ids|_code|_keys)$|^contact$")


def _openapi():
    return _app().openapi()


def _resolve_ref(spec, schema):
    while isinstance(schema, dict) and "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema


def _fields(spec, schema, seen=None):
    """Every property name of ``schema``, nested models and arrays included."""
    seen = seen or set()
    schema = _resolve_ref(spec, schema)
    if not isinstance(schema, dict) or id(schema) in seen:
        return set()
    seen.add(id(schema))
    out = set()
    for name, prop in (schema.get("properties") or {}).items():
        out.add(name)
        out |= _fields(spec, prop, seen)
    for k in ("items", "anyOf", "oneOf", "allOf"):
        v = schema.get(k)
        for sub in (v if isinstance(v, list) else [v] if v else []):
            out |= _fields(spec, sub, seen)
    return out


async def test_bank_record_fields_are_scoped():
    from celerp_accounting.statement_lines import RECORD_FIELDS
    spec = _openapi()
    missing = []
    for method, path in NEW + CHANGED:
        op = spec["paths"][path][method.lower()]
        names = {p["name"] for p in op.get("parameters", [])}
        body = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
        if body:
            names |= _fields(spec, body)
        for name in names:
            if _ID_FIELD.search(name) and name not in {"idempotency_key"} and name not in RECORD_FIELDS:
                missing.append(f"{method} {path}: {name}")
    assert not missing, missing


async def _two_companies(client):
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    bk2 = await B.bank(client, co["h"], name="Second")
    await B.statement(client, co, bk["id"], [("2026-01-05", "FEE", "-5.00")], opening="0.00", closing="-5.00",
                      start="2026-01-01", end="2026-01-31")
    await B.statement(client, co, bk2["id"], [("2026-01-06", "FEE", "-6.00")], opening="0.00", closing="-6.00",
                      start="2026-01-01", end="2026-01-31")
    other = await B.company(client)
    obk = await B.bank(client, other["h"])
    await B.statement(client, other, obk["id"], [("2026-01-07", "FEE", "-7.00")], opening="0.00", closing="-7.00",
                      start="2026-01-01", end="2026-01-31")
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    line2 = B.all_lines(await B.groups(client, co, bk2["id"]))[0]
    oline = B.all_lines(await B.groups(client, other, obk["id"]))[0]
    return co, bk, bk2, line, line2, other, obk, oline


async def _state(client, co, bk) -> list:
    table = await B.groups(client, co, bk["id"], view="all")
    return sorted((l["id"], l["status"], l["matched_je_ids"], json.dumps(l["suggestion"], sort_keys=True))
                  for l in B.all_lines(table))


async def test_bank_record_fields_refuse_foreign_records(client):
    """Each record field: an id of another company and an id from the company's other bank
    are refused with 404, and nothing changes."""
    from celerp_accounting.statement_lines import RECORD_FIELDS
    co, bk, bk2, line, line2, other, obk, oline = await _two_companies(client)
    url = f"{B.API}/bank/{bk['id']}"
    probes = {
        "line_ids": [
            ("POST", f"{url}/resolve", lambda v: {"decisions": [{"line_ids": [v], "kind": "no_entry"}]}),
            ("POST", f"{url}/unmatch", lambda v: {"line_ids": [v]}),
            ("POST", f"{url}/ask", lambda v: {"line_ids": [v], "note": "?"}),
            ("POST", f"{url}/withdraw-question", lambda v: {"line_ids": [v]}),
        ],
        "target_bank_id": [("POST", f"{url}/resolve", lambda v: {"decisions": [
            {"line_ids": [line["id"]], "kind": "transfer", "target_bank_id": v}]})],
        "bank_id": [("POST", f"{B.API}/bank/{{v}}/suggest", lambda v: {})],
        "session_id": [("DELETE", f"{B.API}/{{v}}", lambda v: {})],
        "line_id": [("POST", f"{B.API}/lines/{{v}}/reclassify", lambda v: {"account_code": "5100"})],
    }
    foreign = {"line_ids": oline["id"], "line_id": oline["id"], "target_bank_id": obk["id"], "bank_id": obk["id"],
               "session_id": oline["statement_id"]}
    other_bank = {"line_ids": line2["id"], "line_id": None, "target_bank_id": None, "bank_id": None,
                  "session_id": None}
    assert set(probes) <= set(RECORD_FIELDS), "every probed field is a scoped record field"
    before = await _state(client, co, bk), await _state(client, other, obk)
    for field, routes in probes.items():
        for method, path, body in routes:
            for value in (foreign[field], other_bank[field]):
                if value is None:
                    continue
                r = await client.request(method, path.replace("{v}", value), json={
                    **body(value), "idempotency_key": B.key()}, headers=co["h"])
                if r.status_code == 200 and "results" in r.json():
                    assert r.json()["results"][0]["ok"] is False, (field, path)
                else:
                    assert r.status_code == 404, (field, path, r.text)
    assert (await _state(client, co, bk), await _state(client, other, obk)) == before


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "0.001", "0", "1e400"], ids=["nan", "inf", "precision", "zero", "huge"])
async def test_bank_value_fields_refuse_invalid_values(client, bad):
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    other = await B.bank(client, co["h"], name="Other")
    await B.statement(client, co, bk["id"], [("2026-01-05", "FEE", "-5.00")], opening="0.00", closing="-5.00",
                      start="2026-01-01", end="2026-01-31")
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    h = {**co["h"], "Content-Type": "application/json"}

    def raw(body: str) -> str:
        return body.replace('"@BAD@"', bad)
    cases = [
        ("POST", f"{B.API}/bank/{bk['id']}/resolve", '{"idempotency_key": "%s", "decisions": [{"line_ids": ["%s"], '
         '"kind": "post", "splits": [{"account_code": "5100", "amount": "@BAD@"}]}]}' % (B.key(), line["id"])),
        ("POST", f"{B.API}/bank/{bk['id']}/resolve", '{"idempotency_key": "%s", "decisions": [{"line_ids": ["%s"], '
         '"kind": "post", "account_code": "5100", "rate": "@BAD@"}]}' % (B.key(), line["id"])),
        ("PUT", f"/accounting/bank-accounts/{bk['id']}/opening",
         '{"idempotency_key": "%s", "amount": "@BAD@", "date": "2026-01-01"}' % B.key()),
        ("POST", "/accounting/transfers", '{"idempotency_key": "%s", "from_bank_id": "%s", "to_bank_id": "%s", '
         '"amount": "@BAD@", "date": "2026-01-05"}' % (B.key(), bk["id"], other["id"])),
    ]
    for method, path, body in cases:
        if bad == "0" and "opening" in path:
            continue  # a zero opening is a real figure
        if bad == "0.001" and '"rate"' in body:
            continue  # a rate is not money: 0.001 is a real exchange rate
        r = await client.request(method, path, content=raw(body), headers=h)
        assert r.status_code == 422, (path, bad, r.text)
    for method, path, body in [
        ("PUT", f"/accounting/bank-accounts/{bk['id']}/opening",
         '{"idempotency_key": "%s", "amount": 1, "date": "2026-13-45"}' % B.key()),
        ("POST", f"{B.API}/bank/{bk['id']}/resolve",
         '{"idempotency_key": "%s", "decisions": [{"line_ids": ["%s"], "kind": "teleport"}]}' % (B.key(), line["id"])),
        ("POST", f"{B.API}/bank/{bk['id']}/ask",
         '{"idempotency_key": "%s", "line_ids": ["%s"], "note": "%s"}' % (B.key(), line["id"], "x" * 5001)),
    ]:
        r = await client.request(method, path, content=body, headers=h)
        assert r.status_code == 422, (path, r.text)
    after = B.all_lines(await B.groups(client, co, bk["id"], view="all"))[0]
    assert after["status"] == "unmatched" and after["matched_je_ids"] == []


async def test_removed_line_routes_are_gone(client):
    app = _app()
    served = {(m, r.path) for r in app.routes for m in getattr(r, "methods", ()) or ()}
    assert not [r for r in REMOVED if r in served]
    compiled = {(c["method"], c["path"]) for c in _caps(None).values()}
    assert not [r for r in REMOVED if r in compiled]
    ui_src = (HERE.parents[2] / "ui" / "api_client.py").read_text()
    for _m, path in REMOVED:
        tail = path.split("{session_id}", 1)[1].replace("{line_id}", "")
        assert not re.search(r"reconciliation/\{[a-z_]+\}" + re.escape(tail.rstrip("/")) + r"[\"'/]", ui_src), path


_ROLES = (None, "owner", "operator", "viewer")


@functools.cache
def _surface():
    """One real API surface for this module, with its OpenAPI schema and each role's
    compiled capabilities (read only), all built while its modules are loaded: the
    loader is reset after every test, so nothing is compiled from it later."""
    app = real_agent_app()
    app.openapi()
    return app, {role: ai_tools.compile_agent_capabilities(app, {}, role) for role in _ROLES}


def _app():
    return _surface()[0]


def _caps(role="owner"):
    return _surface()[1][role]


def _cap(caps, method, path):
    return next(c for c in caps.values() if c["method"] == method and c["path"] == path)


async def test_read_statement_card_states_credits():
    from celerp.ai.service import _card_warnings
    cap = _cap(_caps(), "POST", "/accounting/reconciliation/read-statement")
    assert cap["credits"] is True and cap["keyed"]
    warnings = _card_warnings(cap, None, {"body": {"file_ids": ["a", "b", "c"]}})
    assert "This reads 3 files with AI credits." in [w["text"] for w in warnings]


async def test_resolve_proposed_as_one_card():
    from celerp.ai.service import _card_warnings
    caps = _caps()
    cap = _cap(caps, "POST", "/accounting/reconciliation/bank/{bank_id}/resolve")
    from celerp.ai.service import _selection_size
    assert cap["selection"] == "decisions[].line_ids accept.line_ids" and cap["requires_confirmation"]
    assert "manage_accounting" in cap["required_permissions"]
    decisions = [{"line_ids": [f"l{i}", f"m{i}"], "kind": "no_entry"} for i in range(120)]
    accept = {"group_keys": ["g"], "line_ids": ["a", "b", "c"], "versions": {}}
    body = {"decisions": decisions, "accept": accept}
    assert _selection_size(cap, {"body": body}) == 243
    assert _selection_size(cap, {"body": {"accept": accept}}) == 3
    assert any("240" in w["text"] for w in _card_warnings(cap, 240, {"body": {"decisions": decisions}}))
    for role in ("viewer", "operator"):
        assert not [c for c in _caps(role).values() if c["path"].endswith("/resolve") and "reconciliation" in c["path"]]
    # The card lists the accepted groups read only and links to the bank page, where a
    # changed selection is made by ticking.
    from celerp.ai.card import card_arguments
    from celerp.ai.tools import screen_of
    from celerp_ai.ui_routes import _action_body
    from fasthtml.common import Div, to_xml
    arguments = {"path": {"bank_id": "b1"}, "body": {"accept": {
        "group_keys": ["NETFLIX", "GRAB TRIP"], "line_ids": ["l1"], "versions": {"l1": "abcdef0123456789"}}}}
    link = screen_of(cap, arguments, "card_screen")
    assert link == "/accounting/reconcile/bank/b1"
    html = to_xml(Div(*_action_body({"shown": card_arguments(cap, arguments), "card_screen": link})))
    assert "NETFLIX" in html and "GRAB TRIP" in html and f'href="{link}"' in html
    assert "abcdef0123456789" not in html, "the card leaves out the version fingerprint"
    assert "<input" not in html and "<select" not in html


async def test_continue_after_resolve_proposes_next_group():
    from celerp.ai.service import agent_system_prompt
    text = agent_system_prompt("en", {}, {})
    assert "bank" in text.lower() and "next group" in text.lower()


async def test_import_creates_bank_then_sessions_with_dependent_stop(client):
    co = await B.company(client)
    preview = await B.read(client, co, [(B.csv_bytes([("2026-01-05", "FEE", "-5.00")]), "a.csv", "text/csv")])
    assert preview["bank"] is None and preview["new_bank"]
    # A refused new bank (a type the bank list does not know) leaves the import pending.
    r = await client.post(f"{B.API}/import-statement", json={
        "job_id": preview["job_id"], "new_bank": {"bank_name": "X", "bank_type": "spaceship", "currency": "USD"},
        "statements": [{"header": {**preview["statements"][0]["header"], "opening_balance": "0.00",
                                   "closing_balance": "-5.00", "period_start": "2026-01-01", "period_end": "2026-01-31"},
                        "lines": preview["statements"][0]["lines"]}], "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 422
    banks = (await client.get("/accounting/bank-accounts", headers=co["h"])).json()
    assert not [b for b in banks.get("items", banks) if b["bank_name"] == "X"]  # the company's default bank stays
    again = await client.get(f"{B.API}/statement-preview/{preview['job_id']}", headers=co["h"])
    assert again.status_code == 200
    out = await B.import_statement(client, co, preview, new_bank={"bank_name": "Main", "bank_type": "checking",
                                                                   "currency": "USD"}, header={
        "opening_balance": "0.00", "closing_balance": "-5.00", "period_start": "2026-01-01", "period_end": "2026-01-31"})
    assert out["bank_id"] and len(out["sessions"]) == 1


_CALLS = {"send_email", "notify_once", "store_file", "apply_doc_payment", "read_statement_files", "model_rung"}


async def test_bank_external_calls_have_failure_paths():
    from celerp_accounting.statement_lines import BANK_CALLS
    found = set()
    for name in ("statement_lines.py", "statement_reader.py", "suggest.py"):
        tree = ast.parse((PKG / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if called in _CALLS:
                    found.add(called)
    assert found, "the bank code reaches calls outside the database"
    assert found <= set(BANK_CALLS), sorted(found - set(BANK_CALLS))
    tests = "".join(p.read_text() for p in HERE.glob("test_bank_*.py"))
    for call, test in BANK_CALLS.items():
        assert f"def {test}(" in tests, (call, test)
