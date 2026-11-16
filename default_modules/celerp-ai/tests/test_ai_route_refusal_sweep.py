# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every route the AI calls refuses in the reader's language, live and after a reload.

One refused request per agent route goes through the real AI executor against the real
app. Its refusal is read as the confirmed action's card shows it and as the reloaded
thread shows it, in Thai and in a stand-in language that marks every catalog string, so
English written in code shows up unmarked. The routes named below are refused by what
their own handler checks; every other agent route gets a generated refusal (_generated)."""
from __future__ import annotations

import html
import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from fasthtml.common import to_xml

from celerp.ai import tools
from celerp_ai import routes, ui_routes

import bank_statement_helpers as B
from ui import i18n

_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("seed_demo", _ROOT / "scripts" / "seed_demo.py")
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

_MARK = "QX:"
_EN = {k: v for f in [Path(i18n.__file__).parent / "locales" / "en.json",
                      *sorted((_ROOT / "default_modules").glob("*/*/locales/en.json"))]
       for k, v in json.loads(f.read_text(encoding="utf-8")).items()}
_NOPE = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def _back_to_english():
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def _capabilities(app) -> dict:
    """The agent capabilities of a company with every bundled module turned on."""
    from fastapi import FastAPI
    from celerp.modules.loader import _BUNDLED_MODULES_DIRS, is_core_folded, load_all, register_api_routes
    root = _BUNDLED_MODULES_DIRS[0]
    names = {p.name for p in root.iterdir()
             if p.is_dir() and (p / "__init__.py").exists() and not is_core_folded(p.name)}
    register_api_routes(FastAPI(docs_url=None, redoc_url=None), load_all(root, names))
    caps = tools.compile_agent_capabilities(app, {}, "owner")
    return {(c["method"], c["path"]): c for c in caps.values()}


# Routes that answer for any id they are given, so a missing record is not refused there.
_ANSWERS_ANY_ID = {
    ("GET", "/companies/me/category-schema/{category}"): "a category with no extra fields reads as none",
    ("GET", "/crm/contacts/{contact_id}/notes"): "a contact with no notes reads as none",
    ("GET", "/docs/{entity_id}/share/state"): "a record with no share link reads as not shared",
    ("POST", "/api/labels/print/{entity_id}"): "a label prints the id it is given when no item has it",
}


def _generated(capability: dict) -> dict:
    """A refused request to a route no case below names: a record that does not exist when
    the path names one, otherwise an argument the route does not take. A download only
    builds its link, so it is refused for the argument, as is a route that answers for
    any id (_ANSWERS_ANY_ID)."""
    if capability["path_names"] and not capability["download"] \
            and (capability["method"], capability["path"]) not in _ANSWERS_ANY_ID:
        return {"path": {n: _NOPE if n.endswith("id") else f"no-such-{n}" for n in capability["path_names"]}}
    return {"query": {"colour": "red"}}


async def _cases(client, h, by_route) -> tuple[list, list, dict]:
    """(name, method, path, arguments) of a refused request to every agent route: the
    named ones and a generated one for each other route; and by named case the arguments
    that reach the company lock where the refused ones do not."""
    named, reaching = await _named_cases(client, h)
    covered = {(m, p) for _n, m, p, _a in named}
    assert covered <= set(by_route), covered - set(by_route)
    return named, [(capability["name"], method, path, _generated(capability))
                   for (method, path), capability in sorted(by_route.items()) if (method, path) not in covered], reaching


async def _named_cases(client, h) -> tuple[list[tuple[str, str, str, dict]], dict]:
    """(name, method, path, arguments) of a refused request each route's own handler checks,
    and by case name the arguments that reach the company lock where the refused ones do not."""
    token = h["Authorization"].split()[1]
    await seed_demo.seed_items(client, token)
    contacts = await seed_demo.seed_contacts(client, token)
    await seed_demo.seed_docs(client, token, contacts, "invoice", 3)
    barcode = "4006381333931"
    coded = await client.post("/items", headers=h, json={
        "sku": "SW-CODED", "name": "Coded", "quantity": 1, "sell_by": "piece", "barcode": barcode})
    assert coded.status_code == 200, coded.text
    items = (await client.get("/items", headers=h, params={"limit": 50})).json()["items"]
    invoices = (await client.get("/docs", headers=h, params={"doc_type": "invoice"})).json()["items"]
    draft = next(d for d in invoices if d["status"] == "draft")
    sent = next(d for d in invoices if d["status"] == "sent")
    vendor = next(c["id"] for c in contacts if c.get("contact_type") == "vendor")

    co = await B.co_of(client, h)
    bank = await B.bank(client, h, name="Harbor Bank", opening=1000.0, opening_date="2026-08-31")
    sid, (l1, l2) = await B.lines_on(client, co, bank["id"], [("2026-09-05", "Deposit", "20.00"),
                                                               ("2026-09-06", "Fee", "-3.00")],
                                     opening="1000.00", closing="1017.00", start="2026-09-01", end="2026-09-30")
    posted = await B.resolve(client, co, bank["id"], decisions=[
        {"line_ids": [l1["id"]], "kind": "post", "account_code": "4100"}])
    assert posted.json()["results"][0]["ok"], posted.text
    other = await B.bank(client, h, name="Quay Bank")
    apart = {"session_id": (await client.post("/accounting/reconciliation/start", headers=h, json={
        "bank_account_id": other["id"], "statement_date": "2026-09-30",
        "statement_balance": 500.0})).json()["id"]}
    on_bank = {"bank_id": bank["id"]}
    nope = [_NOPE]
    unread = B.csv_bytes([("2026-10-02", "Wire", "5.00")])
    preview = await B.read(client, co, [(unread, "oct.csv", "text/csv")], bank_id=bank["id"])
    # A write refused before it waits for the company lock (a record not found) is judged
    # again only once it waits there, so the role-change sweep calls those routes with
    # arguments that reach the lock.
    reaching = {
        "statement_read_no_file": {"body": {"file_ids": [B.upload(co["company_id"], co["user_id"], unread, "r.csv")],
                                            "bank_id": bank["id"]}},
        "statement_import_no_job": {"body": {"job_id": preview["job_id"], "bank_id": bank["id"],
                                             "statements": [{"header": st["header"], "lines": st["lines"]}
                                                            for st in preview["statements"]]}},
        "bank_resolve_no_group": {"path": on_bank, "body": {"decisions": [
            {"line_ids": [l2["id"]], "kind": "post", "account_code": "6100"}]}},
        "bank_suggest_no_bank": {"path": on_bank, "body": {}},
        "bank_unmatch_no_line": {"path": on_bank, "body": {"line_ids": [l1["id"]]}},
        "bank_complete_no_bank": {"path": on_bank, "body": {}},
        "bank_opening_no_bank": {"path": on_bank, "body": {"amount": 1.0, "date": "2026-08-31"}},
        "bank_ask_no_line": {"path": on_bank, "body": {"line_ids": [l2["id"]]}},
        "bank_withdraw_no_line": {"path": on_bank, "body": {"line_ids": [l2["id"]]}},
        "bank_question_answer_no_line": {"path": {"line_id": l2["id"]}, "body": {"answer": "A client lunch"}},
    }
    rec = "/accounting/reconciliation/{session_id}"
    bank_rec = "/accounting/reconciliation/bank/{bank_id}"
    sku = lambda: f"SW-{uuid.uuid4().hex[:6]}"  # noqa: E731
    item = {"name": "Ring", "quantity": 1, "sell_by": "piece"}
    line = [{"description": "x", "quantity": 1, "unit_price": 1}]
    paid = {"amount": 1, "method": "cash", "payment_date": "2026-10-01"}
    return [
        ("bank_accounts_bad_limit", "GET", "/accounting/bank-accounts", {"query": {"limit": "many"}}),
        ("recon_start_no_account", "POST", "/accounting/reconciliation/start", {"body": {
            "bank_account_id": _NOPE, "statement_date": "2026-09-30", "statement_balance": 1.0}}),
        ("statement_read_no_file", "POST", "/accounting/reconciliation/read-statement",
         {"body": {"file_ids": ["nofile"], "bank_id": bank["id"]}}),
        ("statement_import_no_job", "POST", "/accounting/reconciliation/import-statement",
         {"body": {"job_id": _NOPE, "bank_id": bank["id"], "statements": [{"header": {}}]}}),
        ("statement_preview_no_job", "GET", "/accounting/reconciliation/statement-preview/{job_id}",
         {"path": {"job_id": _NOPE}}),
        ("statement_preview_edit_no_job", "POST", "/accounting/reconciliation/statement-preview/{job_id}",
         {"path": {"job_id": _NOPE}, "body": {"statements": [{"header": {}}]}}),
        ("bank_groups_bad_view", "GET", bank_rec + "/groups", {"path": on_bank, "query": {"view": "sideways"}}),
        ("bank_questions_bad_status", "GET", "/accounting/bank-questions", {"query": {"status": "sideways"}}),
        ("transfer_same_bank", "POST", "/accounting/transfers", {"body": {
            "from_bank_id": bank["id"], "to_bank_id": bank["id"], "amount": 5.0, "date": "2026-09-10"}}),
        ("bank_resolve_no_group", "POST", bank_rec + "/resolve",
         {"path": on_bank, "body": {"accept": {"group_keys": ["NO SUCH GROUP"], "line_ids": nope, "versions": {nope[0]: "0"}}}}),
        ("bank_suggest_no_bank", "POST", bank_rec + "/suggest", {"path": {"bank_id": _NOPE}, "body": {}}),
        ("bank_unmatch_no_line", "POST", bank_rec + "/unmatch", {"path": on_bank, "body": {"line_ids": nope}}),
        ("bank_complete_no_bank", "POST", bank_rec + "/complete", {"path": {"bank_id": _NOPE}, "body": {}}),
        ("statement_remove_settled", "DELETE", "/accounting/reconciliation/{session_id}", {"path": {"session_id": sid}, "body": {}}),
        ("bank_opening_no_bank", "PUT", "/accounting/bank-accounts/{bank_id}/opening",
         {"path": {"bank_id": _NOPE}, "body": {"amount": 1.0, "date": "2026-08-31"}}),
        ("line_reclassify_not_suspense", "POST", "/accounting/reconciliation/lines/{line_id}/reclassify",
         {"path": {"line_id": l2["id"]}, "body": {"account_code": "6100"}}),
        ("bank_ask_no_line", "POST", bank_rec + "/ask", {"path": on_bank, "body": {"line_ids": nope}}),
        ("bank_withdraw_no_line", "POST", bank_rec + "/withdraw-question", {"path": on_bank, "body": {"line_ids": nope}}),
        ("bank_question_answer_no_line", "POST", "/accounting/bank-questions/{line_id}/answer",
         {"path": {"line_id": _NOPE}, "body": {"answer": "A client lunch"}}),
        ("recon_reopen_no_session", "POST", rec + "/reopen", {"path": {"session_id": _NOPE}}),
        ("recon_workbench_no_session", "GET", rec + "/workbench", {"path": {"session_id": _NOPE}}),
        ("recon_write_off_bad_account", "POST", rec + "/write-off", {"path": apart, "body": {"account_code": "0000"}}),
        ("contacts_bad_limit", "GET", "/crm/contacts", {"query": {"limit": "many"}}),
        ("contact_no_name", "POST", "/crm/contacts", {"body": {"name": "  "}}),
        ("contact_missing", "GET", "/crm/contacts/{contact_id}", {"path": {"contact_id": "contact:nope"}}),
        ("contact_patch_missing", "PATCH", "/crm/contacts/{contact_id}",
         {"path": {"contact_id": "contact:nope"}, "body": {"fields_changed": {"name": {"new": "x"}}}}),
        ("docs_bad_sort", "GET", "/docs", {"query": {"sort": "sideways"}}),
        ("doc_vendor_on_invoice", "POST", "/docs", {"body": {"doc_type": "invoice", "contact_id": vendor, "line_items": line}}),
        ("doc_bogus_asset", "POST", "/docs",
         {"body": {"doc_type": "invoice", "line_items": [{**line[0], "asset_id": "fa:asset:bogus"}]}}),
        ("docs_summary_unknown_argument", "GET", "/docs/summary", {"query": {"colour": "red"}}),
        ("doc_missing", "GET", "/docs/{entity_id}", {"path": {"entity_id": "doc:nope"}}),
        ("doc_patch_sent_lines", "PATCH", "/docs/{entity_id}",
         {"path": {"entity_id": sent["id"]}, "body": {"fields_changed": {"line_items": {"new": []}}}}),
        ("payment_on_draft", "POST", "/docs/{entity_id}/payment", {"path": {"entity_id": draft["id"]}, "body": paid}),
        ("help_page_missing", "GET", "/help/page/{page_id}", {"path": {"page_id": "no-such-page"}}),
        ("help_search_empty", "GET", "/help/search", {"query": {"q": ""}}),
        ("items_bad_limit", "GET", "/items", {"query": {"limit": "many"}}),
        ("item_reserved", "POST", "/items", {"body": {**item, "sku": sku(), "status": "reserved"}}),
        ("item_duplicate_barcode", "POST", "/items", {"body": {**item, "sku": sku(), "barcode": barcode}}),
        ("item_inventory_type", "POST", "/items", {"body": {**item, "sku": sku(), "inventory_type": "bogus"}}),
        ("item_landed_cost_kind", "POST", "/items", {"body": {**item, "sku": sku(), "landed_cost_kind": "bogus"}}),
        ("import_commit_no_file", "POST", "/items/import/commit", {"body": {"file_id": "nofile", "preview_hash": "0" * 64}}),
        ("import_preview_no_file", "POST", "/items/import/preview", {"body": {"file_id": "nofile"}}),
        ("valuation_bad_limit", "GET", "/items/valuation", {"query": {"limit": "many"}}),
        ("item_missing", "GET", "/items/{entity_id}", {"path": {"entity_id": "item:nope"}}),
        ("item_patch_bad_barcode", "PATCH", "/items/{entity_id}",
         {"path": {"entity_id": items[0]["id"]}, "body": {"fields_changed": {"barcode": {"new": "12ab"}}}}),
        ("reorder_missing_item", "GET", "/items/{entity_id}/reorder-suggestion", {"path": {"entity_id": "item:nope"}}),
        ("search_unknown_argument", "GET", "/search", {"query": {"colour": "red"}}),
    ], reaching


async def _misread(app, h, by_route, cases, before_each=lambda: None) -> tuple[list, dict]:
    """Each case's refusal as the person reads it, in Thai and in the stand-in language:
    what reads wrongly, and the key each route was refused with in the stand-in language."""
    i18n.register_catalog("qx", {k: _MARK + v for k, v in _EN.items()})
    wrong, keys = [], {}
    for lang in ("th", "qx"):
        i18n.set_lang(lang)
        for name, method, path, args in cases:
            capability = by_route[(method, path)]
            before_each()
            result = await tools.execute_agent_capability(app, h["Authorization"], capability, args,
                                                          f"call-{uuid.uuid4().hex}")
            if result.get("ok"):
                wrong.append((name, lang, "not refused", result.get("status")))
                continue
            error = routes._action_error(result)
            keys.setdefault((method, path), set()).add(error.get("message_key"))
            live = ui_routes._outcome_text({"ok": False, "error": error}, lang)[1]
            stored = {"id": "call", "name": capability["name"], "status": "failed", "error": error}
            reloaded = [to_xml(ui_routes._action_card("c", "m", stored, lang)),
                        to_xml(ui_routes._action_status_cell(stored, lang)[1])]
            if not all(live in text or html.escape(live, quote=False) in text for text in reloaded):
                wrong.append((name, lang, "reload differs", live))
            if lang == "qx" and not live.startswith(_MARK):
                wrong.append((name, lang, "English source", live))
    return wrong, keys


@pytest.mark.asyncio
async def test_every_agent_route_refuses_in_the_reader_language_live_and_after_a_reload(client):
    from test_helpers import register_admin
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    app = client._transport.app
    by_route = _capabilities(app)
    named, generated, _reaching = await _cases(client, h, by_route)
    cases = named + generated
    assert {(m, p) for _n, m, p, _a in cases} == set(by_route), set(by_route) ^ {(m, p) for _n, m, p, _a in cases}
    wrong, _keys = await _misread(app, h, by_route, cases)
    assert not wrong, "\n".join(map(repr, wrong))


@pytest.mark.asyncio
async def test_every_agent_write_refuses_in_the_reader_language_when_the_role_changed_while_it_waited(
        client, session, monkeypatch):
    """A write the AI makes waits for the company lock, which judges the caller again
    (company_lock._judge_authority_again). A role changed in the meantime refuses it in
    the reader's language, live and after a reload, on every route that asks the person
    to confirm."""
    from celerp.services import company_lock, permissions
    from test_helpers import register_admin
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    app = client._transport.app
    by_route = _capabilities(app)
    writes = {route for route, capability in by_route.items() if capability["requires_confirmation"]}
    named, generated, reaching = await _cases(client, h, by_route)
    cases = [(name, method, path, reaching.get(name, args)) for name, method, path, args in named + generated
             if (method, path) in writes]
    named = {(m, p) for _n, m, p, _a in named} & writes
    real = permissions.read_authority

    async def changed(session, company_id, user_id):
        _role, settings = await real(session, company_id, user_id)
        return "viewer", settings

    def as_a_new_request():
        """Each request is its own transaction in the app; here they share the test's one,
        so each starts as a request does: not yet judged under the lock, nothing refused."""
        session.sync_session.info.pop(company_lock._PROOFS, None)
        session.sync_session.info.pop(company_lock._REFUSED, None)

    monkeypatch.setattr(permissions, "read_authority", changed)
    wrong, keys = await _misread(app, h, by_route, cases, as_a_new_request)
    as_a_new_request()
    assert not wrong, "\n".join(map(repr, wrong))
    assert {route for route, seen in keys.items() if "error.access_changed" in seen} >= named, "\n".join(
        f"{route}: {keys.get(route)}" for route in sorted(named) if "error.access_changed" not in keys.get(route, ()))
