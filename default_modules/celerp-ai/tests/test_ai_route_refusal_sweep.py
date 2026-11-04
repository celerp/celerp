# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every route the AI calls refuses in the reader's language, live and after a reload.

One refused request per agent route goes through the real AI executor against the real
app. Its refusal is read as the confirmed action's card shows it and as the reloaded
thread shows it, in Thai and in a stand-in language that marks every catalog string, so
English written in code shows up unmarked. A new agent route needs a case here."""
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


async def _cases(client, h) -> list[tuple[str, str, str, dict]]:
    """(name, method, path, arguments) of a refused request to every agent route."""
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

    bank = await client.post("/accounting/bank-accounts", headers=h, json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking",
        "currency": "USD", "opening_balance": 1000.0})
    sid = (await client.post("/accounting/reconciliation/start", headers=h, json={
        "bank_account_id": bank.json()["id"], "statement_date": "2026-09-30",
        "statement_balance": 1020.0})).json()["id"]
    await client.post(f"/accounting/reconciliation/{sid}/import-csv", headers=h, files={
        "file": ("s.csv", b"Date,Description,Amount\n2026-09-05,Deposit,20.00\n2026-09-06,Fee,-3.00\n", "text/csv")})
    body = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)).json()
    l1, l2 = [line["id"] for line in (body.get("items") if isinstance(body, dict) else body)]
    created = await client.post(f"/accounting/reconciliation/{sid}/lines/{l1}/create", headers=h,
                                json={"account_code": "4100"})
    assert created.status_code == 200, created.text
    other = await client.post("/accounting/bank-accounts", headers=h, json={
        "bank_name": "Quay Bank", "account_number": "0002", "bank_type": "checking",
        "currency": "USD", "opening_balance": 0.0})
    apart = {"session_id": (await client.post("/accounting/reconciliation/start", headers=h, json={
        "bank_account_id": other.json()["id"], "statement_date": "2026-09-30",
        "statement_balance": 500.0})).json()["id"]}

    rec = "/accounting/reconciliation/{session_id}"
    on = {"session_id": sid}
    sku = lambda: f"SW-{uuid.uuid4().hex[:6]}"  # noqa: E731
    item = {"name": "Ring", "quantity": 1, "sell_by": "piece"}
    line = [{"description": "x", "quantity": 1, "unit_price": 1}]
    paid = {"amount": 1, "method": "cash", "payment_date": "2026-10-01"}
    return [
        ("bank_accounts_bad_limit", "GET", "/accounting/bank-accounts", {"query": {"limit": "many"}}),
        ("recon_start_no_account", "POST", "/accounting/reconciliation/start", {"body": {
            "bank_account_id": _NOPE, "statement_date": "2026-09-30", "statement_balance": 1.0}}),
        ("recon_auto_match_no_session", "POST", rec + "/auto-match", {"path": {"session_id": _NOPE}}),
        ("recon_bulk_confirm_no_session", "POST", rec + "/bulk-confirm",
         {"path": {"session_id": _NOPE}, "body": {"line_ids": [l2]}}),
        ("recon_complete_difference", "POST", rec + "/complete", {"path": apart}),
        ("recon_import_no_session", "POST", rec + "/import-file",
         {"path": {"session_id": _NOPE}, "body": {"file_id": "nofile"}}),
        ("recon_patch_no_line", "PATCH", rec + "/lines/{line_id}",
         {"path": {**on, "line_id": _NOPE}, "body": {"description": "x"}}),
        ("recon_create_bad_account", "POST", rec + "/lines/{line_id}/create",
         {"path": {**on, "line_id": l2}, "body": {"account_code": "0000"}}),
        ("recon_match_created", "POST", rec + "/lines/{line_id}/match",
         {"path": {**on, "line_id": l1}, "body": {"je_id": str(uuid.uuid4())}}),
        ("recon_unmatch_no_line", "POST", rec + "/lines/{line_id}/unmatch", {"path": {**on, "line_id": _NOPE}}),
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
    ]


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
    cases = await _cases(client, h)
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
    cases = [case for case in await _cases(client, h) if (case[1], case[2]) in writes]
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
    assert {route for route, seen in keys.items() if "error.access_changed" in seen} == writes, keys
